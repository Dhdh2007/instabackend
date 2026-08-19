"""
lead_scoring.py

Scores a batch of leads (comment + DM reply text) for purchase intent.
Batching matters for two reasons: it's cheaper (one call instead of N),
and it's actually more accurate — the model sees the spread of replies
in context ("busy asking price" vs "just typed the trigger word for fun")
instead of judging each in a vacuum.

BATCH_SIZE=10 is a reasonable default: small enough that the model's
JSON reply stays well-formed and reviewable, big enough to be efficient.
"""

from __future__ import annotations

from typing import Any

from .ai_provider import call_model_json

BATCH_SIZE = 10

SYSTEM_PROMPT = """You score Instagram leads for a small business's DM-automation bot.
Each lead commented a trigger word on a post, got an automated DM, and may have replied.
For EACH lead, decide how genuinely interested they are in buying/booking, from 0-100,
and whether they're a real prospect (true) or just noise — a joke reply, a bot, someone
who never replied, or clearly not a buyer (false).

Respond with ONLY a JSON array, one object per lead, in the SAME ORDER you received them:
[{"id": "<lead id>", "confidence_score": 0-100, "is_interested": true|false, "reason": "<under 12 words>"}]

No prose, no markdown fences, just the JSON array."""


def _format_lead(lead: dict[str, Any]) -> str:
    return (
        f"id: {lead['id']}\n"
        f"trigger_word: {lead.get('trigger_word') or 'n/a'}\n"
        f"comment: {lead.get('comment_text') or 'n/a'}\n"
        f"dm_reply: {lead.get('dm_reply_text') or '(no reply yet)'}\n"
    )


async def score_lead_batch(leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    leads: list of dicts with at least id, trigger_word, comment_text, dm_reply_text.
    Returns list of {id, confidence_score, is_interested, reason} — may be shorter
    than the input if the model drops a malformed entry, so callers should match
    on id rather than assuming positional alignment.
    """
    if not leads:
        return []

    user_prompt = "Score these leads:\n\n" + "\n---\n".join(_format_lead(l) for l in leads)
    result = await call_model_json(SYSTEM_PROMPT, user_prompt)

    if not isinstance(result, list):
        raise ValueError(f"Expected a JSON array of scores, got: {type(result)}")

    cleaned = []
    for item in result:
        if not isinstance(item, dict) or "id" not in item:
            continue
        cleaned.append(
            {
                "id": item["id"],
                "confidence_score": max(0, min(100, int(item.get("confidence_score", 0)))),
                "is_interested": bool(item.get("is_interested", False)),
                "reason": str(item.get("reason", ""))[:200],
            }
        )
    return cleaned


async def score_all_unanalyzed(leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chunks a big unanalyzed queue into BATCH_SIZE pieces and scores each."""
    scored: list[dict[str, Any]] = []
    for i in range(0, len(leads), BATCH_SIZE):
        chunk = leads[i : i + BATCH_SIZE]
        scored.extend(await score_lead_batch(chunk))
    return scored