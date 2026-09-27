# Vera Bot — magicpin AI Challenge

A production-ready implementation of the Vera WhatsApp merchant-AI assistant, built for the magicpin AI Challenge.

## Architecture

```
bot/
├── main.py           — FastAPI server (all 5 endpoints)
├── composer.py       — LLM + rule-based message composer
├── reply_handler.py  — Multi-turn conversation handler
├── bot.py            — Submission-format compose() shim
└── requirements.txt
```

## All 5 Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/context` | Receive category/merchant/customer/trigger context pushes |
| `POST` | `/v1/tick` | Periodic wake-up; bot decides proactive sends |
| `POST` | `/v1/reply` | Receive merchant/customer reply; return next action |
| `GET`  | `/v1/healthz` | Liveness probe with context count |
| `GET`  | `/v1/metadata` | Bot identity and approach |

## Design Approach

### Composer
1. **Trigger-kind dispatch**: Each `trigger.kind` maps to a specific composition rule that pulls the right fields from all 4 contexts.
2. **LLM override**: Tries Gemini 1.5 Flash (temperature=0 for determinism) first. Falls back to rule-based if no API key or quota exceeded.
3. **Context-anchored specificity**: Every message anchors on verifiable data — digest item titles/sources, CTR numbers, peer stats, offer prices, slot labels.
4. **Language awareness**: Auto-detects Hindi-English merchants and uses code-mix. Pure English for en-only merchants.

### Reply Handler
Multi-turn conversation with explicit detection for:
- **Auto-reply**: Canned WA Business patterns → try owner once → wait 4h → end (3rd time)
- **Opt-out / hostility**: Immediate graceful `end`
- **Intent transition**: "Yes/OK/Let's do it" → switch from qualifying to action immediately
- **Off-topic**: Politely decline, redirect back to main thread
- **Normal advance**: LLM reply → rule-based fallback

### Context Management
- Idempotent `(scope, context_id, version)` — higher version atomically replaces
- Suppression via `suppression_key` set — one message per trigger per session
- Anti-repetition — same body not sent twice in same conversation

## Quick Start

```bash
# Install
pip install -r requirements.txt

# Set API key (optional — falls back to rule-based)
export GOOGLE_API_KEY=your_gemini_key

# Run
uvicorn main:app --host 0.0.0.0 --port 8080

# Test
python test_bot.py
```

## Tradeoffs

- **Speed vs quality**: Rule-based composer ensures <1s response even without LLM quota. Gemini adds richer phrasing when available.
- **Determinism**: All LLM calls use temperature=0. Rule-based path is fully deterministic.
- **In-memory state**: Restarting the server clears state. For a production deployment, use Redis or SQLite.
- **Trigger expiry**: Bot respects `expires_at` strictly (ISO comparison). The judge simulator uses timestamps within trigger windows.

## What Additional Context Would Help Most

1. **Merchant's actual GBP URL** — to enable real profile-completion checks
2. **Real-time review feed** — to catch review themes as they emerge
3. **Booking calendar integration** — to offer real slots for recall/appointment triggers
4. **A/B message performance history** — to learn which copy hooks work per category
