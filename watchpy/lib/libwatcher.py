#!/usr/bin/env python2.7
# -*- coding: utf-8 -*-
#
# Copyright 2015, Jianing Yang
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.
#
# Author: Jianing Yang <jianingy.yang@gmail.com>
#
from ansible.inventory import Inventory
from collections import deque, defaultdict
from datetime import datetime
from tornado.gen import coroutine, Return
from tornado.queues import Queue as TornadoQueue
from tornado.tcpclient import TCPClient
from os.path import join as path_join, basename, splitext, exists
from subprocess import call
from yaml import load as yaml_load
from guppy import hpy

# from yaml.parser import ParserError as YAMLParserError
# from yaml.scanner import ScannerError as YAMLScannerError

import objgraph
import logging
import re
import tornado.ioloop
import time

LOG = logging.getLogger('tornado.application')
DEBUG_CONTENT_LENGTH = 20
LAST_HEAP = None
HEAP = hpy()


class WError(Exception):

    message = "An unknown exception occurred."
    errcode = -1

    def __init__(self, **kwargs):
        try:
            message = self.message % kwargs
        except Exception:
            # kwargs doesn't match a variable in the message
            # log the issue and the kwargs
            LOG.exception('Error on formatting exception string')
            for name, value in kwargs.iteritems():
                LOG.error("%s: %s" % (name, value))
            message = self.message

        super(WError, self).__init__(message)

    def __unicode__(self):
        return unicode(self.msg)


class NotImplementError(WError):
    message = "This function has not been implement yet."


class UnsupportedQuerier(WError):
    message = "Querier %(name)s is not supported."


class TornadoQuerierBase(object):

    def __init__(self):
        self.tasks = TornadoQueue()

    def gen_task(self):
        raise NotImplementError()

    def run_task(self, task):
        raise NotImplementError()

    def prepare(self):
        self.running = True

    def cleanup(self):
        self.running = False

    @coroutine
    def run_worker(self, worker_id, f):
        while self.tasks.qsize() > 0:
            task = yield self.tasks.get()
            LOG.debug('worker[%d]: current task is %s' % (worker_id, task))
            try:
                yield f(task)
                pass
            except Exception as e:
                LOG.warning(str(e))
            finally:
                self.tasks.task_done()
                task = None
        LOG.debug('worker[%d]: all tasks done %s' % (worker_id, self.tasks))

    @coroutine
    def start(self, num_workers=1):

        self.prepare()

        # add tasks
        tasks = yield self.gen_task()
        for task in tasks:
            yield self.tasks.put(task)

        # start shoot workers
        for worker_id in range(num_workers):
            LOG.debug('starting worker %d' % worker_id)
            self.run_worker(worker_id, self.run_task)

        yield self.tasks.join()
        self.cleanup()


class TCPQuerier(TornadoQuerierBase):

    def __init__(self, hosts, port, content, timeout=15):
        self.hosts = hosts
        self.port = port
        self.content = content
        self.returns = dict()
        super(TCPQuerier, self).__init__()

    @coroutine
    def gen_task(self):
        raise Return(self.hosts)

    @coroutine
    def run_task(self, host):
        client = TCPClient()
        LOG.debug("connecting to `%s:%s'" % (host, self.port))
        try:
            stream = yield client.connect(host, port=self.port)
            LOG.debug("sending query `%s' to `%s:%s'" % (
                self.content.encode('string-escape'), host, self.port))
            yield stream.write(self.content)
            ret = yield stream.read_until_close()
            resp = str(ret).encode('string-escape')[:DEBUG_CONTENT_LENGTH]
            LOG.debug("`%s:%s' returns `%s'" % (host, self.port, resp))
            stream.close()
            del stream
            self.returns[host] = ret
        except:
            LOG.warn("`%s:%s' return status unknown" % (host, self.port))
            self.returns[host] = None
        finally:
            client = None

    def get_result(self):
        return dict(self.returns)


class StateAnalyzer(object):

    avail_states = ['error', 'warning']

    def __init__(self, filename):
        with open(filename) as f:
            self.rules = yaml_load(f)

    def analyze(self, results):
        return [x for r in results.iteritems() for x in self.do_analyze(r)]

    def do_analyze(self, item):
        name, val = item
        actives = [self.check_state(x, val) for x in self.avail_states]
        return [(name, x, datetime.now())
                for x in set(actives) if x is not None]

    def check_state(self, state, val):
        if state not in self.rules:
            return None
        rules = self.rules[state]
        if all([self.check_state_rule(val, x) for x in rules]):
            return (state, True)
        else:
            return (state, False)

    def check_state_rule(self, val, rule):
        flags = re.MULTILINE

        if val is None:
            return False

        escaped_val = val.encode('string-escape')
        if 'when' in rule and re.search(rule['when'], val, flags):
            LOG.debug("`%s' matched with when(`%s')" %
                      (escaped_val[:DEBUG_CONTENT_LENGTH], rule['when']))
            return True

        if 'unless' in rule and not re.search(rule['unless'], val, flags):
            LOG.debug("`%s' matched with unless(`%s')" %
                      (escaped_val[:DEBUG_CONTENT_LENGTH], rule['unless']))
            return True

        """
        gt/lt examples:
        >   error:
        >     - gt: ['val: (\d+)', 200]
        >     - lt: ['val: (\d+)', 200]
        """

        if 'eq' in rule:
            found = re.search(rule['eq'][0], val, flags)
            if found and found.group(1) == rule['eq'][1]:
                LOG.debug("`%s' is equal to (`%s')" %
                          (escaped_val, rule['eq'][1]))
                return True

        if 'ne' in rule:
            found = re.search(rule['ne'][0], val, flags)
            if found and found.group(1) != rule['ne'][1]:
                LOG.debug("`%s' is not equal to (`%s')" %
                          (escaped_val, rule['ne'][1]))
                return True

        if 'gt' in rule:
            found = re.search(rule['gt'][0], val, flags)
            if found and float(found.group(1)) > float(rule['gt'][1]):
                LOG.debug("`%s' is greater than (`%s')" %
                          (escaped_val, rule['gt'][1]))
                return True

        if 'lt' in rule:
            found = re.search(rule['lt'][0], val, flags)
            if found and float(found.group(1)) < float(rule['lt'][1]):
                LOG.debug("`%s' is lesser than (`%s')" %
                          (escaped_val, rule['lt'][1]))
                return True

        return False


