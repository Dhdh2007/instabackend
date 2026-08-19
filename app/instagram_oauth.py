# =============================================================================
# app/instagram_oauth.py
# -----------------------------------------------------------------------------
# Rebuilt on "Instagram API with Instagram Login" instead of Facebook Login
# for Business. The account logs into Instagram directly — no Facebook Page,
# no /me/accounts lookup, no pages_manage_metadata permission fight.
#
#   GET /api/instagram/oauth/start-url   (protected)
#   GET /api/instagram/oauth/callback    (public — Instagram redirects here)
#   DELETE /api/instagram/oauth/disconnect  (protected)
#   GET /api/instagram/oauth/webhook     (public — Meta verification)
#   POST /api/instagram/oauth/webhook    (public — Meta event delivery)
#   send_instagram_dm(...)
# =============================================================================
import logging
import time
from urllib.parse import urlencode

import httpx
import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from supabase import Client

from app.auth import verify_jwt_and_get_user_id
from app.config import get_settings, get_supabase_admin_client

logger = logging.getLogger("dm_trigger_bot")

router = APIRouter(prefix="/api/instagram/oauth", tags=["instagram-oauth"])

# Note the different hosts: authorization + short-lived token exchange use
# api.instagram.com, everything AFTER you have a token uses graph.instagram.com.
INSTAGRAM_OAUTH_DIALOG = "https://api.instagram.com/oauth/authorize"
INSTAGRAM_TOKEN_EXCHANGE = "https://api.instagram.com/oauth/access_token"
INSTAGRAM_LONG_LIVED_EXCHANGE = "https://graph.instagram.com/access_token"
INSTAGRAM_GRAPH = "https://graph.instagram.com/v21.0"

SCOPES = "instagram_business_basic,instagram_business_manage_messages"


def _make_state(user_id: str) -> str:
    settings = get_settings()
    return pyjwt.encode(
        {"user_id": user_id, "exp": int(time.time()) + 600},
        settings.OAUTH_STATE_SECRET,
        algorithm="HS256",
    )


def _read_state(state: str) -> str:
    settings = get_settings()
    try:
        payload = pyjwt.decode(state, settings.OAUTH_STATE_SECRET, algorithms=["HS256"])
    except pyjwt.PyJWTError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired OAuth state")
    return payload["user_id"]


@router.get("/start-url")
def get_start_url(user_id: str = Depends(verify_jwt_and_get_user_id)):
    settings = get_settings()
    state = _make_state(user_id)

    params = {
        "client_id": settings.FACEBOOK_APP_ID,  # Instagram App ID, same App Dashboard
        "redirect_uri": settings.INSTAGRAM_REDIRECT_URI,
        "state": state,
        "scope": SCOPES,
        "response_type": "code",
    }
    # urlencode handles percent-encoding correctly and is much clearer
    # than round-tripping through httpx.QueryParams one key at a time.
    return {"url": f"{INSTAGRAM_OAUTH_DIALOG}?{urlencode(params)}"}


@router.get("/callback")
def oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: Client = Depends(get_supabase_admin_client),
):
    settings = get_settings()
    user_id = _read_state(state)

    try:
        with httpx.Client(timeout=10) as client:
            # 1. Exchange the code for a short-lived token.
            token_res = client.post(
                INSTAGRAM_TOKEN_EXCHANGE,
                data={
                    "client_id": settings.FACEBOOK_APP_ID,
                    "client_secret": settings.FACEBOOK_APP_SECRET,
                    "grant_type": "authorization_code",
                    "redirect_uri": settings.INSTAGRAM_REDIRECT_URI,
                    "code": code,
                },
            )
            if token_res.status_code != 200:
                logger.error("Instagram token exchange failed: %s", token_res.text)
                return RedirectResponse(f"{settings.FRONTEND_URL}/dashboard?instagram=error")

            token_data = token_res.json()
            try:
                short_lived_token = token_data["access_token"]
                ig_account_id = str(token_data["user_id"])
            except KeyError:
                logger.error(
                    "Instagram token response missing expected fields: %s", token_data
                )
                return RedirectResponse(f"{settings.FRONTEND_URL}/dashboard?instagram=error")

            # 2. Exchange for a long-lived token (60 days instead of ~1 hour).
            long_res = client.get(
                INSTAGRAM_LONG_LIVED_EXCHANGE,
                params={
                    "grant_type": "ig_exchange_token",
                    "client_secret": settings.FACEBOOK_APP_SECRET,
                    "access_token": short_lived_token,
                },
            )
            if long_res.status_code != 200:
                # Don't silently fall back — a short-lived token expires in
                # ~1hr, so DM sends would start failing later with no clue
                # why. Fail loudly instead so it gets noticed and retried.
                logger.error(
                    "Instagram long-lived token exchange failed for user %s: %s",
                    user_id,
                    long_res.text,
                )
                return RedirectResponse(f"{settings.FRONTEND_URL}/dashboard?instagram=error")

            long_lived_token = long_res.json()["access_token"]

    except httpx.HTTPError:
        logger.exception("Network error during Instagram OAuth callback for user %s", user_id)
        return RedirectResponse(f"{settings.FRONTEND_URL}/dashboard?instagram=error")

    # 3. Check whether this Instagram account is already connected to a
    #    DIFFERENT user before saving.
    existing_res = (
        db.table("profiles")
        .select("id")
        .eq("instagram_account_id", ig_account_id)
        .neq("id", user_id)
        .limit(1)
        .execute()
    )
    if existing_res.data:
        logger.info(
            "Instagram account %s already connected to a different user (attempted by %s)",
            ig_account_id,
            user_id,
        )
        return RedirectResponse(
            f"{settings.FRONTEND_URL}/dashboard?instagram=already_connected"
        )

    # 4. Save the connection.
    #
    # NOTE: facebook_user_id is intentionally left untouched here — this
    # login flow never involves a Facebook account at all, so there's no
    # such ID to save. This means Meta's Data Deletion Callback (which
    # matches on facebook_user_id) won't find accounts connected through
    # this flow. Known gap to revisit before real users rely on it — the
    # in-app "Delete my account" button still works fine regardless.
    try:
        db.table("profiles").update(
            {
                "instagram_account_id": ig_account_id,
                "instagram_access_token": long_lived_token,
            }
        ).eq("id", user_id).execute()
    except Exception:
        logger.exception(
            "Failed to save Instagram connection for user %s (possible duplicate key)",
            user_id,
        )
        return RedirectResponse(
            f"{settings.FRONTEND_URL}/dashboard?instagram=already_connected"
        )

    return RedirectResponse(f"{settings.FRONTEND_URL}/dashboard?instagram=connected")


