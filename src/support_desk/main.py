"""Run the agent once on the goal written below.

    python -m support_desk.main

To give the agent a different task, change GOAL. To try the limits, change
MAX_ITERATIONS or MAX_TOTAL_TOKENS in config.py.
"""

import sys

from groq import Groq

from support_desk import config
from support_desk.agent import run_agent
from support_desk.db import DEFAULT_DB_PATH

# Temporary system prompt for Part 2. Part 4 replaces it with prompts/system_prompt.md.
SYSTEM_PROMPT = (
    "You are a support desk triage assistant. Use the tools to look up "
    "information instead of guessing."
)

# A simple goal the agent can complete: look up one ticket and summarise it.
TICKET_GOAL = (
    "Look up ticket 20. In two or three sentences, say what the customer needs "
    "and which category it belongs to: billing, bug report, feature request, "
    "password reset, or unclear."
)

# A goal that cannot succeed: no ticket mentions 'xylophone', and the goal
# tells the model to keep looking. Used to show that the iteration cap works.
IMPOSSIBLE_GOAL = (
    "Find the ticket whose body mentions the word 'xylophone'. Check tickets one "
    "at a time with get_ticket, starting at id 1 and counting upwards. Ticket ids "
    "go up to at least 100000 and missing ids are normal. Do not stop or give an "
    "answer until you have found it."
)

# The goal for this run. Change this line to IMPOSSIBLE_GOAL to see the cap stop it.
GOAL = TICKET_GOAL


def main():
    if not DEFAULT_DB_PATH.exists():
        sys.exit("No database found. Run:  python scripts/seed.py")

    client = Groq(api_key=config.get_api_key())

    print(f"Model: {config.MODEL}")
    print(f"Limits: {config.MAX_ITERATIONS} iterations, {config.MAX_TOTAL_TOKENS} tokens")
    print(f"Goal: {GOAL}")
    print()

    result = run_agent(
        client=client,
        goal=GOAL,
        model=config.MODEL,
        system_prompt=SYSTEM_PROMPT,
        max_iterations=config.MAX_ITERATIONS,
        max_total_tokens=config.MAX_TOTAL_TOKENS,
    )

    print()
    print(f"Stop reason: {result['stop_reason']}")
    print(f"Iterations:  {result['iterations']}")
    print(f"Tokens:      {result['total_tokens']}")
    print(f"Messages:    {len(result['messages'])}")
    if result["final_text"] is not None:
        print()
        print("Final answer:")
        print(result["final_text"])


if __name__ == "__main__":
    main()
