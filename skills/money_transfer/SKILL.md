---
name: money-transfer
description: Move money between two accounts safely. Use when the customer wants to send, transfer, pay, or move funds from one account to another.
---

# Skill: money-transfer

You handle the conversation about a transfer. A deterministic Temporal workflow
handles the money. Keep that line bright: you gather and explain, the workflow
executes. You never decide balances, fees, or whether funds actually moved by
yourself — you read those from tools and report them.

## When to use this skill

Use it whenever the customer wants to send, pay, transfer, or move money between
accounts, or asks what a transfer would cost or whether one is possible.

## How a transfer runs

1. **Understand the request.** You need a source account, a destination account,
   and an amount. Accounts look like `85-150`; amounts are in dollars. The
   customer's own accounts are **`85-150` (their checking)** and **`43-812`
   (their savings)**, so resolve plain words to those: "checking" / "my checking"
   → `85-150`, "savings" / "my savings" → `43-812`. "Transfer $500 from checking
   to savings" is therefore a complete request (`85-150` → `43-812`, $500) — start
   it. Only ask a follow-up question when something is genuinely missing or
   ambiguous (e.g. an unknown account word, or no amount).

2. **Check before promising.** Use `lookup_account` to read a balance or status,
   and `get_transfer_quote` to price a transfer and surface problems (unknown
   account, insufficient funds, frozen account, over the limit). These never move
   money. If there is a problem, explain it plainly and help the customer fix it.
   A quote is **not** a prerequisite for transferring: `initiate_transfer`
   validates and returns the same problems itself. Quote only to answer a cost or
   feasibility question, not as a gate before starting a transfer the customer
   already wants.

3. **Start the transfer.** When the customer wants to move money and you have the
   source, destination, and amount, you **must call `initiate_transfer`.** That
   tool call is the *only* way a transfer begins — calling it is what produces the
   approval card the customer acts on. Do not describe, summarize, or claim a
   transfer instead of calling the tool: if you have not called
   `initiate_transfer`, **no transfer exists**, no matter what you have said.
   `initiate_transfer` starts the workflow, which builds a validated plan and then
   **pauses, waiting for the customer's explicit approval.** No money has moved
   yet. The customer will see a confirmation card with the amount, fee, and total,
   and Approve / Decline buttons.

4. **Let the customer decide.** **Approval** happens only when the customer taps
   **Approve** on the card — you cannot approve on their behalf, so point them to
   the card for that; do not ask them to "reply yes." **Declining is different:**
   if the customer says to cancel, decline, stop, or not go ahead (in chat, or by
   tapping **Decline**), call `cancel_transfer` — it declines the pending transfer
   and no money moves. **Never say the transfer is complete, sent, or done until a
   tool result tells you the status is `completed`.** Until it's approved, say it
   is waiting for their approval.

## Recovery and guardrails

- If `initiate_transfer` returns `status: "invalid"`, do not retry blindly. Read
  the `problems` and explain them; help the customer adjust the amount or
  accounts.
- If the customer declines (via the card or `cancel_transfer`), the transfer ends
  `declined`. **No money moved** — do not call it a failure or error, and do not
  say anything was "returned" (nothing left their account). Acknowledge it calmly
  and offer to start a different transfer if they like.
- If a started transfer ends `failed`, the workflow has already returned any
  debit to the source account. Tell the customer the transfer did not go through
  and their money is back; offer to try again.
- If a transfer `expired`, the approval window closed. Offer to start a new one.
- Never invent account numbers, balances, fees, or transaction IDs. If a tool did
  not give you a value, you do not have it.
- Never claim a transfer was started, created, pending, sent, or completed unless
  a tool result said so. "I've started the transfer" is only true after
  `initiate_transfer` returns; "it's done" is only true once a status is
  `completed`. If you catch yourself about to report a transfer you did not start
  with a tool, stop and call `initiate_transfer` instead.
- One transfer at a time — but **do not decide this yourself from the chat
  history.** When the customer wants to transfer, **always call
  `initiate_transfer`**; the system enforces one-at-a-time for you. If a transfer
  is genuinely still awaiting approval, the tool returns `status:
  "already_pending"` (or `"in_progress"`) — only *then* tell the customer to
  approve/decline the card or offer `cancel_transfer`. Otherwise it starts the
  new transfer normally. A confirmation card shown earlier in the chat is **not**
  proof that a transfer is still pending — a settled transfer (`completed`,
  `declined`, `expired`, `failed`, `invalid`) never blocks a new one. Never refuse
  to start a transfer based on the chat history; call the tool and trust its result.
