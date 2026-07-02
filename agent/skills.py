"""Loads skills and exposes them to the model.

A skill here is the same idea as in the determinism memo: a folder with a
``SKILL.md`` contract plus the code that executes the work. This module reads the
contract into the system prompt (so the model knows when and how to use the
skill and the guardrails) and registers the skill's tools with their handlers.

The tools that touch money are deliberately narrow:

  * ``lookup_account`` and ``get_transfer_quote`` are read-only.
  * ``initiate_transfer`` *starts* the durable workflow but cannot complete a
    transfer — completion requires a separate human approval relayed by the API,
    not a tool the model can call.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from skills.money_transfer import client as transfer
from skills.money_transfer.shared import TransferRequest

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def _dollars_to_cents(amount: float) -> int:
    return int(round(amount * 100))


# ---- SKILL.md loading ------------------------------------------------------


@dataclass
class SkillDoc:
    name: str
    description: str
    body: str


def _parse_skill_md(text: str) -> SkillDoc:
    """Minimal front-matter parse: a `--- ... ---` block with name/description."""
    name, description, body = "", "", text
    if text.startswith("---"):
        _, _, rest = text.partition("---")
        front, _, body = rest.partition("---")
        for line in front.splitlines():
            key, sep, value = line.partition(":")
            if not sep:
                continue
            key, value = key.strip(), value.strip()
            if key == "name":
                name = value
            elif key == "description":
                description = value
    return SkillDoc(name=name, description=description, body=body.strip())


def load_skill_docs() -> list[SkillDoc]:
    docs: list[SkillDoc] = []
    for skill_md in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        docs.append(_parse_skill_md(skill_md.read_text()))
    return docs


# ---- Tool schemas (Anthropic tool-use format) ------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "lookup_account",
        "description": (
            "Read an account's balance and status. Read-only; never moves money. "
            "Use to answer questions like whether an account exists or has enough "
            "funds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "string",
                    "description": "Account number, e.g. '85-150'.",
                }
            },
            "required": ["account_id"],
        },
    },
    {
        "name": "get_transfer_quote",
        "description": (
            "Validate and price a transfer without moving money. Returns the fee, "
            "total debit, and any problems (unknown account, insufficient funds, "
            "frozen account, over the limit)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_account": {"type": "string"},
                "target_account": {"type": "string"},
                "amount_dollars": {"type": "number", "description": "Amount in dollars."},
            },
            "required": ["source_account", "target_account", "amount_dollars"],
        },
    },
    {
        "name": "initiate_transfer",
        "description": (
            "Start a money transfer. This begins the durable workflow and returns "
            "a plan that is WAITING for the customer's explicit approval. It does "
            "NOT move money and does NOT complete the transfer. Only call this once "
            "the source account, destination account, and amount are all known and "
            "the customer wants to proceed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_account": {"type": "string"},
                "target_account": {"type": "string"},
                "amount_dollars": {"type": "number", "description": "Amount in dollars."},
                "note": {"type": "string", "description": "Optional memo for the transfer."},
            },
            "required": ["source_account", "target_account", "amount_dollars"],
        },
    },
    {
        "name": "cancel_transfer",
        "description": (
            "Cancel the transfer that is currently awaiting the customer's "
            "approval — call this when the customer says to cancel, decline, stop, "
            "or not go ahead with it. It declines the pending transfer; NO money "
            "moves. It can only decline, never approve or complete a transfer. "
            "Only meaningful while a transfer is awaiting approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Optional reason the customer gave for cancelling.",
                }
            },
        },
    },
]


# ---- Session + dispatch ----------------------------------------------------


@dataclass
class Session:
    """Per-conversation state held by the API."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    pending_reference_id: str | None = None


@dataclass
class ToolOutcome:
    """What a tool call produced: a result for the model and an optional UI event."""

    result: dict[str, Any]
    ui_event: dict[str, Any] | None = None


def _plan_to_dollars(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        "source_account": plan["source_account"],
        "target_account": plan["target_account"],
        "amount_dollars": round(plan["amount_cents"] / 100, 2),
        "fee_dollars": round(plan["fee_cents"] / 100, 2),
        "total_debit_dollars": round(plan["total_debit_cents"] / 100, 2),
        "valid": plan["valid"],
        "problems": plan["problems"],
    }


