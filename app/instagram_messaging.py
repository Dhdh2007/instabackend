"""
instagram_messaging.py — handles the "messaging" events Meta sends inside
the same /api/webhook/instagram POST as comment changes: i.e. when a
customer DMs your page directly, OR replies to the DM your bot already sent
after they commented the trigger word ("tree" -> bot DMs them -> they reply
"tree price" -> THIS is what handles that reply).

Wired into main.py's receive_instagram_webhook(), which already loops
`entry.get("messaging", [])` and calls:

    await handle_messaging_event(messaging_event, db, memory_store)

This module is the missing piece behind that call.

Flow:
  1. Pull sender psid + recipient (your IG account) id + message text out of
     Meta's messaging_event envelope. Skip echoes (messages YOUR page sent,
     which Meta also delivers back to you) and anything with no text.
  2. Look up which of your users owns that IG account (profiles table),
     same pattern as _match_and_record() in main.py.
  3. Load that business's BusinessMemoryBox from memory_store (now backed
     by Supabase, see app/memory_store.py).
  4. Ask qa_engine to answer, grounded only in that memory box, via your
     existing NVIDIA fallback chain.
  5. Send the answer back via send_instagram_dm — same helper main.py
     already uses for the initial trigger-word DM.

Every step is defensive: like receive_instagram_webhook() in main.py, this
must never raise up into the webhook loop — a bad/malformed event should be
logged and skipped, not take down processing of the rest of the payload.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.config import get_settings
from app.instagram_oauth import send_instagram_dm
from app.routers.memory_store import MemoryStore
from app.qaengine import answer_customer_message

logger = logging.getLogger("dm_trigger_bot.instagram_messaging")


def _extract_event_fields(messaging_event: dict) -> Optional[tuple[str, str, str]]:
    """Returns (sender_igsid, recipient_account_id, text), or None to skip."""
    message = messaging_event.get("message") or {}

    # Meta echoes back messages YOUR page sent inside the same messaging
    # array — without this check, the bot would "answer" its own DMs.
    if message.get("is_echo"):
        return None

    text = message.get("text")
    sender_igsid = (messaging_event.get("sender") or {}).get("id")
    recipient_account_id = (messaging_event.get("recipient") or {}).get("id")

    if not (text and sender_igsid and recipient_account_id):
        return None

    return sender_igsid, recipient_account_id, text


def _lookup_business(db: Any, recipient_account_id: str) -> Optional[tuple[str, Optional[str]]]:
    """Returns (business_id, page_access_token), or None if unknown account.

    Mirrors the profiles lookup in main.py's _match_and_record(). If your
    profiles table/columns are named differently, adjust this query —
    everything downstream only needs the (business_id, page_access_token)
    tuple this returns.
    """
    profile_res = (
        db.table("profiles")
        .select("id, instagram_access_token")
        .eq("instagram_account_id", recipient_account_id)
        .maybe_single()
        .execute()
    )
    if not profile_res.data:
        return None
    return profile_res.data["id"], profile_res.data.get("instagram_access_token")


async def handle_messaging_event(
    messaging_event: dict,
    db: Any,
    memory_store: MemoryStore,
) -> None:
    fields = _extract_event_fields(messaging_event)
    if fields is None:
        return
    sender_igsid, recipient_account_id, text = fields

    business = _lookup_business(db, recipient_account_id)
    if business is None:
        logger.info(
            "instagram_messaging: ignoring DM for unknown account %s", recipient_account_id
        )
        return
    business_id, page_access_token = business

    if not page_access_token:
        logger.warning(
            "instagram_messaging: no page_access_token on file for business %s, cannot reply",
            business_id,
        )
        return

    memory = memory_store.get_memory_box(business_id)

    settings = get_settings()
    result = await answer_customer_message(
        memory,
        text,
        fallback_contact_email=getattr(settings, "DEFAULT_SUPPORT_EMAIL", None),
    )

    delivered = send_instagram_dm(
        recipient_igsid=sender_igsid,
        message_text=result.answer,
        page_access_token=page_access_token,
    )
    logger.info(
        "instagram_messaging: business=%s grounded=%s delivered=%s",
        business_id,
        result.grounded,
        delivered,
    )