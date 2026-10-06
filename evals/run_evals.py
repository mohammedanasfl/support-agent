"""Run every eval case through the real agent and report how it did (Part 8).

Run from the project folder:

    set -a; source .env; set +a
    python evals/run_evals.py

This is a harness around the real agent, not a second agent. Each run calls
the same run_agent() as main.py, with the real system prompt, tools,
approval gates, limits and config. The runner only does five things around it:

1. A clean, separate database for every run. scripts/seed.py builds a fresh
   database in a temporary folder, and tools.DB_PATH points at it during the
   run (the same mechanism the test suite uses). The real data/ database is
   never opened, and the temporary one is deleted after the run.

2. A recorded approval answer. send_reply and escalate stop at the human
   approval gate, which reads the answer through run_agent's input_function
   (input() in main.py). There is no person during an eval, so the runner
   passes eval_approver instead: it answers "y" to every proposal and records
   which gate asked. The gates themselves are unchanged: they still run,
   still show the proposal, and nothing runs without that answer. Approving
   is needed to see what the agent does next (for example: a reply, then an
   escalation), and because of 1., it only changes the temporary database.

3. Trace data from the agent's own result. The trajectory, iterations,
   tokens and stop reason come from the dictionary run_agent returns (the
   same data tracing.py writes). Each run is also saved with the real
   write_run_trace() to traces/eval_runs.jsonl (not committed), so a failed
   run can be inspected with:
       python scripts/trace_viewer.py traces/eval_runs.jsonl

4. Checks against the expectations in evals/test_set.py. Only behaviour is
   checked: decisions, tools, tool order, arguments and the presence of key
   facts or known mistakes. Exact wording is never compared.

5. Several runs per case (RUNS_PER_CASE). The model does not always make the
   same choice for the same goal, so one run cannot tell a real improvement
   from chance. The report shows how many runs of each case passed.
"""

import contextlib
import io
import json
import runpy
import sys
import tempfile
from pathlib import Path

import openai
from openai import OpenAI

# "python evals/run_evals.py" puts only the evals/ folder on Python's import
# path. The project folder is added so "prompts" and "evals" can be imported
# the same way main.py and the tests import them.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from evals.test_set import TEST_CASES  # noqa: E402  (needs the line above first)
from prompts.system_prompt import SYSTEM_PROMPT  # noqa: E402
from support_desk import config, tools  # noqa: E402
from support_desk.agent import run_agent  # noqa: E402
from support_desk.guardrails import (  # noqa: E402
    APPROVAL_QUESTION,
    ESCALATION_QUESTION,
    ESCALATION_REJECTION_MESSAGE,
    REJECTION_MESSAGE,
)
from support_desk.tracing import write_run_trace  # noqa: E402

# How many times every case is run. See point 5 at the top.
RUNS_PER_CASE = 3

# seed.py's seed(db_path) builds the database at any path we give it.
SEED = runpy.run_path(str(PROJECT_ROOT / "scripts" / "seed.py"))

# Eval runs get their own trace file, so they do not mix with real runs.
EVAL_TRACE_PATH = PROJECT_ROOT / "traces" / "eval_runs.jsonl"

# Tools that only read data. These may appear between the required tools.
READ_ONLY_TOOLS = ["get_ticket", "search_tickets", "get_customer_history", "get_refund_policy"]


# ---------- Running one case ----------

def run_case(client, case):
    """Run the real agent on one case, in its own temporary database.

    Returns run_agent's result dictionary and the list of approvals given.
    """
    approvals = []

    def eval_approver(question):
        # Record which gate asked, then approve. See point 2 at the top.
        if question == APPROVAL_QUESTION:
            approvals.append("send_reply")
        elif question == ESCALATION_QUESTION:
            approvals.append("escalate")
        else:
            approvals.append(question)
        return "y"

    real_db_path = tools.DB_PATH
    # TemporaryDirectory deletes the folder, and the database in it, at the
    # end of the "with" block, even if the run raised an error.
    with tempfile.TemporaryDirectory() as temp_folder:
        eval_db_path = Path(temp_folder) / "eval.db"
        SEED["seed"](eval_db_path)
        tools.DB_PATH = eval_db_path
        try:
            # The agent prints its progress; hide it so the report stays short.
            # Everything it did is in the result and the trace instead.
            with contextlib.redirect_stdout(io.StringIO()):
                result = run_agent(
                    client=client,
                    goal=case["goal"],
                    model=config.MODEL,
                    system_prompt=SYSTEM_PROMPT,
                    max_iterations=config.MAX_ITERATIONS,
                    max_total_tokens=config.MAX_TOTAL_TOKENS,
                    max_tool_calls=config.MAX_TOOL_CALLS,
                    context_message_threshold=config.CONTEXT_MESSAGE_THRESHOLD,
                    context_keep_exchanges=config.CONTEXT_KEEP_EXCHANGES,
                    input_function=eval_approver,
                )
        finally:
            # Always point the tools back at the real path, even after an error.
            tools.DB_PATH = real_db_path

    return result, approvals


