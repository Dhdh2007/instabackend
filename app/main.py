# app/main.py
import base64
import hashlib
import hmac
import json
import logging
import secrets
import httpx
from fastapi import FastAPI, Depends, Header, HTTPException, Query, Response, status, Request
from fastapi.middleware.cors import CORSMiddleware
from supabase import Client

from app.auth import verify_jwt_and_get_user_id
from app.config import get_settings, get_supabase_admin_client
from app.instagram_oauth import router as instagram_oauth_router, send_instagram_dm
from app.routers.dash import router as dashboard_router  # IMPORT NEW ROUTER
from app.schemas import InstagramWebhookPayload, WebhookResult
from app.routers.cloud import router as auth_router
from app.routers.emai import router as op
from app.routers.ab import router as ai_insights_router
from app.instagram_messaging import handle_messaging_event
from app.routers.memory_store import store as memory_store
from app.insight import router as b
from app.ab import router as c
logger = logging.getLogger("dm_trigger_bot")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="DM Trigger Bot API", version="1.0.0")

# CORS Setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://instabot-sand.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Connect Routers
app.include_router(b)
app.include_router(instagram_oauth_router)
app.include_router(dashboard_router)  # ATTACH DASHBOARD ROUTER HERE
app.include_router(
    auth_router,
   
    tags=["Authentication"]
)
app.include_router(c)
app.include_router(op)
app.include_router(ai_insights_router, prefix="/api", tags=["ai-insights"])
@app.get("/")
def root_check():
    return {"message": "DM Trigger Bot API is live on Render! 🚀"}


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/me")
def read_me(user=Depends(verify_jwt_and_get_user_id)):
    return {"user": user}
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error."},
    )

def _match_and_record(payload: InstagramWebhookPayload, db: Client) -> WebhookResult:
    """
    Shared matching logic used by both webhook endpoints below:
      1. Whichidjdhfbffh of our users owns the Instagram account the comment landed on?
      2. Do any of that user's ACTIVE campaigns have a trigger word contained
         in the comment text?
      3. If so, atomically increment their DM counter + log the event via
         the record_dm_sent() Postgres function (see sql/002_analytics.sql) —
         this is what enforces the free-tier limit race-condition-safely.
    """
    settings = get_settings()

    profile_res = (
        db.table("profiles")
        .select("id")
        .eq("instagram_account_id", payload.recipient_account_id)
        .maybe_single()
        .execute()
    )
    if not profile_res.data:
        return WebhookResult(status="ignored_unknown_account")
    user_id = profile_res.data["id"]
    page_access_token = profile_res.data.get("instagram_access_token")

   
    campaigns_res = (
        db.table("campaigns")
        .select("*")
        .eq("user_id", user_id)
        .eq("is_active", True)
        .execute()
    )
    comment_lower = payload.comment_text.lower()
    matched = next(
        (c for c in campaigns_res.data if c["trigger_word"].lower() in comment_lower),
        None,
    )
    if not matched:
        return WebhookResult(status="no_trigger_matched")

    rpc_res = db.rpc(
        "record_dm_sent",
        {
            "p_user_id": user_id,
            "p_campaign_id": matched["id"],
            "p_trigger_word": matched["trigger_word"],
            "p_commenter_username": payload.commenter_username,
            "p_limit": settings.FREE_TIER_DM_LIMIT,
        },
    ).execute()
    new_count = rpc_res.data

    if new_count is None:
        return WebhookResult(
            status="limit_reached", matched_trigger=matched["trigger_word"]
        )
    if payload.commenter_igsid and page_access_token:
        message_text = matched.get("message_template") or (
            f"Hey! 👋 Here's the link you asked for: {matched['destination_link']}"
        )
        delivered = send_instagram_dm(
            recipient_igsid=payload.commenter_igsid,
            message_text=message_text,
            page_access_token=page_access_token,
        )
        dm_delivery = "sent" if delivered else "failed"
    else:
        dm_delivery = "skipped_no_recipient_id"

    return WebhookResult(
        status="dm_sent",
        matched_trigger=matched["trigger_word"],
        dm_sent_to=payload.commenter_username,
        dms_sent_count=new_count,
        dm_delivery=dm_delivery,
    )

    


@app.post("/webhook", response_model=WebhookResult)
def simulated_webhook(
    payload: InstagramWebhookPayload,
    x_webhook_secret: str = Header(..., alias="X-Webhook-Secret"),
    db: Client = Depends(get_supabase_admin_client),
):
    """
    Simulated Instagram comment webhook — takes the simplified payload
    from app/schemas.py (commenter_username, comment_text,
    recipient_account_id) rather than Meta's real webhook envelope, per
    the note on InstagramWebhookPayload. Protected by a shared secret
    header instead of Meta's X-Hub-Signature-256 since there's no real
    Meta request to verify here.
    """
    settings = get_settings()
    if x_webhook_secret != settings.WEBHOOK_SECRET:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook secret")

    return _match_and_record(payload, db)


