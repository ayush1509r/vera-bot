from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

from composer import compose
from reply_handler import compose_reply

app = FastAPI(
    title="Vera Bot",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

START_TIME = time.time()

contexts: dict[tuple[str, str], dict] = {}
conversations: dict[str, list[dict]] = {}
conv_meta: dict[str, dict] = {}
sent_suppressions: set[str] = set()
last_sent: dict[str, str] = {}

VALID_SCOPES = {"category", "merchant", "customer", "trigger"}


class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str = ""

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, v: str) -> str:
        if v not in VALID_SCOPES:
            raise ValueError(f"Invalid scope '{v}'. Must be one of {VALID_SCOPES}.")
        return v


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str = "merchant"
    message: str
    received_at: str = ""
    turn_number: int = 1


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ack_id(context_id, version):
    return f"ack_{context_id}_v{version}"


def _scope_counts():
    counts: dict[str, int] = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _) in contexts:
        if scope in counts:
            counts[scope] += 1
    return counts


def _conv_id_fresh(merchant_id, trigger_id):
    base = f"conv_{merchant_id}_{trigger_id}"
    return base[:120].replace(" ", "_")


@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": _scope_counts(),
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Vera-Builder",
        "team_members": ["Raj"],
        "model": "gemini-1.5-flash (fallback: rule-based)",
        "approach": (
            "Trigger-kind dispatch with per-kind rule-based composer and Gemini LLM override. "
            "Multi-turn reply handler with auto-reply, opt-out, and intent-transition detection."
        ),
        "contact_email": os.getenv("CONTACT_EMAIL", "ayush1509r@example.com"),
        "version": "1.0.0",
        "submitted_at": "2026-09-27T07:12:00Z",
    }


@app.post("/v1/context", status_code=200)
async def push_context(body: ContextBody):
    key = (body.scope, body.context_id)
    current = contexts.get(key)

    if current is not None and current["version"] >= body.version:
        return {
            "accepted": False,
            "reason": "stale_version",
            "current_version": current["version"],
        }

    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": _ack_id(body.context_id, body.version),
        "stored_at": _now_iso(),
    }


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions: list[dict] = []

    for trg_id in body.available_triggers:
        trg_entry = contexts.get(("trigger", trg_id))
        if not trg_entry:
            continue
        trg = trg_entry["payload"]

        expires_at = trg.get("expires_at", "")
        if expires_at and expires_at < body.now:
            continue

        suppression_key = trg.get("suppression_key", "")
        if suppression_key and suppression_key in sent_suppressions:
            continue

        merchant_id = trg.get("merchant_id")
        customer_id = trg.get("customer_id")

        merchant_entry = contexts.get(("merchant", merchant_id))
        if not merchant_entry:
            continue
        merchant = merchant_entry["payload"]

        cat_slug = merchant.get("category_slug", "")
        category_entry = contexts.get(("category", cat_slug))
        if not category_entry:
            continue
        category = category_entry["payload"]

        customer = None
        if customer_id:
            cust_entry = contexts.get(("customer", customer_id))
            if cust_entry:
                customer = cust_entry["payload"]

        conv_id = _conv_id_fresh(merchant_id, trg_id)
        meta = conv_meta.get(conv_id, {})
        if meta.get("ended"):
            continue

        try:
            composed = compose(category, merchant, trg, customer)
        except Exception as exc:
            composed = {
                "body": f"Hi, Vera here — something important for you. Reply YES.",
                "cta": "binary_yes_no",
                "send_as": "vera",
                "suppression_key": suppression_key,
                "rationale": f"Composition error: {exc}",
            }

        if not composed.get("body"):
            continue

        if last_sent.get(conv_id) == composed["body"]:
            continue

        last_sent[conv_id] = composed["body"]
        if suppression_key:
            sent_suppressions.add(suppression_key)

        conversations.setdefault(conv_id, []).append(
            {"from": "vera", "msg": composed["body"]}
        )
        conv_meta[conv_id] = {
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "trigger_id": trg_id,
            "ended": False,
        }

        template_name = f"vera_{trg.get('kind', 'generic')}_v1"
        merchant_name = (merchant.get("identity") or {}).get("name", merchant_id)

        action_item = {
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed.get("send_as", "vera"),
            "trigger_id": trg_id,
            "template_name": template_name,
            "template_params": [merchant_name, composed["body"][:80]],
            "body": composed["body"],
            "cta": composed.get("cta", "open_ended"),
            "suppression_key": suppression_key,
            "rationale": composed.get("rationale", ""),
        }
        actions.append(action_item)

        if len(actions) >= 20:
            break

    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv_id = body.conversation_id
    history = conversations.setdefault(conv_id, [])
    meta = conv_meta.get(conv_id, {})

    if meta.get("ended"):
        return {
            "action": "end",
            "rationale": "Conversation already closed by previous end signal.",
        }

    merchant_id = body.merchant_id or meta.get("merchant_id", "")
    customer_id = body.customer_id or meta.get("customer_id")

    history.append({"from": body.from_role, "msg": body.message})

    result = compose_reply(
        conversation_id=conv_id,
        merchant_id=merchant_id,
        customer_id=customer_id,
        message=body.message,
        turn_number=body.turn_number,
        history=history,
        contexts=contexts,
    )

    if result.get("action") == "send":
        bot_body = result.get("body", "")
        if last_sent.get(conv_id) == bot_body:
            result["body"] = bot_body + " (follow-up)"
        last_sent[conv_id] = result.get("body", "")
        history.append({"from": "vera", "msg": result.get("body", "")})

    elif result.get("action") == "end":
        if conv_id in conv_meta:
            conv_meta[conv_id]["ended"] = True

    return result
