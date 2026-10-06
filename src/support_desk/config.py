"""Settings for the agent: which model to use, the run limits, and the API key."""

import os
import sys

# The model every agent call uses: Google's Gemini 3.5 Flash Lite.
MODEL = "gemini-3.5-flash-lite"

# Gemini offers an OpenAI-compatible endpoint, so we call it with the openai
# SDK. The request and reply have the same shape as before (messages,
# tool_calls, usage), which is why agent.py did not have to change.
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Hard limits for one agent run. They are enforced by the code in agent.py,
# not by asking the model nicely in the prompt.
MAX_ITERATIONS = 10  # the most model calls one run may make
MAX_TOTAL_TOKENS = 50_000  # the most tokens one run may use, added up over all calls
MAX_TOOL_CALLS = 3  # the most times EACH tool may be called in one run (counted per tool)

# Context compaction. Every call sends the whole message history, so a long run
# pays for its old tool results again on every call. When the history is longer
# than CONTEXT_MESSAGE_THRESHOLD messages, the oldest tool exchanges are dropped
# and only the most recent CONTEXT_KEEP_EXCHANGES are kept (plus the system
# prompt and the goal, which are always kept).
CONTEXT_MESSAGE_THRESHOLD = 12
CONTEXT_KEEP_EXCHANGES = 2


def get_api_key():
    """Return the Gemini API key from the environment, or stop with a clear message."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY is not set. Load it first:  set -a; source .env; set +a")
    return api_key
