"""Run the agent once on the goal written below.

    python -m support_desk.main

To give the agent a different task, change GOAL. To try the limits, change
MAX_ITERATIONS, MAX_TOTAL_TOKENS, MAX_TOOL_CALLS or CONTEXT_MESSAGE_THRESHOLD
in config.py.
"""

import sys

from groq import Groq

from prompts.system_prompt import SYSTEM_PROMPT
from support_desk import config
from support_desk.agent import run_agent
from support_desk.db import DEFAULT_DB_PATH

# A normal triage task. HOW to triage (order of work, categories, answer
# format) is in the system prompt, so the goal only says which ticket and
# what to do. It asks for a reply explicitly, because the prompt only allows
# send_reply when the task asks for one; the reply then goes through the
# human approval gate.
TICKET_GOAL = "what is api ?"

# A goal that cannot succeed: no ticket mentions 'xylophone', and the goal
# tells the model to keep looking. Used to show that the iteration cap and
# context compaction work.
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
    print(
        f"Limits: {config.MAX_ITERATIONS} iterations, {config.MAX_TOTAL_TOKENS} tokens, "
        f"{config.MAX_TOOL_CALLS} calls per tool"
    )
    print(
        f"Context: compact above {config.CONTEXT_MESSAGE_THRESHOLD} messages, "
        f"keep the last {config.CONTEXT_KEEP_EXCHANGES} tool exchanges"
    )
    print(f"Goal: {GOAL}")
    print()

    result = run_agent(
        client=client,
        goal=GOAL,
        model=config.MODEL,
        system_prompt=SYSTEM_PROMPT,
        max_iterations=config.MAX_ITERATIONS,
        max_total_tokens=config.MAX_TOTAL_TOKENS,
        max_tool_calls=config.MAX_TOOL_CALLS,
        context_message_threshold=config.CONTEXT_MESSAGE_THRESHOLD,
        context_keep_exchanges=config.CONTEXT_KEEP_EXCHANGES,
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
