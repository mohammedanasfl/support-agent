"""The agent loop.

run_agent() works like this:

    messages = [system prompt, user goal]
    repeat, at most max_iterations times:
        send ALL the messages to the model
        add the model's reply to messages
        if the model did not ask for a tool -> stop: that reply is the final answer
        otherwise run the tool ourselves and add the result to messages

The model never runs our code. It can only ASK for a tool by name. Our Python
code decides which function to run, runs it, and decides when to stop.

Every message is a plain Python dictionary with a "role":
    "system"     instructions for the model
    "user"       the goal we give the agent
    "assistant"  a reply from the model (text, tool requests, or both)
    "tool"       the result of one tool call, linked to its request by id
"""

import json

from support_desk.tools import TOOL_DECLARATIONS, TOOL_FUNCTIONS


def run_tool(tool_name, arguments_json):
    """Find the Python function for a tool name, run it, and return its result.

    Always returns a string, so a mistake becomes a message the model can read
    and recover from, instead of crashing the program.
    """
    if tool_name not in TOOL_FUNCTIONS:
        return f"Error: unknown tool '{tool_name}'."

    # The model sends the arguments as JSON text, e.g. '{"ticket_id": 20}'.
    # json.loads turns that text into a Python dictionary: {"ticket_id": 20}.
    try:
        tool_args = json.loads(arguments_json)
    except json.JSONDecodeError:
        return f"Error: the arguments for {tool_name} are not valid JSON: {arguments_json}"

    tool_function = TOOL_FUNCTIONS[tool_name]

    # **tool_args turns {"ticket_id": 20} into get_ticket(ticket_id=20).
    # If the model sent a missing or misspelled argument name, Python raises
    # a TypeError, which we turn into an error message for the model.
    try:
        return tool_function(**tool_args)
    except TypeError as error:
        return f"Error: wrong arguments for {tool_name}: {error}"


def get_call_tokens(response):
    """Return how many tokens one model call used.

    total_tokens = prompt_tokens (the whole history we sent)
                 + completion_tokens (the model's reply).
    """
    if response.usage is None:
        return 0
    return response.usage.total_tokens


def format_tool_call(tool_call):
    """Turn a tool call into readable text for the log, e.g. get_ticket({"ticket_id":20})."""
    return f"{tool_call.function.name}({tool_call.function.arguments})"


def make_assistant_message(reply_text, tool_calls):
    """Turn the model's reply into a plain dictionary for the message history.

    We copy the fields we need instead of storing the SDK's object, so every
    message in the history is a simple dictionary we can print or save.
    """
    assistant_message = {"role": "assistant", "content": reply_text}

    if len(tool_calls) > 0:
        assistant_message["tool_calls"] = []
        for tool_call in tool_calls:
            assistant_message["tool_calls"].append({
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments,
                },
            })

    return assistant_message


def make_result(stop_reason, final_text, iterations, total_tokens, messages):
    """Collect everything about how a run ended into one dictionary."""
    return {
        "stop_reason": stop_reason,  # why the loop stopped
        "final_text": final_text,  # the model's answer, or None if it never gave one
        "iterations": iterations,  # how many times we called the model
        "total_tokens": total_tokens,  # tokens used, added up over all calls
        "messages": messages,  # the complete history of the run
    }


def run_agent(client, goal, model, system_prompt, max_iterations, max_total_tokens):
    """Run the agent loop on one goal and return a result dictionary."""

    # The ONE message list for the whole run. It starts with the system
    # prompt and the user's goal. Every model reply and every tool result is
    # appended to this same list, and the complete list is sent on every call.
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": goal},
    ]

    total_tokens = 0

    for iteration in range(1, max_iterations + 1):

        # Step 1: send the complete message history to the model.
        # tools=TOOL_DECLARATIONS shows the model which tools it may ASK for.
        # Groq never runs a tool itself; it only returns the request to us.
        messages_sent = len(messages)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOL_DECLARATIONS,
        )

        # Step 2: add the tokens this call used to the running total.
        call_tokens = get_call_tokens(response)
        total_tokens = total_tokens + call_tokens
        print(
            f"[iter {iteration}] messages={messages_sent} "
            f"call_tokens={call_tokens} total_tokens={total_tokens}"
        )

        # Step 3: read the reply, and stop if it contains nothing usable.
        if not response.choices:
            print(f"[iter {iteration}] empty response")
            return make_result("empty_response", None, iteration, total_tokens, messages)

        model_reply = response.choices[0].message
        reply_text = model_reply.content  # None when the model only asks for tools
        tool_calls = model_reply.tool_calls  # None when the model asks for no tools
        if tool_calls is None:
            tool_calls = []

        if len(tool_calls) == 0 and not reply_text:
            print(f"[iter {iteration}] empty response")
            return make_result("empty_response", None, iteration, total_tokens, messages)

        # Step 4: add the model's reply to the history.
        messages.append(make_assistant_message(reply_text, tool_calls))

        # Step 5: no tool requested means the model has given its final answer.
        if len(tool_calls) == 0:
            print(f"[iter {iteration}] final answer")
            return make_result("final_answer", reply_text, iteration, total_tokens, messages)

        for tool_call in tool_calls:
            print(f"[iter {iteration}] tool requested: {format_tool_call(tool_call)}")

        # Step 6: stop if the token budget is used up.
        # This check comes after the final-answer check, so a finished answer is
        # kept even if it went over budget, and before running the tools,
        # because no further model call would read their results.
        if total_tokens >= max_total_tokens:
            print(f"[iter {iteration}] token budget of {max_total_tokens} reached")
            return make_result("token_budget", None, iteration, total_tokens, messages)

        # Step 7: run each requested tool ourselves, and add each result to the
        # history as its own "tool" message. tool_call_id tells the model which
        # of its requests this result answers.
        for tool_call in tool_calls:
            result = run_tool(tool_call.function.name, tool_call.function.arguments)
            tool_message = {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            }
            messages.append(tool_message)

        # The loop now goes round, and the model is called again with everything.

    # The for loop finished without a final answer: the iteration cap was reached.
    print(f"Iteration cap of {max_iterations} reached")
    return make_result("max_iterations", None, max_iterations, total_tokens, messages)
