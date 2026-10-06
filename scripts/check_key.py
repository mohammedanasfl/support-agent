"""Make one Gemini call and print the reply, to prove GEMINI_API_KEY works.

Run:  python scripts/check_key.py
"""

from openai import OpenAI

from support_desk import config

# get_api_key() stops with a clear message if GEMINI_API_KEY is not set.
client = OpenAI(api_key=config.get_api_key(), base_url=config.GEMINI_BASE_URL)
response = client.chat.completions.create(
    model=config.MODEL,
    messages=[{"role": "user", "content": "Reply with exactly: Gemini connection successful"}],
)
print(response.choices[0].message.content)
