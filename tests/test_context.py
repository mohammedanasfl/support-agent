"""Tests for Part 4: loading the system prompt, and context compaction."""

from support_desk import agent
from support_desk.agent import compact_messages
from prompts.system_prompt import SYSTEM_PROMPT
from tests.test_agent import FakeClient, make_text_reply, make_tool_request_reply, run_with


# ---------- Helpers that build message lists by hand ----------

def assistant_asking(*call_ids):
    """An assistant message that asks for one tool call per id."""
    tool_calls = []
    for call_id in call_ids:
        tool_calls.append({
            "id": call_id,
            "type": "function",
            "function": {"name": "get_ticket", "arguments": "{}"},
        })
    return {"role": "assistant", "content": None, "tool_calls": tool_calls}


def tool_result(call_id):
    return {"role": "tool", "tool_call_id": call_id, "content": f"result {call_id}"}


def start_messages():
    return [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "the goal"},
    ]


def roles_and_ids(messages):
    """Short labels such as 'system', 'assistant a', 'tool a', for easy comparing."""
    labels = []
    for message in messages:
        if message["role"] == "assistant":
            labels.append("assistant " + message["tool_calls"][0]["id"])
        elif message["role"] == "tool":
            labels.append("tool " + message["tool_call_id"])
        else:
            labels.append(message["role"])
    return labels


# ---------- The system prompt ----------

def test_system_prompt_has_the_five_sections_in_order():
    sections = ["ROLE", "RULES", "TOOL GUIDANCE", "ESCALATION GUIDANCE", "OUTPUT FORMAT"]
    positions = []
    for section in sections:
        assert section in SYSTEM_PROMPT
        positions.append(SYSTEM_PROMPT.index(section))
    assert positions == sorted(positions)


def test_system_prompt_lists_every_category():
    for category in ["billing", "bug_report", "feature_request", "password_reset", "unclear"]:
        assert category in SYSTEM_PROMPT


def test_system_prompt_gives_guidance_for_every_tool():
    for tool_name in ["get_ticket", "search_tickets", "get_customer_history",
                      "get_refund_policy", "send_reply"]:
        assert tool_name in SYSTEM_PROMPT


# ---------- compact_messages ----------

def test_compaction_keeps_system_goal_and_newest_exchanges():
    messages = start_messages()
    for call_id in ["a", "b", "c", "d"]:
        messages.append(assistant_asking(call_id))
        messages.append(tool_result(call_id))

    removed = compact_messages(messages, keep_exchanges=2)

    assert removed == 4
    assert roles_and_ids(messages) == [
        "system", "user", "assistant c", "tool c", "assistant d", "tool d",
    ]
    assert messages[0]["content"] == "system prompt"
    assert messages[1]["content"] == "the goal"


def test_compaction_never_splits_an_exchange():
    # Exchange "b" asked for two tools at once, so it has two tool messages.
    messages = start_messages()
    messages.append(assistant_asking("a"))
    messages.append(tool_result("a"))
    messages.append(assistant_asking("b1", "b2"))
    messages.append(tool_result("b1"))
    messages.append(tool_result("b2"))
    messages.append(assistant_asking("c"))
    messages.append(tool_result("c"))

    compact_messages(messages, keep_exchanges=2)

    assert roles_and_ids(messages) == [
        "system", "user", "assistant b1", "tool b1", "tool b2", "assistant c", "tool c",
    ]


def test_every_kept_tool_result_still_has_its_request():
    messages = start_messages()
    for call_id in ["a", "b", "c", "d", "e"]:
        messages.append(assistant_asking(call_id))
        messages.append(tool_result(call_id))

    compact_messages(messages, keep_exchanges=2)

    requested_ids = []
    for message in messages:
        if message["role"] == "assistant":
            for tool_call in message["tool_calls"]:
                requested_ids.append(tool_call["id"])
    for message in messages:
        if message["role"] == "tool":
            assert message["tool_call_id"] in requested_ids


def test_compaction_does_nothing_when_there_is_little_history():
    messages = start_messages()
    messages.append(assistant_asking("a"))
    messages.append(tool_result("a"))

    removed = compact_messages(messages, keep_exchanges=2)

    assert removed == 0
    assert len(messages) == 4


# ---------- Compaction inside the agent loop ----------

def test_loop_compacts_when_history_passes_threshold(monkeypatch, capsys):
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", lambda ticket_id: "not it")
    client = FakeClient([make_tool_request_reply(ticket_id=1, tokens=10)])  # asks forever

    result = run_with(
        client,
        max_iterations=6,
        context_message_threshold=6,
        context_keep_exchanges=1,
    )

    # Calls 1-3 send 2, 4, 6 messages. Before call 4 there are 8 (> 6), so the
    # history is cut to system + goal + 1 exchange = 4, and so on.
    assert client.completions.messages_sent == [2, 4, 6, 4, 6, 4]
    assert "[context] compacted 8 messages -> 4 messages" in capsys.readouterr().out

    # Still one message list, and the system prompt and goal are still first.
    for message_list in client.completions.lists_sent:
        assert message_list is result["messages"]
    assert result["messages"][0]["content"] == "test prompt"
    assert result["messages"][1]["content"] == "Look up ticket 20."


def test_loop_prints_nothing_when_nothing_could_be_dropped(monkeypatch, capsys):
    # Over the threshold (4 messages > 3), but there is only one exchange and
    # we keep one, so nothing can be removed and nothing should be printed.
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", lambda ticket_id: "FAKE TICKET")
    client = FakeClient([
        make_tool_request_reply(ticket_id=20, tokens=10),
        make_text_reply("Done.", tokens=10),
    ])

    run_with(client, context_message_threshold=3, context_keep_exchanges=1)

    assert client.completions.messages_sent == [2, 4]
    assert "[context]" not in capsys.readouterr().out


def test_loop_does_not_compact_below_threshold(monkeypatch, capsys):
    monkeypatch.setitem(agent.TOOL_FUNCTIONS, "get_ticket", lambda ticket_id: "FAKE TICKET")
    client = FakeClient([
        make_tool_request_reply(ticket_id=20, tokens=10),
        make_text_reply("Done.", tokens=10),
    ])

    run_with(client, context_message_threshold=6, context_keep_exchanges=1)

    assert client.completions.messages_sent == [2, 4]
    assert "[context]" not in capsys.readouterr().out
