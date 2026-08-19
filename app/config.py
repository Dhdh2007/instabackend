
# =============================================================================
# app/config.py
# -----------------------------------------------------------------------------
# Central application configuration and Supabase client.
# =============================================================================

import os
from functools import lru_cache

from dotenv import load_dotenv
from supabase import create_client, Client


# Load variables from .env
load_dotenv()


class Settings:
    """
    Central application settings.

    Environment variables are loaded once when this object is created.
    """

    # -------------------------------------------------------------------------
    # Supabase project URL
    # -------------------------------------------------------------------------

    SUPABASE_URL: str = os.environ["SUPABASE_URL"]
    OAUTH_STATE_SECRET : str = os.environ["OAUTH_STATE_SECRET"]
    FACEBOOK_APP_ID :str = os.environ["FACEBOOK_APP_ID"]
    FACEBOOK_APP_SECRET :str = os.environ["FACEBOOK_APP_SECRET"]
    INSTAGRAM_REDIRECT_URI :str = os.environ["INSTAGRAM_REDIRECT_URI"]

    # -------------------------------------------------------------------------
    # Supabase backend service-role key
    # -------------------------------------------------------------------------
    #
    # IMPORTANT:
    # This is backend-only.
    #
    # NEVER:
    # - put it in frontend code
    # - use NEXT_PUBLIC_
    # - commit it to Git
    #
    # It has elevated database privileges.
    #

    SUPABASE_SERVICE_ROLE_KEY: str = os.environ[
        "SUPABASE_SERVICE_ROLE_KEY"
    ]

    # -------------------------------------------------------------------------
    # Application settings
    # -------------------------------------------------------------------------

    FREE_TIER_DM_LIMIT: int = 100

    # -------------------------------------------------------------------------
    # Instagram
    # -------------------------------------------------------------------------

    INSTAGRAM_WEBHOOK_VERIFY_TOKEN: str = os.environ[
        "INSTAGRAM_WEBHOOK_VERIFY_TOKEN"
    ]

    # -------------------------------------------------------------------------
    # Frontend
    # -------------------------------------------------------------------------

    FRONTEND_URL: str = os.environ.get(
        "FRONTEND_URL",
        "http://localhost:3000",
    )


@lru_cache
def get_settings() -> Settings:
    """
    Create the settings object once and reuse it.
    """

    return Settings()


@lru_cache
def get_supabase_admin_client() -> Client:
    """
    Create one backend Supabase client.

    This client uses the service-role key and must remain
    on the backend only.
    """

    settings = get_settings()

    return create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_ROLE_KEY,
    )

