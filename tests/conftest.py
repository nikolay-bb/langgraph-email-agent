"""What every test shares. pytest loads this file before the test files."""
import os

# The tests run the real graph nodes and tools against fakes, and LangSmith
# tracing is on in .env, so each run wrote about 23 rows into the live project
# (5 Oct). They go to a project of their own. It is set here, before a test
# file imports the agent: load_dotenv() keeps a variable that is already set,
# and LangSmith reads the project name once. tests/bot/harness.py imports
# this file, for a bot suite run on its own.
os.environ["LANGSMITH_PROJECT"] = "email-agent-tests"
# No test sends a trace: the tests of a public copy must leave nothing at
# LangSmith, whatever .env says. load_dotenv() keeps this value too.
os.environ["LANGSMITH_TRACING"] = "false"
