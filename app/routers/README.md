# AI Insights — Business Memory Box for Instagram DM SaaS

## What this is

- **Business Memory Box** (`app/schemas.py: BusinessMemoryBox`) — the *only*
  source of business facts (products, prices, policies, FAQs, rules, etc).
  Owner-provided, updatable any time via `PUT /api/memory-box/{business_id}`.
- **Insights engine** (`app/insights_engine.py`) — analyzes DMs/comments
  *grounded in* the Memory Box. Deterministic stats (e.g. product mention
  counts) are computed in plain Python, never guessed by the model. The
  system prompt explicitly forbids inventing prices/policies/facts not in
  the Memory Box.
- **NVIDIA NIM client** (`app/nvidia_client.py`) — calls NVIDIA's free
  hosted inference API (OpenAI-compatible) with automatic fallback across a
  list of models: if model 1 errors out or is rate-limited, it tries model
  2, then 3, etc., until one succeeds.
- **Dashboard endpoint** (`GET /api/insights/{business_id}/dashboard`) —
  pre-shaped JSON for a simple "AI Insights" tab: key discoveries,
  recommended actions, product opportunities, customer questions, sales
  opportunities, trends, and hot/warm/cold leads.

## Setup

```bash
pip install -r requirements.txt
```

Get a **free** NVIDIA API key at https://build.nvidia.com (sign in → API Keys),
then:

```bash
export NVIDIA_API_KEY="nvapi-xxxxxxxx"
uvicorn main:app --reload
```

## Fixing your original snippet

Your original code had a few bugs worth flagging directly:
- `os.getenv("dhr")` — reads an env var literally named `dhr`. Almost
  certainly meant to be your actual key's env var name, e.g. `NVIDIA_API_KEY`.
- The URL was a **Gemini** endpoint (`generativelanguage.googleapis.com`)
  but the header was `X-goog-api-key` mixed with an f-string placeholder
  `{api_key}` that was never filled in — that call would have failed before
  it hit the network-auth issue.
- No fallback logic despite wanting "if one fails, try another" — that's
  what `nvidia_client.chat_with_fallback` / `chat_json_with_fallback` do.
- Hardcoded prompt ("Analyze my Instagram engagement...") instead of one
  grounded in real stored business data — that's what the Memory Box +
  `build_user_prompt` fix.

## Example flow

```bash
# 1. Owner sets up their Memory Box (do this once, update any time)
curl -X PUT http://localhost:8000/api/memory-box/biz1 \
  -H "Content-Type: application/json" \
  -d '{
    "business_name": "Glow Skincare",
    "products": [
      {"name": "Vitamin C Serum", "price": "$25", "is_best_seller": true},
      {"name": "Retinol Night Cream", "price": "$32", "wants_to_sell_more": true}
    ],
    "target_customers": "Women 20-40 interested in clean skincare",
    "brand_voice": "Friendly, warm, no hard selling",
    "shipping_policy": "3-5 business days, free over $50",
    "return_policy": "30-day returns, unopened items only",
    "faqs": [{"question": "Is it cruelty-free?", "answer": "Yes, always."}],
    "business_rules": ["Never promise same-day delivery"]
  }'

# 2. Feed in real DM/comment data and get insights
curl -X POST http://localhost:8000/api/insights/biz1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "conversations": [
      {
        "customer_id": "u123",
        "source": "dm",
        "messages": [
          {"role": "customer", "text": "does the vitamin c serum work for dark spots? also is it expensive?"},
          {"role": "business", "text": "Yes! It'\''s $25 and great for dark spots."},
          {"role": "customer", "text": "hmm ok let me think about it, whats your return policy"}
        ]
      }
    ]
  }'

# 3. Render the dashboard
curl http://localhost:8000/api/insights/biz1/dashboard
```

## Swapping the default model chain

Edit `DEFAULT_MODEL_FALLBACK_CHAIN` in `app/nvidia_client.py`, or pass a
custom `models=[...]` list into `chat_with_fallback` / `chat_json_with_fallback`
per call. Any model listed at https://build.nvidia.com/models works, as long
as it's chat-completion compatible.

## Storage

`app/memory_store.py` ships a JSON-file-backed `JSONFileStore` so this runs
with zero infra. For production, implement the same `MemoryStore` interface
against Postgres/Mongo/whatever your SaaS already uses, and swap the `store`
singleton — nothing in `router.py` or `insights_engine.py` needs to change.

## Notes on the "never invent facts" guarantee

This is enforced two ways, not just via prompt instruction:
1. Everything computable in code (product mention counts) is computed in
   code and returned as `computed_product_mentions`, tagged `source: "computed"`.
2. The system prompt for the LLM explicitly instructs it to say "not
   provided in Business Memory Box" rather than fabricate anything, and the
   only business data it ever sees is what's in the Memory Box — it has no
   other channel to "know" facts about the business.

No prompt-based guarantee is 100% airtight with any LLM — for anything
customer-facing (e.g. auto-replying with a price), you should still validate
that the value quoted actually exists verbatim in the Memory Box before
sending it to a customer.
