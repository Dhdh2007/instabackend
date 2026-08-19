"""
qa_engine.py — answers a single customer DM (e.g. "tree price") using ONLY
facts stored in that business's BusinessMemoryBox, via the same NVIDIA NIM
fallback chain insights_engine.py already uses.

This is a separate, narrower thing from insights_engine.generate_insights():
that analyzes a whole conversation history after the fact; this answers one
incoming message, in real time, so it can be DM'd straight back.

Design choices, and why:

- Reuses insights_engine.build_grounding_context() rather than re-serializing
  the memory box a second way — one definition of "what the AI is allowed to
  know," used consistently by both the insights report and the DM auto-reply.
- The model is told, explicitly, to answer ONLY from the memory box and to
  say so (grounded=false) if the box doesn't contain the fact, rather than
  letting it improvise a plausible-sounding price.
- chat_json_with_fallback() is synchronous (uses `requests`) and already
  tries multiple NVIDIA models in order — a bad/invalid-JSON response from
  one model just moves to the next. Since main.py awaits this from an async
  webhook handler, the blocking call is pushed onto a thread via
  asyncio.to_thread so it doesn't stall the event loop.
- Every failure mode (all NVIDIA models down, malformed JSON from all of
  them, no memory box on file, empty message) resolves to a safe fallback
  message rather than raising — a webhook handler that raises here means
  the customer just never gets a reply.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from pydantic import BaseModel

from app.routers.insights_engine import build_grounding_context
from app.routers.nvidia_client import NvidiaAPIError, chat_json_with_fallback
from .schemas import BusinessMemoryBox

logger = logging.getLogger("ai_insights.qa_engine")

GENERIC_FALLBACK = (
    "Thanks for your message! I don't have that detail handy right now — "
    "please reach out to us  directly and we'll get you a straight answer."
)

DM_REPLY_SYSTEM_PROMPT_TEMPLATE = """You are replying to a single Instagram DM from \
a customer, on behalf of this business. Answer using ONLY the facts listed below. Do \
not invent, guess, or estimate anything not explicitly present here — no prices, no \
policies, no availability, nothing.

BUSINESS MEMORY BOX:
{context}

Rules:
- If the facts above answer the customer's message, write a short, friendly DM \
reply (1-3 sentences, under 400 characters) that answers it, in the business's \
brand voice if one is given.
- If the facts above do NOT contain what's needed to answer, do not guess — \
set "grounded" to false and leave "answer" as an empty string.
- Never contradict any business rule listed above.
- Respond with ONLY a single JSON object, no markdown fences, no commentary, \
in exactly this shape: {{"grounded": true or false, "answer": "..."}}"""


class QAResult(BaseModel):
    grounded: bool
    answer: str


def _build_system_prompt(memory: BusinessMemoryBox) -> str:
    return DM_REPLY_SYSTEM_PROMPT_TEMPLATE.format(context=build_grounding_context(memory))


def _answer_sync(memory: BusinessMemoryBox, user_message: str) -> QAResult:
    """Blocking call — run this via asyncio.to_thread from async callers."""
    system_prompt = _build_system_prompt(memory)
    try:
        parsed, model_used = chat_json_with_fallback(
            system_prompt=system_prompt,
            user_prompt=user_message,
            temperature=0.0,
            max_tokens=300,
        )
    except NvidiaAPIError:
        logger.exception("qa_engine: every NVIDIA model failed or returned invalid JSON")
        return QAResult(grounded=False, answer="")

    grounded = bool(parsed.get("grounded"))
    answer = str(parsed.get("answer") or "").strip()
    if not grounded or not answer:
        return QAResult(grounded=False, answer="")

    logger.info("qa_engine: grounded answer produced with model=%s", model_used)
    return QAResult(grounded=True, answer=answer)


async def answer_customer_message(
    memory: Optional[BusinessMemoryBox],
    user_message: str,
    *,
    fallback_contact_email: Optional[str] = None,
) -> QAResult:
    """
    Returns a QAResult that is always safe to send back to the customer as-is:
    - grounded=True  -> answer is the AI's DM reply, grounded in memory box facts.
    - grounded=False -> answer is a fallback message (never empty).
    """
    contact_email = (memory.contact_email if memory else None) or fallback_contact_email
    fallback_text = GENERIC_FALLBACK
    if contact_email:
        fallback_text += f" You can also email us at {contact_email}."

    if memory is None or not user_message.strip():
        return QAResult(grounded=False, answer=fallback_text)

    try:
        result = await asyncio.to_thread(_answer_sync, memory, user_message)
    except Exception:
        logger.exception("qa_engine: unexpected failure answering customer message")
        return QAResult(grounded=False, answer=fallback_text)

    if not result.grounded:
        return QAResult(grounded=False, answer=fallback_text)

    return result