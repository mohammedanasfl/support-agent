"""Make one Groq call and print the reply, to prove GROQ_API_KEY works.

Run:  python scripts/check_key.py
"""

import os
import sys

from groq import Groq

MODEL = "qwen/qwen3.8-27b"

# Read the key ourselves so there is exactly one place it can come from and a
# clear message when it is missing.
api_key = os.environ.get("GROQ_API_KEY")
if not api_key:
    sys.exit("GROQ_API_KEY is not set. Load it first:  set -a; source .env; set +a")

client = Groq(api_key=api_key)
response = client.chat.completions.create(
    model=MODEL,
    messages=[{"role": "user", "content": "Reply with exactly: Groq connection successful"}],
)
print(response.choices[0].message.content)
