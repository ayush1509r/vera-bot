"""
test_bot.py — Integration tests for all 5 bot endpoints.
"""
import json
import sys
import urllib.request
import urllib.error

BASE = "http://localhost:8080"


def request(method, path, body=None):
    url = BASE + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_healthz():
    status, body = request("GET", "/v1/healthz")
    assert status == 200, f"healthz failed: {status}"
    assert body["status"] == "ok"
    print(f"  [PASS] GET /v1/healthz — {body}")


def test_metadata():
    status, body = request("GET", "/v1/metadata")
    assert status == 200
    assert "team_name" in body
    print(f"  [PASS] GET /v1/metadata — team: {body['team_name']}, model: {body['model']}")


def test_context_push():
    with open("../dataset/categories/dentists.json", encoding="utf-8") as f:
        cat = json.load(f)

    # Push category
    status, body = request("POST", "/v1/context", {
        "scope": "category",
        "context_id": "dentists",
        "version": 1,
        "delivered_at": "2026-09-27T07:00:00Z",
        "payload": cat,
    })
    assert status == 200 and body["accepted"] is True, f"category push failed: {body}"
    print(f"  [PASS] POST /v1/context (category) — ack: {body['ack_id']}")

    # Idempotency: same version → 409-style stale
    status2, body2 = request("POST", "/v1/context", {
        "scope": "category",
        "context_id": "dentists",
        "version": 1,
        "delivered_at": "2026-09-27T07:01:00Z",
        "payload": cat,
    })
    assert body2["accepted"] is False and body2["reason"] == "stale_version"
    print(f"  [PASS] idempotency check — {body2}")

    # Version bump
    status3, body3 = request("POST", "/v1/context", {
        "scope": "category",
        "context_id": "dentists",
        "version": 2,
        "delivered_at": "2026-09-27T07:02:00Z",
        "payload": cat,
    })
    assert body3["accepted"] is True
    print(f"  [PASS] version bump accepted — {body3['ack_id']}")

    # Invalid scope
    status4, body4 = request("POST", "/v1/context", {
        "scope": "INVALID_SCOPE",
        "context_id": "x",
        "version": 1,
        "delivered_at": "2026-09-27T07:00:00Z",
        "payload": {},
    })
    assert status4 == 422, f"Expected 422 for invalid scope, got {status4}"
    print(f"  [PASS] invalid scope returns 422")


def test_tick_with_context():
    """Push category + merchant + trigger, then tick."""
    with open("../dataset/categories/dentists.json", encoding="utf-8") as f:
        cat = json.load(f)
    with open("../dataset/merchants_seed.json", encoding="utf-8") as f:
        merchants_raw = json.load(f)
    with open("../dataset/triggers_seed.json", encoding="utf-8") as f:
        triggers_raw = json.load(f)

    merchant = merchants_raw["merchants"][0]  # Dr. Meera
    trigger = triggers_raw["triggers"][0]     # research_digest_dentists

    # Push category (already pushed above, but use fresh version 3)
    request("POST", "/v1/context", {
        "scope": "category", "context_id": "dentists", "version": 3,
        "delivered_at": "2026-09-27T07:10:00Z", "payload": cat,
    })

    # Push merchant
    status, body = request("POST", "/v1/context", {
        "scope": "merchant",
        "context_id": merchant["merchant_id"],
        "version": 1,
        "delivered_at": "2026-09-27T07:10:01Z",
        "payload": merchant,
    })
    assert body["accepted"] is True, f"merchant push failed: {body}"
    print(f"  [PASS] POST /v1/context (merchant) — {body['ack_id']}")

    # Push trigger
    status, body = request("POST", "/v1/context", {
        "scope": "trigger",
        "context_id": trigger["id"],
        "version": 1,
        "delivered_at": "2026-09-27T07:10:02Z",
        "payload": trigger,
    })
    assert body["accepted"] is True
    print(f"  [PASS] POST /v1/context (trigger) — {body['ack_id']}")

    # Tick
    status, body = request("POST", "/v1/tick", {
        "now": "2026-09-27T07:15:00Z",
        "available_triggers": [trigger["id"]],
    })
    assert status == 200
    assert "actions" in body
    print(f"  [PASS] POST /v1/tick — {len(body['actions'])} action(s)")
    if body["actions"]:
        a = body["actions"][0]
        print(f"         conv_id: {a['conversation_id']}")
        print(f"         body: {a['body'][:120]}...")
        print(f"         cta: {a['cta']} | send_as: {a['send_as']}")
        return a["conversation_id"]
    return None