def run_and_evaluate(client, case):
    """Run one case once and check it. Returns a dictionary describing the run."""
    try:
        result, approvals = run_case(client, case)
    except openai.APIError as error:
        # An API failure is not the agent's decision: record it and go on.
        return {"error": str(error)}

    trace = write_run_trace(case["goal"], result, EVAL_TRACE_PATH)
    return {
        "error": None,
        "result": result,
        "approvals": approvals,
        "outcome": evaluate(case, result),
        "run_id": trace["run_id"],
    }


# ---------- Reading the result ----------

def get_tool_calls(result):
    """Return every tool call the model requested, in order, from the trace data.

    Each item is {"tool", "arguments", "result"}, as recorded by agent.py.
    """
    tool_calls = []
    for record in result["iteration_records"]:
        if record["tool_calls"] is not None:
            for tool_call in record["tool_calls"]:
                tool_calls.append(tool_call)
    return tool_calls


def tool_really_ran(tool_call):
    """True if the tool ran and succeeded: not skipped, not an error, not rejected."""
    output = tool_call["result"]
    if output is None or output.startswith("Error:"):
        return False
    if output == REJECTION_MESSAGE or output == ESCALATION_REJECTION_MESSAGE:
        return False
    return True


def read_argument(tool_call, name):
    """Return one argument of a tool call as the model sent it, or None."""
    try:
        arguments = json.loads(tool_call["arguments"])
    except json.JSONDecodeError:
        return None
    if not isinstance(arguments, dict):
        return None
    return arguments.get(name)


def find_decision(result, tool_calls):
    """Work out what the agent actually ended up doing, in the same words as test_set.py."""
    if result["stop_reason"] == "escalated":
        return "escalate"
    for tool_call in tool_calls:
        if tool_call["tool"] == "send_reply" and tool_really_ran(tool_call):
            return "reply"
    if len(tool_calls) == 0:
        final_text = normalize(result["final_text"])
        # The out-of-scope answer says the task is outside the agent's scope.
        # It is checked first, because it may also ask for a ticket id.
        if "scope" in final_text or "not support" in final_text:
            return "decline"
        # Asking for the ticket id is the decision. Whether the answer also
        # uses the right format ("Ticket: not provided", category unclear)
        # is a separate check, done by the category check in evaluate().
        if "ticket id" in final_text or "ticket number" in final_text:
            return "ask_for_ticket_id"
    # It finished without replying or escalating (for example, it only triaged).
    return "no_action"


def find_category(final_text):
    """Return the value of the "Category:" line in the final answer, or None."""
    for line in (final_text or "").splitlines():
        if line.strip().lower().startswith("category:"):
            return line.split(":", 1)[1].strip().lower()
    return None


def find_sent_replies(tool_calls):
    """Return the text of every reply that was really sent, or "" if none was.

    These are the messages the customer received, so they are checked even
    when the decision was wrong.
    """
    messages = []
    for tool_call in tool_calls:
        if tool_call["tool"] == "send_reply" and tool_really_ran(tool_call):
            message = read_argument(tool_call, "message")
            if isinstance(message, str):
                messages.append(message)
    return "\n".join(messages)


def find_escalation_reason(tool_calls):
    """Return the reason of the escalation that really ran, or "" if none did."""
    for tool_call in tool_calls:
        if tool_call["tool"] == "escalate" and tool_really_ran(tool_call):
            reason = read_argument(tool_call, "reason")
            if isinstance(reason, str):
                return reason
    return ""


# ---------- Checks ----------

def normalize(text):
    """Lower case, with curly apostrophes made straight, so "We’ve" matches "we've"."""
    return (text or "").lower().replace("’", "'")


def check_tool_order(trajectory, expected_tools, forbidden_tools):
    """Return a problem description, or None if the order is right.

    The expected tools must appear in this order. Any other tool in between
    is allowed only if it is read-only (a second send_reply is not). A
    forbidden tool is skipped here, because evaluate() already reports it.
    """
    next_expected = 0  # position in expected_tools we are waiting for
    for tool_name in trajectory:
        if next_expected < len(expected_tools) and tool_name == expected_tools[next_expected]:
            next_expected = next_expected + 1
        elif tool_name in forbidden_tools:
            continue
        elif tool_name not in READ_ONLY_TOOLS:
            return f"unexpected {tool_name} call (only read-only tools may be added)"
    if next_expected < len(expected_tools):
        return f"{expected_tools[next_expected]} was not called (in the required order)"
    return None


