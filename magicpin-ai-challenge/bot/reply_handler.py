from __future__ import annotations

import re
import os
import json

_AUTO_REPLY_PATTERNS = [
    r"thank you for contact",
    r"our team will (respond|get back)",
    r"we will (reply|respond) shortly",
    r"this is an automated (message|response|reply)",
    r"main ek automated assistant hoon",
    r"i am an? (automated|auto) (response|assistant|bot|reply)",
    r"aapki madad ke liye shukriya.*automated",
    r"bahut.shukriya.*team tak pahuncha",
    r"jaankari ke liye.*shukriya.*team",
]

_OPT_OUT_PATTERNS = [
    r"\bnot interested\b",
    r"\bstop (messaging|contacting|this)\b",
    r"\bdo not (message|contact|call)\b",
    r"\bopt.?out\b",
    r"\bremove (me|my number)\b",
    r"\bblock\b",
    r"\bwhy are you bothering\b",
    r"\buseless\b.*stop",
    r"\bstop send",
    r"\bband karo\b",
    r"\bbaz aa\b",
    r"\bmujhe nahi chahiye\b",
]

_INTENT_TRANSITION_PATTERNS = [
    r"\b(ok|okay|yes|haan|ha|go ahead|let'?s do it|confirm|kar do|karo|chaliye|chal|agree|proceed|do it|send it|share it|great let'?s)\b",
    r"\bwhat'?s next\b",
    r"\bkya karna hai\b",
    r"\bkaro\b",
    r"\bchale?\b",
    r"\bplease (send|do|share|draft|post)\b",
]

_HOSTILE_PATTERNS = [
    r"\b(chutiya|bc|mc|bhad me jao|shut up|idiot|stupid bot|bakwaas)\b",
    r"\bstop bothering\b",
    r"\bwhy (are|r) you (sending|messaging|bothering)\b",
]

_OFF_TOPIC_KEYWORDS = [
    "gst", "income tax", "legal advice", "court", "police", "aadhaar",
    "visa", "passport", "insurance claim", "loan", "credit card",
    "mutual fund", "stock market",
]


def _match_any(text, patterns):
    t = text.lower()
    return any(re.search(p, t) for p in patterns)


def detect_auto_reply(message):
    return _match_any(message, _AUTO_REPLY_PATTERNS)


def detect_opt_out(message):
    return _match_any(message, _OPT_OUT_PATTERNS)


def detect_intent_transition(message):
    t = message.lower().strip()
    if "?" in t and len(t) > 20:
        return False
    return _match_any(message, _INTENT_TRANSITION_PATTERNS)


def detect_hostile(message):
    return _match_any(message, _HOSTILE_PATTERNS)


def detect_off_topic(message):
    t = message.lower()
    return any(kw in t for kw in _OFF_TOPIC_KEYWORDS)


def count_auto_replies(history):
    count = 0
    for turn in reversed(history):
        if turn.get("from") == "merchant" and detect_auto_reply(turn.get("msg", "")):
            count += 1
        else:
            break
    return count


def _gemini_reply(conversation_id, history, message, merchant, category, customer, contexts):
    import google.generativeai as genai
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            "gemini-1.5-flash",
            generation_config={"temperature": 0, "max_output_tokens": 400},
        )
        merchant_name = (merchant.get("identity") or {}).get("name", "")
        cat_slug = merchant.get("category_slug", "")
        languages = (merchant.get("identity") or {}).get("languages", ["en"])
        hi_mix = "hi" in languages

        history_text = "\n".join(
            f"[{t.get('from','?').upper()}]: {t.get('msg','')}" for t in history[-6:]
        )

        system = (
            "You are Vera, magicpin's merchant AI assistant on WhatsApp.\n"
            "Reply to the merchant's last message. Rules:\n"
            "- One CTA only. No URLs. No re-introduction.\n"
            "- Peer tone, not promotional.\n"
            f"- Language: {'Hindi-English code-mix' if hi_mix else 'English'}.\n"
            "- Output ONLY JSON: {\"action\":\"send\",\"body\":\"...\",\"cta\":\"...\",\"rationale\":\"...\"}\n"
            "  or {\"action\":\"wait\",\"wait_seconds\":N,\"rationale\":\"...\"}\n"
            "  or {\"action\":\"end\",\"rationale\":\"...\"}\n"
        )

        prompt = (
            f"Merchant: {merchant_name} ({cat_slug})\n"
            f"Conversation history:\n{history_text}\n"
            f"Latest merchant message: \"{message}\"\n"
            "What is Vera's next action?"
        )

        resp = model.generate_content([{"role": "user", "parts": [system + "\n\n" + prompt]}])
        text = resp.text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except Exception:
        return None