@router.delete("/disconnect", status_code=status.HTTP_200_OK)
def disconnect_instagram(
    user_id: str = Depends(verify_jwt_and_get_user_id),
    db: Client = Depends(get_supabase_admin_client),
):
    """
    Disconnects Instagram without touching campaigns or DM history.
    """
    db.table("profiles").update(
        {
            "instagram_account_id": None,
            "instagram_access_token": None,
        }
    ).eq("id", user_id).execute()

    return {"status": "disconnected"}


def send_instagram_dm(recipient_igsid: str, message_text: str, page_access_token: str) -> bool:
    """
    Sends a DM via the Instagram-native Send API (graph.instagram.com),
    using the Instagram access token saved during the OAuth callback above.
    `page_access_token` is a holdover parameter name from the old Facebook
    flow — with Instagram Login, it's really just "the Instagram account's
    own access token," kept as-is so app/main.py doesn't need changes.
    """
    try:
        with httpx.Client(timeout=10) as client:
            res = client.post(
                f"{INSTAGRAM_GRAPH}/me/messages",
                params={"access_token": page_access_token},
                json={
                    "recipient": {"id": recipient_igsid},
                    "message": {"text": message_text},
                },
            )
        if res.status_code != 200:
            logger.error(
                "Instagram DM send failed (status %s) to %s: %s",
                res.status_code,
                recipient_igsid,
                res.text,
            )
            return False
        return True
    except httpx.HTTPError:
        logger.exception("Network error sending Instagram DM to %s", recipient_igsid)
        return False


@router.get("/webhook")
def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    settings = get_settings()
    # Pulled from settings instead of hardcoded — keeps it out of source
    # control and lets it differ between dev/staging/prod.
    verify_token = settings.INSTAGRAM_WEBHOOK_VERIFY_TOKEN

    if hub_mode == "subscribe" and hub_verify_token == verify_token:
        logger.info("Webhook verified successfully!")
        return int(hub_challenge)

    raise HTTPException(status_code=403, detail="Verification token mismatch")


@router.post("/webhook")
async def receive_webhook_event(request: Request):
    """
    Real Instagram webhook delivery. Unpacks Meta's entry[].changes[]
    envelope and hands comment events to the same matching logic used
    by the /webhook simulation endpoint in main.py. Always returns 200
    quickly — Meta retries aggressively on non-2xx responses.
    """
    # Deferred import to avoid a circular import: main.py imports this
    # router at module load time, so importing main.py back at THIS
    # module's load time would deadlock. Importing inside the function
    # body only runs once a request actually comes in, by which point
    # both modules are already fully loaded.
    from app.main import _match_and_record
    from app.schemas import InstagramWebhookPayload
    from app.config import get_supabase_admin_client as _get_db

    body = await request.json()
    logger.info("Received Instagram Webhook Event: %s", body)

    db = _get_db()

    for entry in body.get("entry", []):
        recipient_account_id = entry.get("id")
        for change in entry.get("changes", []):
            # Only comment events matter for trigger-word matching — skip
            # everything else (message_seen, message reactions, etc.)
            if change.get("field") != "comments":
                continue

            value = change.get("value", {})
            comment_text = value.get("text")
            commenter_username = value.get("from", {}).get("username")
            commenter_igsid = value.get("from", {}).get("id")

            if not (recipient_account_id and comment_text and commenter_username):
                logger.warning("Skipping malformed webhook change: %s", change)
                continue

            try:
                payload = InstagramWebhookPayload(
                    commenter_username=commenter_username,
                    comment_text=comment_text,
                    recipient_account_id=recipient_account_id,
                    commenter_igsid=commenter_igsid,
                )
                result = _match_and_record(payload, db)
                logger.info("Instagram webhook processed: %s", result.status)
            except Exception:
                logger.exception("Failed to process Instagram webhook change")

    return {"status": "ok"}