def check_arguments(tool_calls, expected_arguments):
    """Return a list of problems: each expected argument must match at least one call."""
    problems = []
    for tool_name, wanted in expected_arguments.items():
        found = False
        for tool_call in tool_calls:
            if tool_call["tool"] != tool_name:
                continue
            matches = True
            for key, value in wanted.items():
                if read_argument(tool_call, key) != value:
                    matches = False
            if matches:
                found = True
        if not found:
            problems.append(f"{tool_name} was not called with {wanted}")
    return problems


def missing_facts(text, facts):
    """Return the facts not found in text.

    Each fact is a list of acceptable words; one of them is enough. Case is
    ignored, so only the presence of the fact is checked, not the wording.
    """
    normalized = normalize(text)
    missing = []
    for alternatives in facts:
        found = False
        for word in alternatives:
            if normalize(word) in normalized:
                found = True
        if not found:
            missing.append(alternatives)
    return missing


def found_phrases(text, phrases):
    """Return the phrases that appear in text (case and apostrophe style are ignored)."""
    normalized = normalize(text)
    found = []
    for phrase in phrases:
        if normalize(phrase) in normalized:
            found.append(phrase)
    return found


def evaluate(case, result):
    """Compare one run with its case. Returns a dictionary with the outcome."""
    tool_calls = get_tool_calls(result)
    trajectory = []
    for tool_call in tool_calls:
        trajectory.append(tool_call["tool"])

    decision = find_decision(result, tool_calls)
    decision_ok = decision == case["expected_decision"]

    # Trajectory checks: forbidden tools, order, tool-call limit, arguments.
    trajectory_problems = []
    for tool_name in case["forbidden_tools"]:
        if tool_name in trajectory:
            trajectory_problems.append(f"forbidden tool {tool_name} was called")
    order_problem = check_tool_order(trajectory, case["expected_tools"], case["forbidden_tools"])
    if order_problem is not None:
        trajectory_problems.append(order_problem)
    if case["max_tool_calls"] is not None and len(trajectory) > case["max_tool_calls"]:
        trajectory_problems.append(
            f"{len(trajectory)} tool calls, at most {case['max_tool_calls']} allowed"
        )
    trajectory_problems.extend(check_arguments(tool_calls, case["expected_arguments"]))

    # Reply checks: whenever a reply was really sent, whatever the decision,
    # because the customer received it.
    reply_problems = []
    reply_text = find_sent_replies(tool_calls)
    if reply_text != "":
        for fact in missing_facts(reply_text, case["reply_must_mention"]):
            reply_problems.append(f"reply does not mention any of {fact}")
        for phrase in found_phrases(reply_text, case["reply_must_not_mention"]):
            reply_problems.append(f"reply contains '{phrase}'")

    # Answer checks: only when the decision is right. After a wrong decision
    # the run ends differently (for example with the escalate result instead
    # of the four-field answer), so a missing category or fact there is a
    # side effect of that decision, not a second mistake.
    answer_problems = []
    if decision_ok:
        if case["expected_category"] is not None:
            category = find_category(result["final_text"])
            if category != case["expected_category"]:
                answer_problems.append(
                    f"category {category}, expected {case['expected_category']}"
                )
        for fact in missing_facts(result["final_text"], case["must_mention"]):
            answer_problems.append(f"final answer does not mention any of {fact}")
        reason = find_escalation_reason(tool_calls)
        for fact in missing_facts(reason, case["reason_must_mention"]):
            answer_problems.append(f"escalation reason does not mention any of {fact}")

    problems = []
    if not decision_ok:
        problems.append(f"decision {decision}, expected {case['expected_decision']}")
    problems.extend(trajectory_problems)
    problems.extend(reply_problems)
    problems.extend(answer_problems)

    return {
        "decision": decision,
        "decision_ok": decision_ok,
        "trajectory": trajectory,
        "trajectory_ok": len(trajectory_problems) == 0,
        "problems": problems,
        "passed": len(problems) == 0,
    }


# ---------- Report ----------

def count_passed_runs(runs):
    """Count the runs that finished and passed every check."""
    passed = 0
    for run in runs:
        if run["error"] is None and run["outcome"]["passed"]:
            passed = passed + 1
    return passed


