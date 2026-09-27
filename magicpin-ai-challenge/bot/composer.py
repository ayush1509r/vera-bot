from __future__ import annotations

import json
import os
import re
from typing import Any

_gemini_model = None

def _get_gemini():
    global _gemini_model
    if _gemini_model is None:
        import google.generativeai as genai
        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not api_key:
            return None
        genai.configure(api_key=api_key)
        _gemini_model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            generation_config={
                "temperature": 0,
                "top_p": 1,
                "max_output_tokens": 600,
            },
        )
    return _gemini_model


SYSTEM_PROMPT = """You are Vera, magicpin's merchant AI assistant running on WhatsApp.

COMPOSITION RULES (strictly enforce):
1. Voice — peer/colleague tone. Clinical vocabulary allowed for dentists/pharmacies. No promotional hype.
2. Hindi-English code-mix preferred for merchants with languages containing "hi". Pure English for English-only merchants.
3. Body — concise, WhatsApp-readable. No bullet-lists of 5+ items. No long preambles.
4. Specificity — anchor on verifiable numbers from context (CTR, review count, price, date). Never invent.
5. CTA — exactly ONE. Binary (Reply YES / STOP) for action triggers. Open-ended question for info triggers. None for pure information.
6. Taboos — no: "guaranteed", "100% safe", "miracle", "best in city", "Flat X% off" (use service+price format instead), multiple CTAs, URLs, re-introducing yourself after turn 1.
7. send_as — "vera" for merchant-facing. "merchant_on_behalf" for customer-facing triggers.
8. Output ONLY valid JSON — no prose, no markdown fences:
   {"body":"...","cta":"open_ended|binary_yes_no|binary_confirm_cancel|multi_choice_slot|none","send_as":"vera|merchant_on_behalf","suppression_key":"...","rationale":"..."}

COMPULSION LEVERS (use >= 1):
- Specificity/verifiability (concrete number, date, source)
- Loss aversion ("you're missing X")
- Social proof ("3 dentists in your locality did Y")
- Effort externalization ("I've drafted X — just say go")
- Curiosity ("want to see who?")
- Reciprocity ("I noticed Y, thought you'd want to know")
- Single binary commitment (YES / STOP)
"""


def _build_compose_prompt(category, merchant, trigger, customer):
    parts = [
        "Compose a WhatsApp message for the following context.\n",
        f"=== CATEGORY CONTEXT ===\n{json.dumps(category, ensure_ascii=False, indent=2)}\n",
        f"=== MERCHANT CONTEXT ===\n{json.dumps(merchant, ensure_ascii=False, indent=2)}\n",
        f"=== TRIGGER CONTEXT ===\n{json.dumps(trigger, ensure_ascii=False, indent=2)}\n",
    ]
    if customer:
        parts.append(f"=== CUSTOMER CONTEXT ===\n{json.dumps(customer, ensure_ascii=False, indent=2)}\n")
    parts.append("\nReturn ONLY valid JSON with keys: body, cta, send_as, suppression_key, rationale.")
    return "".join(parts)


def _slot_label(slots):
    if not slots:
        return "Let us know a convenient time."
    labels = [s.get("label", s.get("iso", "")) for s in slots[:2]]
    return f"Available slots: {' or '.join(labels)}."


def _safe(d, *keys, default=""):
    v = d
    for k in keys:
        if not isinstance(v, dict):
            return default
        v = v.get(k, default)
    return v if v is not None else default


