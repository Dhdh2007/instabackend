import os

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.supabase_client import supabase


# This is an ALTERNATE verification path — use it only if your Supabase
# email template sends a link (uses {{ .ConfirmationURL }}) instead of
# a bare code (uses {{ .Token }}).
#
# The link Supabase sends looks like:
#   https://yourapi.com/auth/confirm?token_hash=xxxxx&type=signup
#
# The user clicks it in their email client — there is no form, no code
# to type. This route runs, verifies, and redirects them straight in.

router = APIRouter()

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")


@router.get("/auth/confirm")
async def confirm_email(request: Request):

    token_hash = request.query_params.get("token_hash")
    otp_type = request.query_params.get("type", "signup")

    # ==========================================
    # 1. MISSING PARAMS -> BACK TO SIGNUP
    # ==========================================
    if not token_hash:
        return RedirectResponse(
            url=f"{FRONTEND_URL}/signup?error=missing_token"
        )

    # ==========================================
    # 2. VERIFY THE LINK'S TOKEN_HASH
    # ==========================================
    try:
        response = supabase.auth.verify_otp(
            {
                "token_hash": token_hash,
                "type": otp_type,
            }
        )

        

        session = response.session

        if not session:
            return RedirectResponse(
                url=f"{FRONTEND_URL}/signup?error=invalid_or_expired"
            )

        # ==========================================
        # 3. SUCCESS -> REDIRECT TO DASHBOARD
        # ==========================================
        # Session tokens go in the redirect URL's hash fragment so the
        # frontend can pick them up and store them (e.g. in a small
        # useEffect on the dashboard page that reads window.location.hash).
        # Fragments aren't sent to servers, so this keeps tokens out of
        # your logs.

        redirect_url = (
            f"{FRONTEND_URL}/dashboard"
            f"#access_token={session.access_token}"
            f"&refresh_token={session.refresh_token}"
        )

        return RedirectResponse(url=redirect_url)

    # ==========================================
    # 4. EXPIRED / INVALID LINK
    # ==========================================
    except Exception as e:
        print("LINK VERIFY ERROR:", repr(e))
        return RedirectResponse(
            url=f"{FRONTEND_URL}/signup?error=invalid_or_expired"
        )