def print_run(run_number, run):
    """Print one run: a summary line, the tools, and the reasons if it failed."""
    prefix = f"      run {run_number}: "
    indent = " " * len(prefix)
    if run["error"] is not None:
        print(f"{prefix}ERROR  API error: {run['error']}")
        return

    result = run["result"]
    outcome = run["outcome"]
    if outcome["passed"]:
        status = "PASS"
    else:
        status = "FAIL"
    if outcome["trajectory_ok"]:
        trajectory_status = "PASS"
    else:
        trajectory_status = "FAIL"
    print(
        f"{prefix}{status}  decision: {outcome['decision']}  "
        f"iterations: {result['iterations']}  tokens: {result['total_tokens']}  "
        f"trajectory: {trajectory_status}"
    )

    if outcome["trajectory"]:
        tools_text = " > ".join(outcome["trajectory"])
    else:
        tools_text = "none"
    if run["approvals"]:
        tools_text = tools_text + f"  (approved by eval approver: {', '.join(run['approvals'])})"
    print(f"{indent}tools: {tools_text}")

    for problem in outcome["problems"]:
        print(f"{indent}reason: {problem}")
    if not outcome["passed"]:
        print(f"{indent}trace: {run['run_id']}")


def print_case(case, runs):
    """Print a case: PASS only if every run passed, then each run."""
    passed = count_passed_runs(runs)
    errors = 0
    for run in runs:
        if run["error"] is not None:
            errors = errors + 1
    summary = f"{passed}/{len(runs)} runs passed"
    if errors > 0:
        # Say so, so an API problem is never read as the agent failing.
        summary = summary + f", {errors} stopped by an API error"
    if passed == len(runs):
        print(f"PASS  {case['name']}  ({summary})")
    else:
        print(f"FAIL  {case['name']}  ({summary})")
    print(f"      expected: {case['expected_decision']}")
    for run_number, run in enumerate(runs, start=1):
        print_run(run_number, run)
    print()


def main():
    # max_retries: the openai SDK waits and retries by itself when the API
    # says "too many requests" (the free tier allows 15 calls a minute).
    # The default is 2 retries, which is not enough for a run of many calls.
    client = OpenAI(api_key=config.get_api_key(), base_url=config.GEMINI_BASE_URL, max_retries=10)

    print(f"Model: {config.MODEL}")
    print(f"Cases: {len(TEST_CASES)}, {RUNS_PER_CASE} runs each")
    print()

    total_runs = 0
    runs_passed = 0
    correct_decisions = 0
    trajectories_passed = 0
    cases_passed_every_run = 0
    iteration_counts = []
    token_counts = []

    for case in TEST_CASES:
        runs = []
        for _ in range(RUNS_PER_CASE):
            runs.append(run_and_evaluate(client, case))
        print_case(case, runs)

        if count_passed_runs(runs) == len(runs):
            cases_passed_every_run = cases_passed_every_run + 1
        for run in runs:
            total_runs = total_runs + 1
            if run["error"] is not None:
                continue  # counted as a run, but it has no decision or tokens
            if run["outcome"]["passed"]:
                runs_passed = runs_passed + 1
            if run["outcome"]["decision_ok"]:
                correct_decisions = correct_decisions + 1
            if run["outcome"]["trajectory_ok"]:
                trajectories_passed = trajectories_passed + 1
            iteration_counts.append(run["result"]["iterations"])
            token_counts.append(run["result"]["total_tokens"])

    # Every number below is over the runs that FINISHED. A run stopped by an
    # API error (for example the daily request limit) has no decision, so
    # counting it would make the agent look worse than it is.
    finished = len(token_counts)

    print("=" * 40)
    print(f"Cases: {len(TEST_CASES)} ({RUNS_PER_CASE} runs each, {total_runs} runs)")
    if finished < total_runs:
        print(f"Runs stopped by an API error: {total_runs - finished} (not counted below)")
    if finished == 0:
        print("No run finished, so there is nothing to report.")
        return
    print(f"Cases passed in every run: {cases_passed_every_run}/{len(TEST_CASES)}")
    print(f"Runs passed (all checks): {runs_passed}/{finished}")
    print(f"Correct decisions: {correct_decisions}/{finished}")
    print(f"Decision accuracy: {correct_decisions / finished:.0%}")
    print(f"Trajectory checks passed: {trajectories_passed}/{finished}")
    print(f"Average iterations: {sum(iteration_counts) / finished:.2f}")
    print(f"Average tokens: {sum(token_counts) / finished:.0f}")
    print(f"Total tokens: {sum(token_counts)}")
    print(f"Traces: {EVAL_TRACE_PATH.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
