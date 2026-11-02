"""Tests for follow-up questions: several runs that share one conversation.

main.py reads a goal from the terminal, then follow-up questions. Each
follow-up is a new run_agent() call that gets the last run's messages as
history. These tests use the fake client from test_agent.py, so they never
call the real API.
"""

from pathlib import Path

import support_desk.main as main_module
from support_desk import agent
from support_desk.agent import NOT_RUN_MESSAGE, run_agent
from tests.test_agent import FakeClient, make_text_reply, make_tool_request_reply, run_with
from tests.test_guardrails import no_input_allowed


def ask_follow_up(client, history, question, max_iterations=10, max_total_tokens=100_000,
                  context_message_threshold=1000, context_keep_exchanges=2):
    return run_agent(
        client=client,
        goal=question,
        model="fake-model",
        system_prompt="test prompt",
        max_iterations=max_iterations,
        max_total_tokens=max_total_tokens,
        max_tool_calls=1000,
        context_message_threshold=context_message_threshold,
        context_keep_exchanges=context_keep_exchanges,
        input_function=no_input_allowed,
        history=history,
    )


def roles(messages):
    return [message["role"] for message in messages]


# ---------- A follow-up continues the conversation ----------

def test_follow_up_question_sees_the_whole_earlier_conversation(monkeypatch):
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", lambda ticket_id: "FAKE TICKET 20")
    first = run_with(FakeClient([
        make_tool_request_reply(ticket_id=20, tokens=10),
        make_text_reply("First answer.", tokens=10),
    ]))

    client = FakeClient([make_text_reply("Second answer.", tokens=10)])
    second = ask_follow_up(client, first["messages"], "Why?")

    # The follow-up call got the system prompt, the goal, the tool exchange
    # and the first answer, with the question at the end: 6 messages.
    assert client.completions.messages_sent == [6]
    assert client.completions.lists_sent[0] is first["messages"]
    assert roles(second["messages"]) == [
        "system", "user", "assistant", "tool", "assistant", "user", "assistant",
    ]
    assert second["messages"][0]["content"] == "test prompt"  # only one system prompt
    assert second["messages"][3]["content"] == "FAKE TICKET 20"
    assert second["messages"][5]["content"] == "Why?"
    assert second["final_text"] == "Second answer."
    # Counts start again for each question.
    assert second["iterations"] == 1
    assert second["total_tokens"] == 10


# ---------- Requests that never ran get a result first ----------

def test_follow_up_after_the_token_limit_closes_the_requests_that_never_ran():
    # The run stops at the token limit right after the model asked for a
    # tool, so that request has no tool message.
    first = run_with(FakeClient([make_tool_request_reply(ticket_id=20, tokens=10)]),
                     max_total_tokens=5)
    assert first["stop_reason"] == "token_budget"
    assert roles(first["messages"]) == ["system", "user", "assistant"]

    second = ask_follow_up(FakeClient([make_text_reply("Answer.", tokens=10)]),
                           first["messages"], "What happened?")

    assert roles(second["messages"]) == ["system", "user", "assistant", "tool", "user", "assistant"]
    assert second["messages"][3] == {
        "role": "tool", "tool_call_id": "call_20", "content": NOT_RUN_MESSAGE,
    }


def test_follow_up_closes_only_the_requests_without_a_result():
    # As after a successful escalation: the first request ran, the run
    # ended, and the second request in the same reply never ran.
    history = [
        {"role": "system", "content": "test prompt"},
        {"role": "user", "content": "Handle ticket 28."},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "escalate", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "get_ticket", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": "escalated"},
    ]

    ask_follow_up(FakeClient([make_text_reply("Answer.", tokens=10)]), history, "Why?")

    assert history[3]["content"] == "escalated"  # unchanged
    assert history[4] == {"role": "tool", "tool_call_id": "b", "content": NOT_RUN_MESSAGE}
    assert history[5] == {"role": "user", "content": "Why?"}


# ---------- Compaction keeps the questions and answers ----------

def test_compaction_in_a_follow_up_never_drops_the_question(monkeypatch):
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", lambda ticket_id: "FAKE TICKET")
    first = run_with(FakeClient([
        make_tool_request_reply(ticket_id=20, tokens=10),
        make_text_reply("First answer.", tokens=10),
    ]))

    client = FakeClient([make_tool_request_reply(ticket_id=20, tokens=10)])  # asks forever
    second = ask_follow_up(client, first["messages"], "Any more?", max_iterations=4,
                           context_message_threshold=8, context_keep_exchanges=1)

    # Before call 3 there are 10 messages (> 8): the oldest exchange of this
    # run is dropped. The first run's get_ticket exchange (the original
    # ticket), the first answer and the question all stay.
    assert client.completions.messages_sent == [6, 8, 8, 8]
    contents = [message["content"] for message in second["messages"]]
    assert "Look up ticket 20." in contents
    assert "First answer." in contents
    assert "Any more?" in contents
    assert roles(second["messages"][:6]) == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert second["messages"][3]["content"] == "FAKE TICKET"  # the original ticket
    assert second["iteration_records"][2]["compaction"]["dropped"] == [
        {"tool": "get_ticket", "arguments": '{"ticket_id": 20}', "result": "FAKE TICKET"},
    ]


# ---------- main.py asks for follow-ups ----------

def run_main(monkeypatch, client, answers):
    """Run main() with a fake client and typed answers; return the goals it traced."""
    monkeypatch.setattr(main_module, "OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(main_module.config, "get_api_key", lambda: "fake-key")
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", Path(__file__))  # any file that exists
    traced_goals = []
    monkeypatch.setattr(main_module, "write_run_trace",
                        lambda goal, result: traced_goals.append(goal))

    answers = iter(answers)

    def fake_input(question):
        answer = next(answers)
        if answer is EOFError:
            raise EOFError
        return answer

    monkeypatch.setattr("builtins.input", fake_input)
    main_module.main()
    return traced_goals


def test_main_runs_the_goal_then_follow_ups_in_one_conversation(monkeypatch, capsys):
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", lambda ticket_id: "FAKE TICKET")
    client = FakeClient([
        make_tool_request_reply(ticket_id=20, tokens=10),
        make_text_reply("First answer.", tokens=10),
        make_text_reply("Second answer.", tokens=10),
    ])

    traced_goals = run_main(monkeypatch, client, ["Look up ticket 20.", "Why?", ""])

    # One trace per question, and the follow-up call got the whole history.
    assert traced_goals == ["Look up ticket 20.", "Why?"]
    assert client.completions.messages_sent == [2, 4, 6]
    output = capsys.readouterr().out
    assert "First answer." in output
    assert "Second answer." in output


def test_main_finishes_cleanly_on_an_empty_goal_or_ctrl_d(monkeypatch):
    client = FakeClient([make_text_reply("Never used.", tokens=10)])

    assert run_main(monkeypatch, client, [""]) == []
    assert run_main(monkeypatch, client, [EOFError]) == []
    assert client.completions.messages_sent == []
