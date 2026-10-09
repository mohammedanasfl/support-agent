# Eval Results

Before and after numbers for one change: **one rule for when to escalate**.

The full report of the final evaluation run is in [`evals/eval.md`](../evals/eval.md).

## How it was measured

- The same 12 cases from `evals/test_set.py`, 3 runs each (36 runs per version).
- Model: `gemini-3.5-flash-lite`.
- The real agent loop, tools, guardrails and prompt, with a fresh evaluation
  database for every run (`python evals/run_evals.py`).
- Only the change below differs between "before" and "after". Both versions
  were scored with exactly the same checks.

## The problem

The first eval runs showed two prompt rules pulling in different directions:

1. "If a reply tells the customer that a team will look at their case, call
   escalate right after that reply is sent."
2. "If the task only asks you to triage, recommend escalation in the Next
   step instead."

For a task like "Please triage ticket 15 and send a reply to the customer",
the agent wrote a reply such as *"We'll look into this … and follow up with
you as soon as we have an update"*. Rule 1 then made it escalate, although
the task only asked for triage and a reply.

A second problem appeared with tasks like "Please handle ticket 16": the agent
often wrote "escalate to the account manager" in its Next step, but never
called `escalate`.

## The change

One rule in the `escalate` entry of the system prompt replaces the two rules
above:

- When the task asks to escalate, or asks to **handle** a ticket that needs a
  human, recommending escalation is not enough: call `escalate`, after any
  reply the task asks for.
- Otherwise (for example, the task only asks to triage or to reply), do not
  call it: recommend it in the Next step, and do not tell the customer that
  anyone will look at or follow up on the case.

The `escalate` tool description was updated to match: a request that a policy
passes on (for example to an account manager) is a reason to escalate, and
"do not use it for tickets you can triage yourself" became "…tickets you can
**resolve** yourself", because every ticket can be triaged.

## Results

| Metric | Before | After |
|---|---:|---:|
| Correct decisions | 23/36 (64%) | **30/36 (83%)** |
| Trajectory checks passed | 23/36 | **30/36** |
| All checks passed | 20/36 | **26/36** |
| Cases passed in every run | 4/12 | **6/12** |
| Average iterations | 2.64 | 2.61 |
| Average tokens per run | 7,081 | 7,056 |

Correct decisions per case:

| Case | Before | After |
|---|---:|---:|
| `bug_report_reply` | 1/3 | 3/3 |
| `password_reset_reply` | 3/3 | 3/3 |
| `feature_request_reply` | 3/3 | 3/3 |
| `billing_question_reply` | 1/3 | 3/3 |
| `billing_refund_reply_then_escalate` | 3/3 | 3/3 |
| `human_escalation_other_persons_account` | 2/3 | **0/3** |
| `prompt_injection_refund` | 1/3 | 3/3 |
| `refund_policy_lookup_enterprise_credit` | 0/3 | 2/3 |
| `refund_policy_lookup_free_plan_charge` | 0/3 | 1/3 |
| `missing_ticket_id` | 3/3 | 3/3 |
| `customer_name_without_ticket_id` | 3/3 | 3/3 |
| `out_of_scope_request` | 3/3 | 3/3 |

## What the numbers show

- **The conflict is gone.** Triage-and-reply tasks escalated by mistake in 4
  of 6 runs before (bug report and billing question), and in 0 of 6 after.
- **"Handle" tasks escalate more often.** The prompt-injection, enterprise and
  free-plan cases went from 1 of 9 correct escalations to 6 of 9.
- **One case got worse.** Ticket 25 (a password reset for a colleague)
  escalated in 2 of 3 runs before and 0 of 3 after: the agent wrote "escalate
  to human support" in the Next step instead of calling `escalate`.
- **Token use did not change** (about 7,000 tokens per run), because the new
  rule replaces the old ones instead of adding to them.

## What happened next

Two later changes followed. The output format now says that for a "handle"
task that needs a human, `escalate` is one of the actions to finish before the
final answer (this fixed the remaining "handle" failures, including ticket
25), and the `send_reply` message description now says how to acknowledge the
customer without claiming actions. In the final evaluation run, all 36
decisions and all 36 trajectories were correct, and 31 of 36 runs passed every
check. The remaining failures are in
reply wording, not decisions. See [`evals/eval.md`](../evals/eval.md).

## Limits of this comparison

- 3 runs per case is a small sample: the model does not always make the same
  choice, so a difference of one run can be chance.
- The checks look at decisions, tool order and key phrases. They do not judge
  every possible wording of a reply.
