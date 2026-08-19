import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

from app.auth import verify_jwt_and_get_user_id
from app.config import get_settings, get_supabase_admin_client
from app.schemas import (
    ActivityEvent,
    AnalyticsOut,
    CampaignCreate,
    CampaignOut,
    DailyTrendPoint,
    CampaignUpdate
)

logger = logging.getLogger("dm_trigger_bot")

router = APIRouter(tags=["Dashboard & Analytics"])


def format_relative_time(iso_timestamp: str) -> str:
    sent_at = datetime.fromisoformat(iso_timestamp)
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    seconds = int((now - sent_at).total_seconds())

    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


@router.post("/api/campaigns", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
def create_campaign(
    payload: CampaignCreate,
    user_id: str = Depends(verify_jwt_and_get_user_id),
    db: Client = Depends(get_supabase_admin_client),
):
    result = (
        db.table("campaigns")
        .insert(
            {
                "user_id": user_id,
                "trigger_word": payload.trigger_word.strip().lower(),
                "destination_link": str(payload.destination_link),
                "message_template": payload.message_template,
                "is_active": True,
            }
        )
        .execute()
    )

    if not result.data:
        logger.error("Campaign insert returned no data for user %s", user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create campaign.",
        )

    return result.data[0]


@router.patch("/api/campaigns/{campaign_id}", response_model=CampaignOut)
def update_campaign(
    campaign_id: str,
    payload: CampaignUpdate,
    user_id: str = Depends(verify_jwt_and_get_user_id),
    db: Client = Depends(get_supabase_admin_client),
):
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "trigger_word" in updates:
        updates["trigger_word"] = updates["trigger_word"].strip().lower()
    if "destination_link" in updates:
        updates["destination_link"] = str(updates["destination_link"])

    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields provided to update.",
        )

    result = (
        db.table("campaigns")
        .update(updates)
        .eq("id", campaign_id)
        .eq("user_id", user_id)
        .execute()
    )

    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found.",
        )

    return result.data[0]


@router.delete("/api/campaigns/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_campaign(
    campaign_id: str,
    user_id: str = Depends(verify_jwt_and_get_user_id),
    db: Client = Depends(get_supabase_admin_client),
):
    """
    Deletes a single trigger word. Scoped with .eq("user_id", user_id) in
    the same query as the delete itself — this is what stops user A from
    deleting user B's campaign just by guessing/reusing an id, since the
    delete only matches a row that's both this id AND owned by the caller.
    """
    result = (
        db.table("campaigns")
        .delete()
        .eq("id", campaign_id)
        .eq("user_id", user_id)
        .execute()
    )

    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign not found.",
        )


# GET /api/campaigns
@router.get("/api/campaigns", response_model=List[CampaignOut])
def list_campaigns(
    user_id: str = Depends(verify_jwt_and_get_user_id),
    db: Client = Depends(get_supabase_admin_client),
):
    result = (
        db.table("campaigns")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data


# GET /api/activity
@router.get("/api/activity")
def get_activity(
    user_id: str = Depends(verify_jwt_and_get_user_id),
    db: Client = Depends(get_supabase_admin_client),
):
    result = (
        db.table("dm_events")
        .select("id, trigger_word, commenter_username, sent_at")
        .eq("user_id", user_id)
        .order("sent_at", desc=True)
        .limit(10)
        .execute()
    )

    return [
        {
            "id": row["id"],
            "username": row["commenter_username"],
            "trigger": row["trigger_word"],
            "time": format_relative_time(row["sent_at"]),
        }
        for row in result.data
    ]


# GET /api/analytics
@router.get("/api/analytics", response_model=AnalyticsOut)
def get_analytics(
    user_id: str = Depends(verify_jwt_and_get_user_id),
    db: Client = Depends(get_supabase_admin_client),
):
    settings = get_settings()

    profile_result = (
        db.table("profiles")
        .select("dms_sent_count")
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )
    dms_sent_count = profile_result.data["dms_sent_count"] if profile_result.data else 0

    today = datetime.now(timezone.utc).date()
    window_start = datetime.combine(
        today - timedelta(days=6), datetime.min.time(), tzinfo=timezone.utc
    )

    events_result = (
        db.table("dm_events")
        .select("id, trigger_word, commenter_username, sent_at")
        .eq("user_id", user_id)
        .gte("sent_at", window_start.isoformat())
        .order("sent_at", desc=True)
        .execute()
    )
    events = events_result.data

    counts_by_day: dict[str, int] = defaultdict(int)
    for event in events:
        event_date = datetime.fromisoformat(event["sent_at"]).date().isoformat()
        counts_by_day[event_date] += 1

    daily_trend = [
        DailyTrendPoint(
            date=(today - timedelta(days=offset)).isoformat(),
            count=counts_by_day[(today - timedelta(days=offset)).isoformat()],
        )
        for offset in range(6, -1, -1)
    ]

    recent_activity = [ActivityEvent(**event) for event in events[:10]]

    return AnalyticsOut(
        dms_sent_count=dms_sent_count,
        free_tier_limit=settings.FREE_TIER_DM_LIMIT,
        daily_trend=daily_trend,
        recent_activity=recent_activity,
    )


# DELETE /api/account
@router.delete("/api/account", status_code=status.HTTP_200_OK)
def delete_account(
    user_id: str = Depends(verify_jwt_and_get_user_id),
    db: Client = Depends(get_supabase_admin_client),
):
    """
    Permanently deletes everything tied to this user: their DM event
    history, their campaigns, their profile row, and finally the
    Supabase auth user itself. Irreversible — the frontend is expected
    to have already gotten explicit confirmation before calling this.

    This is also the actual mechanism behind Meta's "data deletion"
    requirement: a real, self-service way for a user to erase their
    data, not just a policy statement about doing so on request.
    Order matters here — dm_events and campaigns reference the user,
    so they're removed first, then the profile, then the auth account.
    """
    db.table("dm_events").delete().eq("user_id", user_id).execute()
    db.table("campaigns").delete().eq("user_id", user_id).execute()
    db.table("profiles").delete().eq("id", user_id).execute()

    try:
        db.auth.admin.delete_user(user_id)
    except Exception:
        logger.exception("Failed to delete auth user %s after wiping data", user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Your data was deleted, but we couldn't remove your login. Contact support to finish closing your account.",
        )

    return {"status": "deleted"}
