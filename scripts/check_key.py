"""Make one Gemini call and print the reply, to prove GEMINI_API_KEY works.

Run:  python scripts/check_key.py
"""

import os
import sys

from google import genai
from google.genai import types

MODEL = "gemini-3.5-flash-lite"

# Read the key ourselves instead of letting genai.Client() find it, so there is
# exactly one place it can come from and a clear message when it is missing.
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    sys.exit("GEMINI_API_KEY is not set. Load it first:  set -a; source .env; set +a")

client = genai.Client(api_key=api_key)
response = client.models.generate_content(
    model=MODEL,
    contents="Reply with exactly: Gemini connection successful",
    # The SDK can run tool calls and re-call the model by itself ("automatic
    # function calling"). It is on by default; we turn it off because the brief
    # requires our own loop. With no tools here it only silences a warning.
    config=types.GenerateContentConfig(
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    ),
)
print(response.text)
