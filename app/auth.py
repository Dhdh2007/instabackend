
# =============================================================================
# app/auth.py
# -----------------------------------------------------------------------------
# Supabase JWT verification using the Supabase Python client.
# =============================================================================

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.config import get_supabase_admin_client


# -----------------------------------------------------------------------------
# FastAPI Bearer authentication
# -----------------------------------------------------------------------------

bearer_scheme = HTTPBearer()


# -----------------------------------------------------------------------------
# Verify Supabase JWT and return user ID
# -----------------------------------------------------------------------------

def verify_jwt_and_get_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(
        bearer_scheme
    ),
) -> str:
    """
    Verify a Supabase Auth access token and return the user's Supabase ID.

    Supabase's get_claims() method verifies the JWT against the project's
    JWKS endpoint when using asymmetric signing keys.
    """

    # -------------------------------------------------------------------------
    # 1. Get the JWT from:
    #
    # Authorization: Bearer <JWT>
    # -------------------------------------------------------------------------

    token = credentials.credentials

    # -------------------------------------------------------------------------
    # 2. Get the existing Supabase backend client.
    # -------------------------------------------------------------------------

    supabase = get_supabase_admin_client()

    try:
        result = supabase.auth.get_claims(token)
    
        # ---------------------------------------------------------------------
        # 3. Verify the JWT and get its claims.
        #
        # Supabase handles the JWT verification.
        # ---------------------------------------------------------------------

       
       

    except Exception as e:
        print("SUPABASE JWT ERROR:", repr(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Supabase JWT.",
        )

    # -------------------------------------------------------------------------
    # 5. Get the authenticated Supabase user ID.
    #
    # "sub" = subject = user's Supabase Auth UUID.
    # -------------------------------------------------------------------------

    claims = result.get("claims", {})
    user_id = claims.get("sub")
    print("get_claims result keys:", list(result.keys()))
    print("result:", result)
    
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="JWT does not contain a user ID.",
        )

    # -------------------------------------------------------------------------
    # 6. Return the verified user ID.
    # -------------------------------------------------------------------------

    return user_id

def verify_owner(
    business_id: str,
    user_id: str = Depends(verify_jwt_and_get_user_id),
) -> str:
    """
    Route-level dependency for endpoints shaped like /insights/{business_id}.
    `business_id` is filled in from the URL automatically (FastAPI matches
    it by parameter name); `user_id` comes from the verified JWT above.
    401s if the JWT is bad, 403s if it's valid but for someone else's data.
    """
    if user_id != business_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized for this business.",
        )
 
    return business_id