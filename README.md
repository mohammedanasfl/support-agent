# Support Desk Triage Agent

A Python 3.11+ capstone project: a support desk triage agent that uses Groq
(model `qwen/qwen3.8-27b`) for model calls. The agent loop, tool dispatch, and
message history are written by hand — no agent frameworks (LangChain,
LangGraph, CrewAI, smolagents, Agents SDK).

> Status: Part 1 (data and setup) and Part 2 (the loop) are done.

## Project layout

```
src/support_desk/   The agent package
  main.py           Command-line entry point
  config.py         API key from the environment, model name, run limits
  agent.py          The hand-written agent loop
  tools.py          All tools (four read tools, send_reply, escalate)
  guardrails.py     Code-enforced caps and the human approval gate
  context.py        Message-history compaction
  tracing.py        One JSON trace per run
  db.py             SQLite access
prompts/            System prompt, kept in its own file
scripts/            seed.py, check_key.py, view_trace.py
evals/              Eval test cases and runner
docs/               Tool description note, injection notes, eval results
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

## Groq API key

The key is read from the `GROQ_API_KEY` environment variable. Load it from
`.env` into your shell, then check that it works with one model call:

```bash
set -a; source .env; set +a
python scripts/check_key.py
```

## Running the agent

```bash
python scripts/seed.py           # build the database
python -m support_desk.main      # run the agent on the GOAL set in main.py
```

To change the task, edit `GOAL` in `src/support_desk/main.py` (set it to
`IMPOSSIBLE_GOAL` to see the iteration cap stop a run). To try the limits,
edit `MAX_ITERATIONS` or `MAX_TOTAL_TOKENS` in `src/support_desk/config.py`.

## Running tests

```bash
pytest
```