def _fallback_compose(category, merchant, trigger, customer):
    kind = trigger.get("kind", "")
    merchant_name = _safe(merchant, "identity", "name")
    owner_name = _safe(merchant, "identity", "owner_first_name") or merchant_name
    cat_slug = merchant.get("category_slug", category.get("slug", ""))
    languages = _safe(merchant, "identity", "languages") or ["en"]
    hi_mix = "hi" in languages
    send_as = "vera"
    trg_payload = trigger.get("payload", {})
    cust_name = _safe(customer or {}, "identity", "name") if customer else ""
    digest_map = {d["id"]: d for d in category.get("digest", []) if isinstance(d, dict)}
    active_offers = [o for o in merchant.get("offers", []) if o.get("status") == "active"]
    our_offer = active_offers[0]["title"] if active_offers else "our services"
    suppression_key = trigger.get("suppression_key", f"{kind}:{merchant.get('merchant_id', '')}:auto")
    cta = "binary_yes_no"

    if kind == "research_digest":
        top_id = trg_payload.get("top_item_id", "")
        item = digest_map.get(top_id, {})
        if hi_mix:
            body = (
                f"{owner_name}, JIDA ka naaya issue aaya — "
                f"\"{item.get('title', 'new research')}\" ({item.get('source', '')}). "
                f"Aapke high-risk adult patients ke liye relevant hai. "
                f"Abstract pull kar dun + ek patient-ed WhatsApp draft karoon? — Reply YES"
            )
        else:
            body = (
                f"{owner_name}, {item.get('title', 'New research item')} "
                f"({item.get('source', '')}). "
                f"Relevant to your {item.get('patient_segment','patients').replace('_',' ')}. "
                f"Want me to pull the abstract + draft a patient-ed post? — Reply YES"
            )
        cta = "open_ended"

    elif kind == "regulation_change":
        top_id = trg_payload.get("top_item_id", "")
        item = digest_map.get(top_id, {})
        deadline = trg_payload.get("deadline_iso", "soon")[:10] if trg_payload.get("deadline_iso") else "soon"
        body = (
            f"{owner_name}, compliance update: {item.get('title', 'regulation change')} — deadline {deadline}. "
            f"{item.get('actionable', '')} Want a checklist to verify your setup? Reply YES."
        )

    elif kind == "perf_dip":
        metric = trg_payload.get("metric", "calls")
        delta = abs(int(trg_payload.get("delta_pct", 0) * 100))
        baseline = trg_payload.get("vs_baseline", "")
        if hi_mix:
            body = (
                f"{owner_name}, aapke {metric} {delta}% gire hain last week vs baseline {baseline}. "
                f"2 quick fixes suggest karoon? Reply YES."
            )
        else:
            body = (
                f"{owner_name}, your {metric} dropped {delta}% vs last week (baseline: {baseline}). "
                f"Want 2 quick fixes? Reply YES."
            )

    elif kind == "perf_spike":
        metric = trg_payload.get("metric", "calls")
        delta = int(trg_payload.get("delta_pct", 0) * 100)
        driver = trg_payload.get("likely_driver", "")
        body = (
            f"{owner_name}, {metric} up {delta}% this week"
            + (f" — likely driven by '{driver}'" if driver else "") +
            ". Want me to lock in the momentum with a Google post? Reply YES."
        )

    elif kind == "recall_due":
        service = trg_payload.get("service_due", "6_month_cleaning").replace("_", " ")
        slots = trg_payload.get("available_slots", [])
        months_since = "6"
        if customer:
            lang = _safe(customer, "identity", "language_pref")
            if "hi" in lang:
                slots_text = f"Slots available: {', '.join(s.get('label','') for s in slots[:2])}."
                body = (
                    f"Hi {cust_name}, Dr. {owner_name} clinic se Aapki last visit ke {months_since} mahine ho gaye — "
                    f"{service} recall due hai. {slots_text} Reply 1 ya 2 for slot, ya apna time batayein."
                )
            else:
                slots_text = _slot_label(slots)
                body = (
                    f"Hi {cust_name}, {merchant_name} here. It's been {months_since} months since your last visit — "
                    f"your {service} is due. {slots_text} Reply 1 for first slot, 2 for second."
                )
        else:
            body = f"Recall due for a patient — {service}. Check slots and reach out."
        send_as = "merchant_on_behalf"
        cta = "multi_choice_slot"

    elif kind == "festival_upcoming":
        festival = trg_payload.get("festival", "festival")
        days_until = trg_payload.get("days_until", "")
        if hi_mix:
            body = (
                f"{owner_name}, {festival} is {days_until} days away. "
                f"I've drafted a {cat_slug}-specific GBP post + WhatsApp template. Review karein? Reply YES."
            )
        else:
            body = (
                f"{owner_name}, {festival} in {days_until} days — drafted a topical {cat_slug} offer + GBP post. Review? Reply YES."
            )

    elif kind == "competitor_opened":
        comp = trg_payload.get("competitor_name", "a new competitor")
        dist = trg_payload.get("distance_km", "")
        their = trg_payload.get("their_offer", "competing offer")
        body = (
            f"{owner_name}, '{comp}' opened {dist}km away with '{their}'. "
            f"Your active: '{our_offer}'. Want me to refresh your GBP positioning? Reply YES."
        )

    elif kind == "milestone_reached":
        val_now = trg_payload.get("value_now", 0)
        milestone = trg_payload.get("milestone_value", 150)
        gap = milestone - val_now
        body = (
            f"{owner_name}, you're just {gap} reviews away from {milestone} on Google — "
            f"hitting that milestone typically lifts CTR by 12-18%. "
            f"Want me to draft a review-ask WhatsApp for your last 20 visitors? Reply YES."
        )

    elif kind == "dormant_with_vera":
        days_dormant = trg_payload.get("days_since_last_merchant_message", 14)
        if hi_mix:
            body = (
                f"{owner_name}, {days_dormant} dino se baat nahi hui. Quick question — "
                f"is waqt sabse bada challenge kya hai patient footfall mein?"
            )
        else:
            body = (
                f"{owner_name}, haven't heard from you in {days_dormant} days. "
                f"Quick question — what's the one thing slowing footfall right now?"
            )
        cta = "open_ended"

    elif kind == "review_theme_emerged":
        theme = trg_payload.get("theme", "service quality").replace("_", " ")
        occurrences = trg_payload.get("occurrences_30d", 0)
        trend = trg_payload.get("trend", "")
        quote = trg_payload.get("common_quote", "")
        body = (
            f"{owner_name}, {occurrences} reviews this month mention '{theme}' "
            + (f"(e.g. \"{quote[:60]}\")" if quote else "") +
            (f" — trend: {trend}." if trend else ".") +
            " Want me to draft a reply template + a GBP post addressing this? Reply YES."
        )

    elif kind == "winback_eligible":
        days_since = trg_payload.get("days_since_expiry", 0)
        lapsed = trg_payload.get("lapsed_customers_added_since_expiry", 0)
        body = (
            f"{owner_name}, {days_since}d since your plan expired — {lapsed} customers added but not seeing your GBP. "
            f"Restart today? Reply YES to see reactivation offer."
        )

    elif kind in ("customer_lapsed_hard", "customer_lapsed_soft"):
        services = _safe(customer or {}, "relationship", "services_received") or []
        last_svc = services[-1] if services and isinstance(services, list) else "session"
        body = (
            f"Hi {cust_name}, {merchant_name} here. "
            f"It's been a while since your last {last_svc} — "
            f"when you're ready to restart, we have slots available. Reply YES for options."
        )
        send_as = "merchant_on_behalf"

    elif kind == "renewal_due":
        plan = trg_payload.get("plan", "Pro")
        days_rem = trg_payload.get("days_remaining", 0)
        amount = trg_payload.get("renewal_amount", "")
        body = (
            f"{owner_name}, your {plan} plan expires in {days_rem} days. "
            f"Renewing now keeps your GBP priority listing active. Reply YES to confirm Rs.{amount}."
        )

    elif kind == "supply_alert":
        molecule = trg_payload.get("molecule", "medication")
        batches = ", ".join(trg_payload.get("affected_batches", []))
        body = (
            f"{owner_name}, urgent recall: {molecule} — batches {batches}. "
            f"Isolate these units immediately. Want a compliance checklist? Reply YES."
        )

    elif kind == "chronic_refill_due":
        meds = ", ".join(trg_payload.get("molecule_list", ["medications"]))
        date = trg_payload.get("stock_runs_out_iso", "")[:10]
        body = (
            f"Hi {cust_name}, {merchant_name} here. Your {meds} run out by {date}. "
            f"Delivery address on file — shall we dispatch? Reply YES."
        )
        send_as = "merchant_on_behalf"

    elif kind == "cde_opportunity":
        d_id = trg_payload.get("digest_item_id", "")
        item = digest_map.get(d_id, {})
        credits = trg_payload.get("credits", item.get("credits", 0))
        fee = trg_payload.get("fee", item.get("actionable", ""))
        date_str = item.get("date", "")[:10] if item.get("date") else ""
        body = (
            f"{owner_name}, {item.get('title', 'CDE webinar')} — "
            + (f"{date_str}, " if date_str else "") +
            f"{credits} CDE credits, {fee}. Worth attending given your practice scope. Reply YES for details."
        )

    elif kind == "ipl_match_today":
        match = trg_payload.get("match", "IPL match")
        venue = trg_payload.get("venue", "")
        body = (
            f"{owner_name}, {match} tonight at {venue}! "
            f"Match-day combo special on your menu? I can post it on GBP right now — Reply YES."
        )

    elif kind == "gbp_unverified":
        path = trg_payload.get("verification_path", "postcard or phone call").replace("_", " or ")
        uplift = int(trg_payload.get("estimated_uplift_pct", 0) * 100)
        body = (
            f"{owner_name}, your Google profile is unverified — verified listings get ~{uplift}% more calls. "
            f"Verification via {path} takes 5 min. Want me to walk you through it? Reply YES."
        )

    elif kind == "category_seasonal":
        trends = trg_payload.get("trends", [])
        trends_str = ", ".join(str(t).replace("_", " ") for t in trends[:3])
        body = (
            f"{owner_name}, summer demand shift — {trends_str}. "
            f"Want me to update your GBP post to highlight high-demand items? Reply YES."
        )

    elif kind == "active_planning_intent":
        topic = trg_payload.get("intent_topic", "your idea").replace("_", " ")
        body = (
            f"{owner_name}, picking up from your message about '{topic}'. "
            f"Draft ready — just say YES to review."
        )

    elif kind == "curious_ask_due":
        if hi_mix:
            body = f"{owner_name}, quick question — is waqt kaun si service ke liye sabse zyada walk-in aa rahe hain?"
        else:
            body = f"{owner_name}, quick check — what service is getting the most walk-in requests this week?"
        cta = "open_ended"

    elif kind == "wedding_package_followup":
        wedding_date = trg_payload.get("wedding_date", "")[:10]
        days_to = trg_payload.get("days_to_wedding", "")
        body = (
            f"Hi {cust_name}, {merchant_name} here! Wedding on {wedding_date}. "
            f"With {days_to} days to go — time to start your skin-prep program. "
            f"Book a consult? Reply YES."
        )
        send_as = "merchant_on_behalf"

    elif kind == "trial_followup":
        trial_date = trg_payload.get("trial_date", "")
        slots = trg_payload.get("next_session_options", [])
        slot_str = slots[0].get("label", "") if slots else "available soon"
        body = (
            f"Hi {cust_name}, {merchant_name} here! Hope your trial on {trial_date} was great. "
            f"Next session: {slot_str}. Lock it in? Reply YES."
        )
        send_as = "merchant_on_behalf"

    else:
        if hi_mix:
            body = f"{owner_name}, Vera se bol rahi hoon — ek important update hai. Reply YES for details."
        else:
            body = f"{owner_name}, Vera here — something worth your attention. Reply YES to learn more."

    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": suppression_key,
        "rationale": (
            f"Trigger: {kind} | Merchant: {merchant_name} ({cat_slug}) | "
            f"Rule-based composer | CTA: {cta} | send_as: {send_as}"
        ),
    }


def _gemini_compose(category, merchant, trigger, customer):
    model = _get_gemini()
    if not model:
        return None
    try:
        prompt = _build_compose_prompt(category, merchant, trigger, customer)
        resp = model.generate_content(
            [{"role": "user", "parts": [SYSTEM_PROMPT + "\n\n" + prompt]}]
        )
        text = resp.text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        result = json.loads(text)
        required = {"body", "cta", "send_as", "suppression_key", "rationale"}
        if not required.issubset(result.keys()):
            return None
        if not result.get("body"):
            return None
        return result
    except Exception:
        return None


def compose(category, merchant, trigger, customer=None):
    result = _gemini_compose(category, merchant, trigger, customer)
    if result:
        return result
    return _fallback_compose(category, merchant, trigger, customer)
