from fastapi import APIRouter, Depends
from datetime import datetime, timezone
import random

from app.ai_provider import call_model_json, AIProviderError
from app.auth import verify_jwt_and_get_user_id

router = APIRouter()

_cache: dict[str, dict] = {}  # { user_id: {"expires_at": <timestamp>, "quote": ...} }
TTL_SECONDS = 30  # matches the frontend's 1-min poll — no point caching longer

# Different comedic "flavors" — picked at random each generation so it
# doesn't feel like the same joke-bot every time
VIBE_TYPES = [
    "dry deadpan sarcasm",
    "unhinged chaotic-good hype-man energy",
    "dad-joke level pun humor",
    "passive-aggressive roast of the user's inbox habits",
    "overly dramatic movie-trailer narrator voice",
    "conspiracy-theorist about how good the automation is",
]

SYSTEM_PROMPT = (
    "You write short, funny one-liners for the header of 'DM Trigger Bot', "
    "a social-media auto-responder SaaS dashboard. You will be told which "
    "comedic vibe to use for this line — commit to it hard. Be genuinely "
    "funny, not corporate-funny. Always reply with ONLY a JSON object in "
    'the exact shape {"quote": "..."}. Max 18 words. No hashtags. At most '
    "one emoji, and only if it actually adds to the joke."
)

FALLBACK_QUOTES = [
    "Your DMs are doing your job for you. Rude, honestly.",
    "Somewhere a lead just got auto-nurtured. You did nothing. Legend.",
    "This bot has better follow-up game than most humans.",
    "404: motivation not found. Here's a robot doing your job instead.",
    "Breaking: local SaaS replies to comments faster than your ex replied to texts.",
]

@router.get("/mp")
async def get_motivation_quote(user_id: str = Depends(verify_jwt_and_get_user_id)):
    now = datetime.now(timezone.utc).timestamp()

    cached = _cache.get(user_id)
    if cached and cached["expires_at"] > now:
        return {"quote": cached["quote"], "cached": True}

    vibe = random.choice(VIBE_TYPES)
    user_prompt = f"Give me one line for today's dashboard header. Vibe: {vibe}."

    try:
        result = await call_model_json(SYSTEM_PROMPT, user_prompt)
        quote = result["quote"].strip()
    except (AIProviderError, KeyError, TypeError):
        quote = random.choice(FALLBACK_QUOTES)

    _cache[user_id] = {"expires_at": now + TTL_SECONDS, "quote": quote}
    return {"quote": quote, "cached": False}