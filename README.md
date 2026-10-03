# Support Desk Triage Agent

A Python 3.11+ capstone project: a support desk triage agent that uses the
Google Gemini API for model calls. The agent loop, tool dispatch, and message
history are written by hand — no agent frameworks (LangChain, LangGraph,
CrewAI, smolagents, Agents SDK).

> Status: project skeleton only. No agent logic is implemented yet.

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

## Gemini API key

The key is read from the `GEMINI_API_KEY` environment variable. Load it from
`.env` into your shell, then check that it works with one model call:

```bash
set -a; source .env; set +a
python scripts/check_key.py
```

## Running tests

```bash
pytest
```
