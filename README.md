# Support Desk Triage Agent

A Python 3.11+ capstone project: a support desk triage agent that uses the
Google Gemini API for model calls. The agent loop, tool dispatch, and message
history are written by hand — no agent frameworks (LangChain, LangGraph,
CrewAI, smolagents, Agents SDK).

> Status: project skeleton only. No agent logic is implemented yet.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then put your real key in .env (never commit it)
```

## Running tests

```bash
pytest
```