class Watcher(object):

    def __init__(self, config_file, sleep_file, hosts):
        self.name, _ = splitext(basename(config_file))
        self.sleep_file = sleep_file
        with open(config_file) as f:
            watcher_data = yaml_load(f)

        monitor_name = watcher_data['monitor']['use']
        monitor_data = watcher_data['monitor']
        monitor_config = path_join('conf/monitors', monitor_name) + '.yaml'

        self.rules = watcher_data['notification']

        """
        `confirm' examples (3 out of 5 errors means a real error):
        >   monitor:
        >     - confirm: [3, 5]
        """
        span = monitor_data['where']['confirm'][1]
        self.moving_states = defaultdict(lambda: deque(maxlen=span))
        self.events = dict()

        self.freq = monitor_data['where']['frequency']
        self.confirm_round = monitor_data['where']['confirm'][0]

        with open(monitor_config) as f:
            command_data = yaml_load(f)['command']

        self.command_data = command_data
        self.monitor_data = monitor_data
        self.monitor_config = monitor_config
        self.hosts = hosts
        self.workers = int(monitor_data['where']['workers'])

    @coroutine
    def start_app(self):
        if exists(self.sleep_file):
            return

        command_data = self.command_data
        monitor_data = self.monitor_data
        monitor_config = self.monitor_config
        
        if command_data['query'] == 'tcp':
            app = TCPQuerier(self.hosts,
                             command_data['where']['port'],
                             command_data['where']['content'],
                             monitor_data['where']['timeout'])
        else:
            raise UnsupportedQuerier(command_data['query'])

        analyzer = StateAnalyzer(monitor_config)

        yield app.start(self.workers)

        results = app.get_result()
        states_log = analyzer.analyze(results)

        app = None
        analyzer = None

        for item in states_log:
            host, states, started_at = item
            key = '{0}@{1}'.format(states[0], host)
            self.moving_states[key].append((states[1], started_at))

        LOG.debug('moving state = %s' % self.moving_states)
        [self.check_fire_state(event, detail)
         for event, detail in self.moving_states.iteritems()]

        # self.memory_profile()

    def check_fire_state(self, event, detail):
        count = len(filter(lambda x: x[0], detail))
        state, host = event.split('@', 1)
        if count < self.confirm_round:
            if event in self.events:
                self.events.pop(event)
            return

        if event not in self.events:
            self.events[event] = dict(started_at=datetime.now())

        started_at = self.events[event]['started_at']
        event_elapsed = round((datetime.now() - started_at).total_seconds())
        LOG.debug('event elapsed = %s' % int(event_elapsed))
        for rule in self.rules:
            if state not in rule['states']:
                continue
            if event_elapsed <= int(rule['start']):
                continue
            if event_elapsed > int(rule.get('stop', 86400)):
                continue
            fired_at = self.events[event].get('fired_at',
                                              datetime.fromtimestamp(0))
            fired_elapsed = round((datetime.now() - fired_at).total_seconds())
            if fired_elapsed < int(rule['frequency']):
                continue
            LOG.info("fire event: %s (count=%d) with `%s'. "
                     "event_elapsed=%s, fired_elapsed=%s"
                     % (event, count, rule['exec'],
                        event_elapsed, fired_elapsed))
            [self.fire_event(c, state=state, host=host) for c in rule['exec']]
            self.events[event]['fired_at'] = datetime.now()

    def fire_event(self, command, **kwargs):
        command = command.format(**kwargs)
        LOG.info("exec `%s'" % command)
        call(command, shell=True)

    def run_once(self):
        io_loop = tornado.ioloop.IOLoop.current()
        io_loop.run_sync(self.start_app)

    def memory_profile(self):
        global LAST_HEAP
        print "{:=^78}".format(' profile begin')
        import gc
        gc.collect()
        print "{:-^78}".format(' growth')
        print objgraph.show_growth(limit=5)
        print "{:-^78}".format(' common types')
        print objgraph.show_most_common_types(limit=5)
        if LAST_HEAP:
            leftover = HEAP.heap() - LAST_HEAP
            print leftover.byrcs[0].byid
        LAST_HEAP = HEAP.heap()

        print "{:=^78}".format(' profile end')

    def start(self):
        task = tornado.ioloop.PeriodicCallback(self.start_app,
                                               self.freq * 1000)
        task.start()


if __name__ == '__main__':
    from tornado.log import enable_pretty_logging
    from tornado.options import parse_command_line

    enable_pretty_logging()
    parse_command_line()

    # wap = Watcher('conf/watchers/nginx.yaml',
    #               ['localhost',
    #                'localhost'])
    # wap.run()
