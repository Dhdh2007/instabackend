# routers/motivation.py
from fastapi import APIRouter, Depends
from datetime import datetime, timezone
import random

from app.ai_provider import call_model_json, AIProviderError
from app.auth import verify_jwt_and_get_user_id

router = APIRouter()

_cache: dict[str, dict] = {}  # { user_id: {"expires_at": <timestamp>, "quote": ...} }
TTL_SECONDS = 1 * 60  # 5 minutes — change this number to taste

SYSTEM_PROMPT = (
    "You write short dashboard header lines for 'DM Trigger Bot', a "
    "social-media auto-responder SaaS. Tone: witty, a little sarcastic, "
    "genuinely motivating. Always reply with ONLY a JSON object in the "
    'exact shape {"quote": "..."}. Max 18 words. No hashtags. At most one emoji.'
)
USER_PROMPT = "Give me one line for today's dashboard header."

FALLBACK_QUOTES = [
    "Your DMs are doing your job for you. You're welcome.",
    "Automate the small talk, save your charm for the closers.",
    "Somewhere a lead is getting nurtured while you sip coffee.",
]

@router.get("/mp")
async def get_motivation_quote(user_id: str = Depends(verify_jwt_and_get_user_id)):
    now = datetime.now(timezone.utc).timestamp()

    cached = _cache.get(user_id)
    if cached and cached["expires_at"] > now:
        return {"quote": cached["quote"], "cached": True}

    try:
        result = await call_model_json(SYSTEM_PROMPT, USER_PROMPT)
        quote = result["quote"].strip()
    except (AIProviderError, KeyError, TypeError):
        quote = random.choice(FALLBACK_QUOTES)

    _cache[user_id] = {"expires_at": now + TTL_SECONDS, "quote": quote}
    return {"quote": quote, "cached": False}