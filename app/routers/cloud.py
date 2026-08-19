
import os
import httpx

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.supabase_client import supabase


router = APIRouter()
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")


# Signup request data
class Signup(BaseModel):
    email: str
    password: str
    turnstile_token: str


@router.post("/signup")
async def signup(data: Signup):

    # ==========================================
    # 1. VERIFY CLOUDFLARE TURNSTILE
    # ==========================================

    async with httpx.AsyncClient() as client:

        response = await client.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={
                "secret": os.getenv("TURNSTILE_SECRET_KEY"),
                "response": data.turnstile_token,
            },
        )

    # Convert Cloudflare response to JSON
    result = response.json()

    print("CLOUDFLARE RESPONSE:")
    print(result)


    # ==========================================
    # 2. CHECK TURNSTILE RESULT
    # ==========================================

    if not result.get("success"):

        raise HTTPException(
            status_code=400,
            detail={
                "message": "Turnstile verification failed",
                "cloudflare_response": result,
            },
        )


    # ==========================================
    # 3. CREATE SUPABASE ACCOUNT
    # ==========================================

    try:

        signup_response = supabase.auth.sign_up(
            {
                "email": data.email,
                "password": data.password,
                "options": { 
                    "email_redirect_to": f"{BACKEND_URL}/auth"
                }

            }
        )


        # ==========================================
        # 4. PRINT SUPABASE RESPONSE
        # ==========================================

        print("====================================")
        print("SUPABASE SIGNUP RESPONSE:")
        print(signup_response)
        print("====================================")


        # ==========================================
        # 5. RETURN SUCCESS
        # ==========================================

        return {
            "message": "Account created successfully. Please verify your email."
        }


    # ==========================================
    # 6. CATCH SUPABASE ERROR
    # ==========================================

    except Exception as e:

        print("====================================")
        print("SUPABASE SIGNUP ERROR:")
        print("ERROR TYPE:", type(e))
        print("ERROR REPR:", repr(e))
        print("ERROR MESSAGE:", str(e))
        print("====================================")


        raise HTTPException(
            status_code=400,
            detail=str(e),
        )