def compose_reply(conversation_id, merchant_id, customer_id, message, turn_number, history, contexts):
    merchant = (contexts.get(("merchant", merchant_id)) or {}).get("payload", {})
    cat_slug = merchant.get("category_slug", "")
    category = (contexts.get(("category", cat_slug)) or {}).get("payload", {})
    customer = None
    if customer_id:
        customer = (contexts.get(("customer", customer_id)) or {}).get("payload", {})

    merchant_name = (merchant.get("identity") or {}).get("name", merchant_id)
    owner_name = (merchant.get("identity") or {}).get("owner_first_name", merchant_name)
    languages = (merchant.get("identity") or {}).get("languages", ["en"])
    hi_mix = "hi" in languages

    if detect_opt_out(message) or detect_hostile(message):
        return {
            "action": "end",
            "rationale": "Merchant signaled opt-out or expressed hostility. Closing conversation; suppressing triggers for this merchant.",
        }

    if detect_auto_reply(message):
        auto_count = count_auto_replies(history) + 1
        if auto_count == 1:
            return {
                "action": "send",
                "body": (
                    f"Looks like an auto-reply. Jab owner phone dekhe, bas 'YES' reply karen — aage process kar deti hoon."
                    if hi_mix else
                    f"Looks like an auto-reply. When the owner sees this, just reply YES and I'll take it from there."
                ),
                "cta": "binary_yes_no",
                "rationale": "First auto-reply detected; prompting owner to engage when available.",
            }
        elif auto_count == 2:
            return {
                "action": "wait",
                "wait_seconds": 14400,
                "rationale": "Second consecutive auto-reply — owner not at phone. Backing off 4 hours.",
            }
        else:
            return {
                "action": "end",
                "rationale": f"{auto_count} consecutive auto-replies with no real engagement. Closing conversation to avoid spam.",
            }

    if detect_off_topic(message):
        topic_clue = next((kw for kw in _OFF_TOPIC_KEYWORDS if kw in message.lower()), "that topic")
        return {
            "action": "send",
            "body": (
                f"Woh meri expertise se bahar hai ({topic_clue}) — "
                f"iske liye apne CA/specialist se baat karein. "
                f"Back to what we were discussing — shall we proceed? Reply YES."
                if hi_mix else
                f"That ({topic_clue}) is outside what I can help with — your CA/specialist would be best. "
                f"Coming back to our conversation — shall we proceed? Reply YES."
            ),
            "cta": "binary_yes_no",
            "rationale": f"Off-topic ask about '{topic_clue}' politely declined; redirected to main conversation.",
        }

    if detect_intent_transition(message) and turn_number <= 4:
        llm_result = _gemini_reply(conversation_id, history, message, merchant, category, customer, contexts)
        if llm_result and llm_result.get("action") == "send":
            return llm_result

        active_offers = [o for o in merchant.get("offers", []) if o.get("status") == "active"]
        offer_title = active_offers[0]["title"] if active_offers else "your active offer"

        if hi_mix:
            body = (
                f"Perfect! Aage badhte hain — "
                f"'{offer_title}' ke liye GBP post + WhatsApp template draft kar rahi hoon. "
                f"CONFIRM reply karen to send."
            )
        else:
            body = (
                f"Great — moving forward now. "
                f"Drafting your GBP post + WhatsApp template for '{offer_title}'. "
                f"Reply CONFIRM to send."
            )
        return {
            "action": "send",
            "body": body,
            "cta": "binary_confirm_cancel",
            "rationale": "Merchant explicitly committed — switching from qualifying to action mode. Presenting concrete next step.",
        }

    llm_result = _gemini_reply(conversation_id, history, message, merchant, category, customer, contexts)
    if llm_result:
        return llm_result

    msg_lower = message.lower()
    if any(w in msg_lower for w in ["send", "share", "draft", "post", "yes", "haan", "ha", "ok", "go"]):
        return {
            "action": "send",
            "body": (
                f"Sending it now — also preparing a follow-up step. "
                f"Aur kuch chahiye? Reply YES for more options, STOP to close."
                if hi_mix else
                f"Sending now — also lined up a follow-up step. Anything else? Reply YES for more or STOP to close."
            ),
            "cta": "binary_yes_no",
            "rationale": "Merchant accepted; fulfilling request and offering follow-up.",
        }
    elif any(w in msg_lower for w in ["no", "nahi", "nope", "later", "baad mein"]):
        return {
            "action": "wait",
            "wait_seconds": 86400,
            "rationale": "Merchant deferred; backing off 24 hours before next proactive touch.",
        }
    else:
        return {
            "action": "send",
            "body": (
                f"Samajh gayi — thoda aur detail share karein taaki main sahi help kar sakoon. "
                f"Reply karein ya simply YES kehein to proceed."
                if hi_mix else
                f"Got it — could you share a bit more detail so I can help precisely? "
                f"Or simply reply YES to proceed."
            ),
            "cta": "open_ended",
            "rationale": "Merchant asked a question or gave ambiguous reply; requesting clarification.",
        }