def test_reply(conv_id):
    if not conv_id:
        print("  [SKIP] /v1/reply — no conversation_id from tick")
        return

    # Normal engaged reply
    status, body = request("POST", "/v1/reply", {
        "conversation_id": conv_id,
        "merchant_id": "m_001_drmeera_dentist_delhi",
        "customer_id": None,
        "from_role": "merchant",
        "message": "Yes, please send the abstract",
        "received_at": "2026-09-27T07:20:00Z",
        "turn_number": 2,
    })
    assert status == 200 and body["action"] in ("send", "wait", "end")
    print(f"  [PASS] POST /v1/reply (engaged) — action={body['action']}")

    # Auto-reply detection
    status2, body2 = request("POST", "/v1/reply", {
        "conversation_id": conv_id + "_autoreply_test",
        "merchant_id": "m_001_drmeera_dentist_delhi",
        "from_role": "merchant",
        "message": "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly.",
        "received_at": "2026-09-27T07:21:00Z",
        "turn_number": 2,
    })
    assert status2 == 200
    # Should detect auto-reply: either send (first try) or wait
    print(f"  [PASS] POST /v1/reply (auto-reply) — action={body2['action']}, rationale: {body2.get('rationale','')[:60]}")

    # Opt-out
    status3, body3 = request("POST", "/v1/reply", {
        "conversation_id": conv_id + "_optout_test",
        "merchant_id": "m_001_drmeera_dentist_delhi",
        "from_role": "merchant",
        "message": "Not interested. Stop messaging me.",
        "received_at": "2026-09-27T07:22:00Z",
        "turn_number": 2,
    })
    assert status3 == 200 and body3["action"] == "end"
    print(f"  [PASS] POST /v1/reply (opt-out) — action=end ✓")

    # Off-topic
    status4, body4 = request("POST", "/v1/reply", {
        "conversation_id": conv_id + "_offtopic_test",
        "merchant_id": "m_001_drmeera_dentist_delhi",
        "from_role": "merchant",
        "message": "Can you also help me file my GST return?",
        "received_at": "2026-09-27T07:23:00Z",
        "turn_number": 3,
    })
    assert status4 == 200 and body4["action"] == "send"
    print(f"  [PASS] POST /v1/reply (off-topic) — action=send, body: {body4.get('body','')[:80]}...")


def test_customer_trigger():
    """Test customer-scoped recall_due trigger."""
    with open("../dataset/categories/dentists.json", encoding="utf-8") as f:
        cat = json.load(f)
    with open("../dataset/customers_seed.json", encoding="utf-8") as f:
        customers_raw = json.load(f)
    with open("../dataset/triggers_seed.json", encoding="utf-8") as f:
        triggers_raw = json.load(f)

    customer = customers_raw["customers"][0]  # Priya
    recall_trigger = next(t for t in triggers_raw["triggers"] if t["kind"] == "recall_due")

    # Push customer context
    request("POST", "/v1/context", {
        "scope": "customer",
        "context_id": customer["customer_id"],
        "version": 1,
        "delivered_at": "2026-09-27T07:30:00Z",
        "payload": customer,
    })

    # Push recall trigger
    request("POST", "/v1/context", {
        "scope": "trigger",
        "context_id": recall_trigger["id"],
        "version": 1,
        "delivered_at": "2026-09-27T07:30:01Z",
        "payload": recall_trigger,
    })

    # Tick with recall trigger
    status, body = request("POST", "/v1/tick", {
        "now": "2026-09-27T07:35:00Z",
        "available_triggers": [recall_trigger["id"]],
    })
    assert status == 200

    if body["actions"]:
        a = body["actions"][0]
        assert a["send_as"] == "merchant_on_behalf", f"Expected merchant_on_behalf, got {a['send_as']}"
        assert a["customer_id"] == customer["customer_id"]
        print(f"  [PASS] Customer recall trigger — send_as={a['send_as']}, customer_id={a['customer_id']}")
        print(f"         body: {a['body'][:120]}...")
    else:
        print("  [WARN] Customer recall trigger returned no actions (may be suppressed from earlier run)")


def test_healthz_after_warmup():
    """Verify context counts are reflected correctly."""
    status, body = request("GET", "/v1/healthz")
    assert status == 200
    counts = body["contexts_loaded"]
    assert counts["category"] >= 1
    assert counts["merchant"] >= 1
    assert counts["trigger"] >= 1
    print(f"  [PASS] GET /v1/healthz (post-warmup) — contexts: {counts}")


if __name__ == "__main__":
    print("\n🧪 Running Vera Bot Integration Tests\n")
    try:
        print("1. Healthz check")
        test_healthz()

        print("\n2. Metadata check")
        test_metadata()

        print("\n3. Context push tests")
        test_context_push()

        print("\n4. Tick with full context")
        conv_id = test_tick_with_context()

        print("\n5. Reply handling")
        test_reply(conv_id)

        print("\n6. Customer-scoped trigger")
        test_customer_trigger()

        print("\n7. Healthz post-warmup")
        test_healthz_after_warmup()

        print("\n✅ All tests passed!")
    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 Unexpected error: {e}")
        raise
