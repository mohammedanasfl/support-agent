# Support Desk Triage Agent

A Python 3.11+ capstone project: a support desk triage agent that uses Google
Gemini (model `gemini-3.5-flash-lite`) for model calls, through Gemini's
OpenAI-compatible endpoint and the `openai` SDK. The agent loop, tool dispatch, and
message history are written by hand — no agent frameworks (LangChain,
LangGraph, CrewAI, smolagents, Agents SDK).

> Status: all eight parts are done — data and setup, the agent loop, tools,
> system prompt and context compaction, guardrails and human approval, the
> prompt-injection test, tracing, and evals.

## What the agent does

- Reads support tickets from a local SQLite database and triages them: a
  category, a short summary, and the next step.
- Uses six tools. Four only read data: `get_ticket`, `search_tickets`,
  `get_customer_history`, `get_refund_policy`. Two change data and only run
  after a person approves them at the terminal: `send_reply` and `escalate`.
- Enforces hard limits in code: model calls per run, tokens per run, and calls
  per tool.
- Saves every run as one JSON line in `traces/runs.jsonl`.

## Project layout

```
src/support_desk/   The agent package
  main.py           Runs the agent once on the GOAL written in this file
  config.py         Model, API key from the environment, run limits
  agent.py          The hand-written agent loop, including context compaction
  tools.py          The six tools and their descriptions
  guardrails.py     Human approval prompts and the per-tool limit message
  tracing.py        Writes one JSON trace per run
  db.py             SQLite access
prompts/            The system prompt (system_prompt.py)
scripts/            seed.py, check_key.py, trace_viewer.py
evals/              test_set.py (12 cases), run_evals.py (runner), eval.md (report)
docs/               Tool description note, injection notes, tracing note, eval results
data/               SQLite database (generated, not committed)
traces/             Run traces (generated, not committed)
tests/              pytest unit tests
```

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .        # makes `support_desk` importable from scripts/ and evals/
cp .env.example .env    # then put your real key in .env (never commit it)
```

## Gemini API key

The key is read from the `GEMINI_API_KEY` environment variable. Load it from
`.env` into your shell, then check that it works with one model call:

```bash
set -a; source .env; set +a
python scripts/check_key.py
```

## Running the agent

```bash
python scripts/seed.py           # build (or reset) the database
python -m support_desk.main      # run the agent on the GOAL set in main.py
```

- To change the task, edit `GOAL` in `src/support_desk/main.py`.
- When the agent wants to send a reply or escalate, it stops and shows the
  proposal and the tools that really ran so far. Type `y` to approve; anything
  else rejects it, and the agent is told it was not done.
- To see a cap stop a run, lower `MAX_ITERATIONS` (for example to 2),
  `MAX_TOTAL_TOKENS` or `MAX_TOOL_CALLS` in `src/support_desk/config.py`.

## Viewing traces

```bash
python scripts/trace_viewer.py traces/runs.jsonl
```

The viewer lists the runs in the file, then prints the chosen run as a tree:
every iteration with its tool calls, arguments, results, tokens and time.

## Running the evals

```bash
python evals/run_evals.py
```

Each of the 12 cases in `evals/test_set.py` runs 3 times with the real agent,
each time on a fresh temporary database. Proposals are approved automatically
by the eval approver. Eval traces go to `traces/eval_runs.jsonl`. A full run
makes about 120 model calls (the Gemini free tier allows 500 requests a day
for this model).

## Notes and results

- `docs/tool-description-note.md`: the rewritten `search_tickets` description
  and how it changed the agent's searches (Part 3)
- `docs/injection-notes.md`: the planted injection ticket and the lethal
  trifecta (Part 6)
- `docs/tracing-note.md`: a failure investigated with the trace viewer (Part 7)
- `docs/eval-results.md`: before and after numbers for one change (Part 8)
- `evals/eval.md`: the final evaluation report (Part 8)

## Running tests

```bash
pytest
```
