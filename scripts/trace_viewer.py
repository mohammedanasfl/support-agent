"""Show one agent run from a trace file as a readable tree.

Run:  python scripts/trace_viewer.py traces/runs.jsonl

The trace file is written by src/support_desk/tracing.py: one JSON object per
line, one line per run. This script only reads the file; it never changes it.
If the file holds several runs, it lists them and asks which one to show.
"""

import json
import sys

# The keys every run written by tracing.py has, and the keys of each
# iteration and tool call inside it. A run without one of them cannot be
# shown, so it is skipped with a warning instead of crashing the viewer.
RUN_KEYS = ["run_id", "goal", "started_at", "total_duration_ms", "total_tokens",
            "stop_reason", "final_answer", "iterations"]
ITERATION_KEYS = ["iteration", "call_tokens", "tool_calls", "elapsed_ms"]
TOOL_CALL_KEYS = ["tool", "arguments", "result"]


def find_problem(run):
    """Return a short description of what is wrong with a run, or None if it can be shown."""
    if not isinstance(run, dict):
        return "it is not a JSON object"
    for key in RUN_KEYS:
        if key not in run:
            return f"it has no '{key}'"
    if not isinstance(run["iterations"], list):
        return "'iterations' is not a list"

    for record in run["iterations"]:
        if not isinstance(record, dict):
            return "an iteration is not a JSON object"
        for key in ITERATION_KEYS:
            if key not in record:
                return f"an iteration has no '{key}'"

        # tool_calls is None (null) when the model asked for no tool.
        if record["tool_calls"] is None:
            continue
        if not isinstance(record["tool_calls"], list):
            return "an iteration's 'tool_calls' is not a list"
        for tool_call in record["tool_calls"]:
            if not isinstance(tool_call, dict):
                return "a tool call is not a JSON object"
            for key in TOOL_CALL_KEYS:
                if key not in tool_call:
                    return f"a tool call has no '{key}'"

    return None


def read_runs(trace_path):
    """Read the trace file line by line and return a list of the runs that can be shown.

    A bad line is reported with its line number and skipped, so one broken
    line (for example from a run that was cut off while writing) does not
    stop us from showing the other runs.
    """
    runs = []

    # "r" opens the file for reading only, so the viewer cannot change it.
    # errors="replace": a byte that is not valid UTF-8 text becomes a "?"-like
    # character instead of crashing; that line then usually fails json.loads
    # below and is reported like any other bad line.
    with open(trace_path, "r", encoding="utf-8", errors="replace") as trace_file:
        # enumerate(..., start=1) counts lines the way an editor does: 1, 2, 3...
        for line_number, line in enumerate(trace_file, start=1):
            if line.strip() == "":
                continue  # an empty line is not a run, and not an error either

            try:
                run = json.loads(line)
            except json.JSONDecodeError as error:
                print(f"Warning: line {line_number} is not valid JSON, skipped ({error}).")
                continue

            problem = find_problem(run)
            if problem is not None:
                print(f"Warning: line {line_number} is not a complete run, skipped ({problem}).")
                continue

            runs.append(run)

    return runs


def shorten(text, max_length):
    """Cut text down to max_length characters for the run list, adding '...' if it was cut."""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."


def choose_run(runs):
    """Return the run to show: the only one, or the one the user picks from a list."""
    if len(runs) == 1:
        return runs[0]

    print(f"This file has {len(runs)} runs:")
    for number, run in enumerate(runs, start=1):
        print(f"  {number}. {run['run_id']}  {run['stop_reason']}  {shorten(run['goal'], 60)}")
    print()

    # Keep asking until the answer is a valid number or run_id. A wrong
    # answer only prints a message; it never crashes the viewer.
    while True:
        try:
            answer = input(f"Which run? Type a number from 1 to {len(runs)}, or a run_id: ")
        except EOFError:
            # There is no more input (Ctrl+D, or piped input ran out), so
            # asking again would loop forever. Stop with a message instead.
            print()
            sys.exit("No run was chosen.")
        answer = answer.strip()

        # isdigit() is True only for whole numbers like "3", so int() cannot fail.
        if answer.isdigit():
            number = int(answer)
            if number >= 1 and number <= len(runs):
                return runs[number - 1]  # the list starts at 0, the numbers at 1

        for run in runs:
            if run["run_id"] == answer:
                return run

        print(f"'{answer}' is not a number from 1 to {len(runs)} or a run_id in this file. Try again.")


