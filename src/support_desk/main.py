"""Run the agent from the terminal: type a goal, then any follow-up questions.

    python -m support_desk.main

The program asks for a goal and runs the agent on it. When the run ends, it
asks for a follow-up question. A follow-up continues the same conversation:
the agent gets the whole history so far, so it can answer from what it has
already read and done. Press Enter on an empty line (or Ctrl+D) to finish.

To try the limits, change MAX_ITERATIONS, MAX_TOTAL_TOKENS, MAX_TOOL_CALLS or
CONTEXT_MESSAGE_THRESHOLD in config.py. The limits apply to each question.
"""

import sys

from openai import OpenAI

from prompts.system_prompt import SYSTEM_PROMPT
from support_desk import config
from support_desk.agent import run_agent
from support_desk.db import DEFAULT_DB_PATH
from support_desk.tracing import write_run_trace


def ask(question):
    """Return what the person types, without spaces around it.

    Ctrl+D (the end of input) counts as an empty answer, so it finishes the
    program cleanly instead of crashing it.
    """
    try:
        return input(question).strip()
    except EOFError:
        print()
        return ""


def print_result(result):
    print()
    print(f"Stop reason: {result['stop_reason']}")
    print(f"Iterations:  {result['iterations']}")
    print(f"Tokens:      {result['total_tokens']}")
    print(f"Messages:    {len(result['messages'])}")
    if result["final_text"] is not None:
        print()
        print("Final answer:")
        print(result["final_text"])


def main():
    if not DEFAULT_DB_PATH.exists():
        sys.exit("No database found. Run:  python scripts/seed.py")

    # The openai SDK, pointed at Gemini's OpenAI-compatible endpoint.
    client = OpenAI(api_key=config.get_api_key(), base_url=config.GEMINI_BASE_URL)

    print(f"Model: {config.MODEL}")
    print(
        f"Limits: {config.MAX_ITERATIONS} iterations, {config.MAX_TOTAL_TOKENS} tokens, "
        f"{config.MAX_TOOL_CALLS} calls per tool"
    )
    print(
        f"Context: compact above {config.CONTEXT_MESSAGE_THRESHOLD} messages, "
        f"keep the last {config.CONTEXT_KEEP_EXCHANGES} tool exchanges"
    )
    print()

    goal = ask("Goal: ")

    # The conversation so far: None before the first goal, then the message
    # list of the last run, which a follow-up question continues.
    history = None

    while goal != "":
        print()
        result = run_agent(
            client=client,
            goal=goal,
            model=config.MODEL,
            system_prompt=SYSTEM_PROMPT,
            max_iterations=config.MAX_ITERATIONS,
            max_total_tokens=config.MAX_TOTAL_TOKENS,
            max_tool_calls=config.MAX_TOOL_CALLS,
            context_message_threshold=config.CONTEXT_MESSAGE_THRESHOLD,
            context_keep_exchanges=config.CONTEXT_KEEP_EXCHANGES,
            history=history,
        )

        # Save the run to traces/runs.jsonl, one trace per goal or follow-up
        # question. This prints nothing. If run_agent raised an error, we never
        # get here: the error shows exactly as before, and no trace is written
        # for the unfinished run.
        write_run_trace(goal, result)
        print_result(result)

        history = result["messages"]
        print()
        goal = ask("Follow-up question (press Enter to finish): ")


if __name__ == "__main__":
    main()
