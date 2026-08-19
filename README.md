# DM Trigger Bot — Backend

FastAPI backend for the DM Trigger Bot micro-SaaS. Handles JWT-verified
campaign management and a simulated Instagram webhook with a hard,
race-condition-safe free-tier limit of 100 DMs/user.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then fill in your real Supabase values
```

1. Go to your Supabase project → SQL Editor → paste the contents of
   `sql/schema.sql` → Run. This creates the tables, RLS policies, the
   auto-profile trigger, and the atomic `increment_dm_count` function.
2. Paste and run `sql/002_analytics.sql` too — it adds the `dm_events`
   log table and `record_dm_sent`, the function the webhook now calls
   instead of `increment_dm_count` (same atomicity guarantee, plus it
   logs each send for the dashboard's analytics endpoint).
3. Go to Project Settings → API and copy `Project URL` and the
   `service_role` secret key into `.env`.
4. Run the server:

```bash
uvicorn app.main:app --reload --port 8000
```

4. Open `http://localhost:8000/docs` — FastAPI's interactive Swagger UI.
   You can test protected endpoints by pasting a real Supabase session
   JWT (grab it from `supabase.auth.getSession()` on your Next.js
   frontend after logging in) into the "Authorize" button.

## Testing the webhook without a real Instagram connection

```bash
# First, manually set an instagram_account_id on your profile row in the
# Supabase Table Editor, e.g. "ig_test_123", then:

curl -X POST http://localhost:8000/api/webhook/instagram \
  -H "Content-Type: application/json" \
  -d '{
    "commenter_username": "test_user",
    "comment_text": "send me the PRICE please!",
    "recipient_account_id": "ig_test_123"
  }'
```

Run it 100+ times in a row and you'll see it flip from `"status":
"dm_sent"` to a `403 Free tier limit of 100 DMs reached`.

## Note on webhook authenticity

This assignment specifies the webhook as a *simulation*, so
`/api/webhook/instagram` has no auth on it. In a real integration, Meta
signs every webhook request with an `X-Hub-Signature-256` header (an
HMAC of the raw body using your app secret). You'd verify that signature
— not a JWT — before trusting the payload, since the request is coming
from Meta's servers, not a logged-in browser session.
