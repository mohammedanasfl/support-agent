"""Writes one structured JSON trace per run to traces/runs.jsonl.

The file is JSON Lines: every line is one complete JSON object for one run.
A new run is added as a new line at the end, so earlier traces are never
rewritten, and a reader can handle the file one line (one run) at a time.

Only the standard library is used: json to write the text, uuid for run ids.
"""

import json
import uuid
from pathlib import Path

# traces/runs.jsonl in the project folder. parents[2] goes up from
# src/support_desk/tracing.py to the project folder, like DEFAULT_DB_PATH in db.py.
DEFAULT_TRACE_PATH = Path(__file__).resolve().parents[2] / "traces" / "runs.jsonl"


def write_run_trace(goal, result, trace_path=DEFAULT_TRACE_PATH):
    """Append one JSON line describing a finished run, and return that run dictionary.

    result is the dictionary run_agent() returned. Nothing is printed, so
    tracing does not change what the terminal shows.
    """
    run = {
        # A random id that is different for every run, so a run can be found
        # and talked about even when two runs have the same goal.
        "run_id": str(uuid.uuid4()),
        "goal": goal,
        "started_at": result["started_at"],
        "total_duration_ms": result["total_duration_ms"],
        "total_tokens": result["total_tokens"],
        "stop_reason": result["stop_reason"],
        # The model's final answer, or for a run that did not end with one:
        # the escalation result, or the explanation of which limit stopped it.
        # None (null in JSON) after an empty reply.
        "final_answer": result["final_text"],
        "iterations": result["iteration_records"],
    }

    # Create traces/ if it is missing. exist_ok=True: no error if it is there.
    Path(trace_path).parent.mkdir(parents=True, exist_ok=True)

    # "a" means append: add to the end of the file, never overwrite it.
    # ensure_ascii=False keeps characters like "é" readable instead of "é".
    with open(trace_path, "a", encoding="utf-8") as trace_file:
        trace_file.write(json.dumps(run, ensure_ascii=False) + "\n")

    return run