def print_branch(first_prefix, next_prefix, text):
    """Print text as one branch of the tree, even when it has several lines.

    first_prefix goes before the first line, for example "│   └── Result: ".
    next_prefix goes before every other line, so they stay inside the branch
    instead of breaking the tree's lines, for example "│       ".
    """
    lines = str(text).split("\n")
    print(first_prefix + lines[0])
    for line in lines[1:]:
        print(next_prefix + line)


def print_tree(run):
    """Print the run as a tree: the run's details, then each iteration and its tool calls.

    "├──" is a branch with more branches below it, "└──" is the last branch,
    and "│" carries a line down past branches that are not finished yet.
    """
    print(f"Run {run['run_id']}")
    print_branch("├── Goal: ", "│   ", run["goal"])
    print(f"├── Started at: {run['started_at']}")
    print(f"├── Stop reason: {run['stop_reason']}")
    print(f"├── Total tokens: {run['total_tokens']}")
    print(f"├── Total duration: {run['total_duration_ms']} ms")

    iterations = run["iterations"]
    for index, record in enumerate(iterations):
        # The last iteration gets "└──", and nothing more is drawn below it,
        # so the lines under it are indented with spaces instead of "│".
        is_last_iteration = index == len(iterations) - 1
        if is_last_iteration:
            branch = "└── "
            inner = "    "
        else:
            branch = "├── "
            inner = "│   "

        print(f"{branch}Iteration {record['iteration']} "
              f"({record['call_tokens']} tokens, {record['elapsed_ms']} ms)")

        # No tool requested. If this is the last iteration of a run that
        # stopped with a final answer, this model reply WAS the final answer.
        # Otherwise the reply was empty (stop reason empty_response).
        if record["tool_calls"] is None:
            if is_last_iteration and run["stop_reason"] == "final_answer":
                print_branch(inner + "└── Final answer: ", inner + "    ", run["final_answer"])
            else:
                print(inner + "└── No tool requested, and no answer (empty reply)")
            continue

        # One model reply can ask for several tools, so show every one.
        tool_calls = record["tool_calls"]
        for tool_index, tool_call in enumerate(tool_calls):
            if tool_index == len(tool_calls) - 1:
                tool_branch = "└── "
                tool_inner = "    "
            else:
                tool_branch = "├── "
                tool_inner = "│   "

            # result is None (null) when the tool was requested but never run,
            # because the token limit or an escalation ended the run first.
            result = tool_call["result"]
            if result is None:
                result = "(not run)"

            prefix = inner + tool_inner
            print(f"{inner}{tool_branch}Tool: {tool_call['tool']}")
            print_branch(prefix + "├── Arguments: ", prefix + "│   ", tool_call["arguments"])
            print_branch(prefix + "└── Result: ", prefix + "    ", result)


def print_summary(run):
    """Print the final answer, then totals calculated from the iterations."""
    print("=" * 60)
    print("Final answer:")
    if run["final_answer"] is None:
        print("(none: the run ended without an answer)")
    else:
        print(run["final_answer"])
    print("=" * 60)

    # Add up the tokens of every model call, and count every tool request.
    tokens_added_up = 0
    tool_call_count = 0
    for record in run["iterations"]:
        tokens_added_up = tokens_added_up + record["call_tokens"]
        if record["tool_calls"] is not None:
            tool_call_count = tool_call_count + len(record["tool_calls"])

    seconds = run["total_duration_ms"] / 1000

    print("Totals:")
    print(f"  Total tokens:    {tokens_added_up}")
    # The agent also kept its own running total. They should always match;
    # if they do not, the trace file was changed or damaged, so say so.
    if tokens_added_up != run["total_tokens"]:
        print(f"  (Warning: the run itself recorded {run['total_tokens']} tokens)")
    print(f"  Total duration:  {run['total_duration_ms']} ms ({seconds:.1f} seconds)")
    print(f"  Iterations:      {len(run['iterations'])}")
    print(f"  Tool calls:      {tool_call_count}")


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python scripts/trace_viewer.py traces/runs.jsonl")
    trace_path = sys.argv[1]

    # OSError covers a missing file, a folder instead of a file, and a file
    # we are not allowed to read.
    try:
        runs = read_runs(trace_path)
    except OSError as error:
        sys.exit(f"Could not read {trace_path}: {error}")

    if len(runs) == 0:
        sys.exit(f"No runs to show in {trace_path}.")

    run = choose_run(runs)
    print()
    print_tree(run)
    print()
    print_summary(run)


if __name__ == "__main__":
    main()