@app.get("/api/webhook/instagram")
def verify_instagram_webhook(
    hub_mode: str = Query(..., alias="hub.mode"),
    hub_verify_token: str = Query(..., alias="hub.verify_token"),
    hub_challenge: str = Query(..., alias="hub.challenge"),
):
    """
    Meta calls this once, with a GET request, when you register the
    webhook URL in the Facebook App dashboard — it must echo back
    hub.challenge if hub.verify_token matches what you configured there,
    or Meta refuses to save the subscription.
    """
    settings = get_settings()
    if hub_mode == "subscribe" and hub_verify_token == settings.WEBHOOK_SECRET:
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Verification token mismatch")


@app.post("/api/webhook/instagram")
async def receive_instagram_webhook(
    request: Request,
    db: Client = Depends(get_supabase_admin_client),
):
    """
    The real Instagram/Facebook webhook delivery for a new comment. Meta's
    payload is a nested `entry[].changes[]` envelope rather than the flat
    shape our matching logic expects, so we unpack the fields we need and
    hand them to the same _match_and_record() used by the simulated
    endpoint above. Always returns 200 quickly — Meta retries aggressively
    (and can disable the subscription) if a delivery doesn't get a fast
    2xx, so any per-event problem is logged rather than raised.
    """
    body = await request.json()

    for entry in body.get("entry", []):
        recipient_account_id = entry.get("id")

        for change in entry.get("changes", []):
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

        ### NEW — handle actual DM replies (the "tree price" step) ###
        for messaging_event in entry.get("messaging", []):
            try:
                await handle_messaging_event(messaging_event, db, memory_store)
            except Exception:
                logger.exception("Failed to process Instagram messaging event")
        ### END NEW ###

    return {"status": "ok"}



def _base64url_decode(data: str) -> bytes:
    # Meta's signed_request uses base64url without padding — Python's
    # base64 module requires padding to be present, so we add it back.
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


@app.post("/api/data-deletion")
async def facebook_data_deletion_callback(request: Request):
    """
    Meta's Data Deletion Callback — a required field in the App Dashboard
    for any app requesting user data permissions (Settings > Basic > Data
    Deletion). Meta calls THIS, not the user, when someone removes your
    app from Facebook's "Apps and Websites" settings directly — meaning
    this is the only guaranteed way to hear about that removal at all.

    The request body is a single `signed_request` field: two base64url
    segments separated by a dot — a signature, and a JSON payload. We
    must verify the signature ourselves (HMAC-SHA256 using OUR app
    secret) before trusting anything in it, or anyone could POST a fake
    deletion request for someone else's account.

    Meta requires the response to contain a status page URL and a
    confirmation code, which it may show to the user.
    """
    settings = get_settings()
    db = get_supabase_admin_client()

    form = await request.form()
    signed_request = form.get("signed_request")
    if not signed_request or "." not in signed_request:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing or malformed signed_request")

    encoded_sig, payload_b64 = signed_request.split(".", 1)

    try:
        sig = _base64url_decode(encoded_sig)
        payload = json.loads(_base64url_decode(payload_b64))
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed signed_request")

    expected_sig = hmac.new(
        settings.FACEBOOK_APP_SECRET.encode(), payload_b64.encode(), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(sig, expected_sig):
        # Wrong secret, or forged — refuse rather than silently no-op, so a
        # bad signature shows up in logs instead of failing invisibly.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid signed_request signature")

    facebook_user_id = payload.get("user_id")
    confirmation_code = secrets.token_hex(8)

    if facebook_user_id:
        profile_res = (
            db.table("profiles")
            .select("id")
            .eq("facebook_user_id", facebook_user_id)
            .maybe_single()
            .execute()
        )
        if profile_res.data:
            user_id = profile_res.data["id"]
            db.table("dm_events").delete().eq("user_id", user_id).execute()
            db.table("campaigns").delete().eq("user_id", user_id).execute()
            db.table("profiles").delete().eq("id", user_id).execute()
            try:
                db.auth.admin.delete_user(user_id)
            except Exception:
                logger.exception(
                    "Data-deletion callback: failed to remove auth user %s", user_id
                )
        else:
            logger.info(
                "Data-deletion callback for unknown facebook_user_id %s (no matching profile)",
                facebook_user_id,
            )

    # Meta shows the user this URL if they check on the deletion's status.
    return {
        "url": f"{settings.FRONTEND_URL}/data-deletion-status?id={confirmation_code}",
        "confirmation_code": confirmation_code,
    }
