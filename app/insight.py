"""
routers/insights.py

Mount this in your FastAPI app with:
    from app.routers import insights
    app.include_router(insights.router, prefix="/api")

Endpoints (all business_id values must match the authenticated user —
swap the `verify_owner` stub below for your real JWT/Supabase auth dependency):

  GET  /api/insights/{business_id}/dashboard  -> persisted dashboard payload
  GET  /api/insights/{business_id}/leads      -> lead list (for the table)
  POST /api/insights/{business_id}/analyze    -> batch-score unanalyzed leads,
                                                   recompute stats, persist them
  POST /api/insights/{business_id}/ask        -> chatbox: ask a question about
                                                   this business's leads

FIX (this version): the route decorators no longer repeat "/insights" —
the router already has prefix="/insights", so a decorator like
"/insights/{business_id}" was producing the double-nested path
"/api/insights/insights/{business_id}", which 404'd. Routes are now
"/{business_id}/..." so the final path is "/api/insights/{business_id}/...",
matching the order the frontend calls them in (resource id first, then
the action/sub-resource).

/history is not included here — it's already deployed separately and
this file doesn't know its implementation, so it's left untouched.

This file uses `get_supabase_admin_client()` from app/config.py and
`verify_owner()` from app/auth.py — a real Supabase-JWT bearer-token
check (via supabase.auth.get_claims) scoped to the business_id in the URL.
Frontend calls must send `Authorization: Bearer <supabase access token>`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import verify_owner
from .config import get_supabase_admin_client
from .lead_scoring import score_all_unanalyzed
from pydantic import BaseModel
from .ai_provider import call_model_json, AIProviderError
router = APIRouter(prefix = "/api")


class AskBody(BaseModel):
    question: str


@router.get("/{business_id}/dashboard")
async def get_insights(business_id: str, sb=Depends(get_supabase_admin_client), _=Depends(verify_owner)):
    row = (
        sb.table("business_insights")
        .select("*")
        .eq("business_id", business_id)
        .maybe_single()
        .execute()
    )
    payload = row.data or {} if row else {}

    # Today's leads — computed live so it's never stale, unlike the cached charts.
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_leads = (
        sb.table("leads")
        .select("id", count="exact")
        .eq("business_id", business_id)
        .gte("created_at", today_start.isoformat())
        .execute()
    )
    today_replied = (
        sb.table("leads")
        .select("id", count="exact")
        .eq("business_id", business_id)
        .gte("created_at", today_start.isoformat())
        .eq("status", "replied")
        .execute()
    )

    payload["leadsToday"] = today_leads.count or 0
    payload["repliesToday"] = today_replied.count or 0
    return {"data": payload}


@router.get("/{business_id}/leads")
async def list_leads(
    business_id: str,
    limit: int = 50,
    sb=Depends(get_supabase_admin_client),
    _=Depends(verify_owner),
):
    rows = (
        sb.table("leads")
        .select("*")
        .eq("business_id", business_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return {"data": rows.data or []}


@router.post("/{business_id}/analyze")
async def analyze_leads(business_id: str, sb=Depends(get_supabase_admin_client), _=Depends(verify_owner)):
    """
    Pulls every lead that hasn't been scored yet, batches them to the
    AI provider for a confidence score + interest flag, writes the
    scores back, then recomputes the cached dashboard stats.
    """
    unanalyzed = (
        sb.table("leads")
        .select("id, trigger_word, comment_text, dm_reply_text")
        .eq("business_id", business_id)
        .is_("analyzed_at", "null")
        .limit(200)
        .execute()
    ).data or []

    if not unanalyzed:
        return {"data": {"scored": 0, "message": "Nothing new to analyze."}}

    try:
        scores = await score_all_unanalyzed(unanalyzed)
    except AIProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    now = datetime.now(timezone.utc).isoformat()
    for s in scores:
        sb.table("leads").update(
            {
                "confidence_score": s["confidence_score"],
                "is_interested": s["is_interested"],
                "ai_reason": s["reason"],
                "analyzed_at": now,
            }
        ).eq("id", s["id"]).execute()

    await _recompute_dashboard(sb, business_id)
    return {"data": {"scored": len(scores)}}


@router.post("/{business_id}/ask")
async def ask_insights(
    business_id: str,
    body: AskBody,
    sb=Depends(get_supabase_admin_client),
    _=Depends(verify_owner),
):
    leads = (
        sb.table("leads")
        .select("ig_username, trigger_word, comment_text, dm_reply_text, confidence_score, is_interested, status, created_at")
        .eq("business_id", business_id)
        .order("created_at", desc=True)
        .limit(150)
        .execute()
    ).data or []

    system_prompt = (
       "You are the AI Insights assistant inside a small business's DM-automation "
        "dashboard. You speak in a formal, professional tone at all times — no slang, "
        "no emojis, no casual phrasing.\n\n"
        "Answer the owner's question using ONLY the lead data given below. Be concrete: "
        "cite counts, usernames, or trigger words when relevant.\n\n"
        "Boundary: only answer questions about this business's leads, comments, DM "
        "replies, trigger-word performance, or conversion activity. If the question is "
        "unrelated to this lead data (general chit-chat, unrelated topics, requests "
        "outside this dashboard's scope), politely decline and redirect the user to ask "
        "about their leads instead — do not attempt to answer it from general knowledge.\n\n"
        'Respond as JSON: {"answer": "<your answer, plain text, a few sentences max>"}'
    )
    user_prompt = f"Lead data (most recent first):\n{leads}\n\nQuestion: {body.question}"

    try:
        result = await call_model_json(system_prompt, user_prompt)
    except AIProviderError as exc:
        print(f"AI PROVIDER FAILED: {exc}")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    answer = result.get("answer", "") if isinstance(result, dict) else str(result)
    return {"answer": answer, "steps": ["Reading your leads", "Cross-checking replies", "Writing the answer"]}


async def _recompute_dashboard(sb, business_id: str) -> None:
    """Rebuilds the cached stats/charts row from the current leads table."""
    leads = (
        sb.table("leads").select("*").eq("business_id", business_id).execute()
    ).data or []

    total = len(leads)
    replied = sum(1 for l in leads if l["status"] == "replied")
    interested = sum(1 for l in leads if l.get("is_interested"))
    scored = [l for l in leads if l.get("confidence_score") is not None]
    avg_confidence = round(sum(l["confidence_score"] for l in scored) / len(scored)) if scored else 0

    trigger_counts: dict[str, dict[str, int]] = {}
    for l in leads:
        w = l.get("trigger_word") or "unknown"
        trigger_counts.setdefault(w, {"comments": 0, "replied": 0})
        trigger_counts[w]["comments"] += 1
        if l["status"] == "replied":
            trigger_counts[w]["replied"] += 1

    trigger_performance = [
        {
            "word": w,
            "comments": c["comments"],
            "conversion": round(100 * c["replied"] / c["comments"]) if c["comments"] else 0,
        }
        for w, c in trigger_counts.items()
    ]

    since = datetime.now(timezone.utc) - timedelta(days=13)
    day_buckets: dict[str, dict[str, int]] = {}
    for l in leads:
        created = datetime.fromisoformat(l["created_at"].replace("Z", "+00:00"))
        if created < since:
            continue
        day = created.strftime("%a")
        day_buckets.setdefault(day, {"comments": 0, "dms": 0})
        day_buckets[day]["comments"] += 1
        if l["status"] in ("dm_sent", "replied", "no_reply"):
            day_buckets[day]["dms"] += 1

    engagement_series = [{"day": d, **v} for d, v in day_buckets.items()]

    top_word = max(trigger_performance, key=lambda t: t["conversion"], default=None)
    recommendation = None
    if top_word:
        recommendation = {
            "label": "Recommendation",
            "title": f'Lean into "{top_word["word"]}"',
            "body": (
                f'"{top_word["word"]}" converts at {top_word["conversion"]}% — '
                "consider using it in your next post's caption or pinned comment."
            ),
            "confidence": avg_confidence,
            "buttonText": "Schedule my next post",
        }

    payload = {
        "business_id": business_id,
        "stats": [
            {"label": "Total leads", "value": total},
            {"label": "Replied", "value": replied},
            {"label": "Marked interested", "value": interested},
        ],
        "engagement_series": engagement_series,
        "trigger_performance": trigger_performance,
        "recommendation": recommendation,
        "confidence": avg_confidence,
        "analysis_steps": [
            "Pulling new comments",
            "Scoring replies for intent",
            "Updating your dashboard",
        ],
        "ask_steps": ["Reading your leads", "Cross-checking replies", "Writing the answer"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    sb.table("business_insights").upsert(payload, on_conflict="business_id").execute()