async def dispatch_tool(
    name: str, tool_input: dict[str, Any], session: Session
) -> ToolOutcome:
    if name == "lookup_account":
        return ToolOutcome(result=transfer.lookup_account(tool_input["account_id"]))

    if name == "get_transfer_quote":
        cents = _dollars_to_cents(tool_input["amount_dollars"])
        return ToolOutcome(
            result=transfer.quote_transfer(
                tool_input["source_account"], tool_input["target_account"], cents
            )
        )

    if name == "initiate_transfer":
        # Enforce one-transfer-at-a-time here, deterministically, instead of
        # relying on the model to judge it from the chat history (it tends to
        # think a resolved card is still pending). Check the pending transfer's
        # real status and clear it if it has already settled.
        pending = session.pending_reference_id
        if pending:
            try:
                pending_status = (await transfer.get_state(pending)).get("status")
            except Exception:  # noqa: BLE001 - workflow gone: treat as settled
                pending_status = None
            if pending_status == "awaiting_approval":
                return ToolOutcome(
                    result={
                        "status": "already_pending",
                        "reference_id": pending,
                        "detail": (
                            f"A transfer (reference {pending}) is already awaiting "
                            f"the customer's approval. Ask them to approve or "
                            f"decline it, or call cancel_transfer, before starting "
                            f"a new one."
                        ),
                    }
                )
            if pending_status in {"validating", "executing", "compensating"}:
                return ToolOutcome(
                    result={
                        "status": "in_progress",
                        "reference_id": pending,
                        "detail": (
                            f"A transfer (reference {pending}) is still processing; "
                            f"wait for it to finish before starting another."
                        ),
                    }
                )
            # Terminal or unknown: the pending reference is stale — clear it.
            session.pending_reference_id = None

        reference_id = uuid.uuid4().hex[:12]
        request = TransferRequest(
            source_account=tool_input["source_account"],
            target_account=tool_input["target_account"],
            amount_cents=_dollars_to_cents(tool_input["amount_dollars"]),
            reference_id=reference_id,
            note=tool_input.get("note", ""),
        )
        started = await transfer.initiate_transfer(request)
        plan = _plan_to_dollars(started.get("plan"))

        result_for_model = {
            "workflow_id": started["workflow_id"],
            "reference_id": reference_id,
            "status": started["status"],
            "plan": plan,
        }
        if started.get("detail"):
            result_for_model["detail"] = started["detail"]

        ui_event = None
        if started["status"] == "awaiting_approval":
            session.pending_reference_id = reference_id
            ui_event = {
                "type": "transfer_plan",
                "workflow_id": started["workflow_id"],
                "reference_id": reference_id,
                "status": "awaiting_approval",
                "plan": plan,
            }
        return ToolOutcome(result=result_for_model, ui_event=ui_event)

    if name == "cancel_transfer":
        reference_id = session.pending_reference_id
        if not reference_id:
            return ToolOutcome(
                result={
                    "status": "nothing_to_cancel",
                    "detail": "There is no transfer awaiting approval to cancel.",
                }
            )
        # Declining moves no money, so the model may do this on the customer's
        # word — unlike approval, which requires the human card tap.
        outcome = await transfer.submit_decision(
            reference_id, approved=False, reason=tool_input.get("reason", "")
        )
        session.pending_reference_id = None
        result = outcome["result"]
        return ToolOutcome(
            result={
                "status": result.get("status", "declined"),
                "reference_id": reference_id,
                "detail": "The transfer was cancelled. No money moved.",
            },
            # Settle the card in the UI too, in case it's still on screen.
            ui_event={
                "type": "transfer_settled",
                "reference_id": reference_id,
                "result": result,
            },
        )

    return ToolOutcome(result={"error": f"Unknown tool {name!r}"})


def build_system_prompt() -> str:
    docs = load_skill_docs()
    skills_block = "\n\n".join(
        f"## Skill: {d.name}\n{d.description}\n\n{d.body}" for d in docs
    )
    return (
        "You are the money assistant for a personal banking app. You help "
        "customers understand and carry out money transfers in a friendly, plain, "
        "and careful way.\n\n"
        "You can read balances and price transfers yourself, but you never move "
        "money directly — a deterministic workflow does that, and a transfer only "
        "completes after the customer approves it on a confirmation card. Be warm "
        "and concise. Never state a balance, fee, or that money has moved unless a "
        "tool gave you that fact.\n\n"
        "To move money you MUST call the initiate_transfer tool — that is the only "
        "way a transfer begins, and it is what shows the customer their approval "
        "card. Never tell the customer a transfer was started, sent, or completed "
        "unless a tool result said so; if you have not called initiate_transfer, no "
        "transfer exists. When the customer wants to transfer and you know the "
        "source, destination, and amount, call initiate_transfer rather than "
        "describing what you would do.\n\n"
        "The customer's own accounts are 85-150 (their checking) and 43-812 (their "
        "savings); map 'checking' to 85-150 and 'savings' to 43-812 so a request "
        "like 'send $500 from checking to savings' is complete and can be started.\n\n"
        "You have the following skill available. Follow its contract exactly:\n\n"
        f"{skills_block}"
    )
