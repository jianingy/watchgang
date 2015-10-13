module Main where

import Control.Monad.IO.Class
import Data.Text as T
import qualified Text.ParserCombinators.Parsec as P
import Text.Parsec ((<|>))
import System.IO
import System.Process


data ModuleName = ModuleName String deriving (Show)
data ModuleArgument = ModuleArgument String deriving (Show)
data Command = Command { cmdModule :: ModuleName
                       , cmdArgs :: [ModuleArgument]
                       } deriving (Show)


ident :: P.Parser String
ident = P.many (P.oneOf "-_" <|> P.letter <|> P.digit)

symbol :: P.Parser Char
symbol = P.oneOf "!$%&|*+-/:<=>?@^_~"

escapes :: P.Parser Char
escapes = P.char '\\' >> P.oneOf "\\\"rnt" >>= \x -> return $ case x of
    'r' -> '\r'
    'n' -> '\n'
    't' -> '\t'
    _ -> x

argumentToString :: ModuleArgument -> String
argumentToString (ModuleArgument x) = x

parseModuleName :: P.Parser ModuleName
parseModuleName = ident >>= \x -> return $ ModuleName x

parseQuotedString :: P.Parser ModuleArgument
parseQuotedString = do
  _ <- P.char '"'
  s <- P.many1 $ escapes <|> P.noneOf "\""
  _ <- P.char '"'
  return $ ModuleArgument s

parseString :: P.Parser ModuleArgument
parseString = do
  s <- P.many1 (P.letter <|> P.digit <|> symbol)
  return $ ModuleArgument s

parseCommand :: P.Parser Command
parseCommand = do
  name <- parseModuleName
  args <- P.option [] (P.char ' ' >> P.sepEndBy (parseQuotedString <|> parseString) P.spaces)

  return $ Command name args


runModule :: Command -> Either String (IO String)
runModule Command {cmdModule=(ModuleName "dmesg")} =
  Right $ readProcess "dmesg" [] []

runModule Command {cmdModule=(ModuleName "uptime")} =
  Right $ readProcess "uptime" [] []

runModule Command {cmdModule=(ModuleName "md5sum"), cmdArgs=args} =
  Right $ readProcess "md5sum" [filepath] []
  where filepath=argumentToString $ args !! 0

runModule Command {cmdModule=(ModuleName "sha1sum"), cmdArgs=args} =
  Right $ readProcess "sha1sum" [filepath] []
  where filepath=argumentToString $ args !! 0

runModule Command {cmdModule=(ModuleName "pidof"), cmdArgs=args} =
  Right $ readProcess "pidof" [process] []
  where process=argumentToString $ args !! 0

runModule Command {cmdModule=(ModuleName "tail"), cmdArgs=args} =
  Right $ readProcess "tail" [filepath] []
  where filepath=argumentToString $ args !! 0

runModule _ = Left "Invalid Module"

main :: IO ()
main = do
  input <- hGetLine stdin
  either (\_ -> err "Syntax Error") run $ P.parse parseCommand "(command)" input
  where run val = either err out $ runModule val
        out s = s >>= putStrLn . T.unpack . T.strip . T.pack
        err s = do putStrLn $ "ERROR: " ++ s
