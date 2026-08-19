"""
Mount this in your existing FastAPI app:

    from app.router import router as ai_insights_router
    app.include_router(ai_insights_router, prefix="/api", tags=["ai-insights"])

Endpoints:
    PUT  /api/memory-box/{business_id}          create/update the Memory Box
    GET  /api/memory-box/{business_id}          fetch the current Memory Box
    POST /api/insights/{business_id}/analyze    run analysis over supplied conversations
    GET  /api/insights/{business_id}/dashboard  latest insights, dashboard-shaped
    GET  /api/insights/{business_id}/history     recent insight runs
"""

from __future__ import annotations
from datetime import datetime

from fastapi import APIRouter, HTTPException

from app.schemas import (
    AnalyzeRequest,
    BusinessMemoryBox,
    BusinessMemoryBoxUpdate,
    InsightsResult,
    MemoryMessageRequest,
    MemoryMessageResponse,
)
from .memory_store import store
from .insights_engine import generate_insights, extract_facts_from_message, merge_extracted_facts
from .nvidia_client import NvidiaAPIError

router = APIRouter()


# ---------------------------------------------------------------------------
# Business Memory Box — owner-controlled facts
# ---------------------------------------------------------------------------

@router.get("/memory-box/{business_id}", response_model=BusinessMemoryBox)
def get_memory_box(business_id: str):
    box = store.get_memory_box(business_id)
    if not box:
        raise HTTPException(status_code=404, detail="Memory Box not found for this business")
    return box


@router.put("/memory-box/{business_id}", response_model=BusinessMemoryBox)
def upsert_memory_box(business_id: str, update: BusinessMemoryBoxUpdate):
    """
    Create the Memory Box if it doesn't exist, or merge the supplied fields
    into the existing one. Only owner-supplied data ever lands here — this
    endpoint is the single point of truth the AI is grounded against.
    """
    existing = store.get_memory_box(business_id)

    if existing is None:
        if not update.business_name:
            raise HTTPException(
                status_code=400,
                detail="business_name is required when creating a Memory Box for the first time",
            )
        box = BusinessMemoryBox(business_id=business_id, business_name=update.business_name)
    else:
        box = existing

    update_data = update.model_dump(exclude_unset=True)
    merged_data = {**box.model_dump(), **update_data}
    merged_data["updated_at"] = datetime.utcnow()
    merged = BusinessMemoryBox(**merged_data)

    store.save_memory_box(merged)
    return merged


@router.post("/memory-box/{business_id}/message", response_model=MemoryMessageResponse)
def send_memory_message(business_id: str, payload: MemoryMessageRequest):
    """
    The 'message box' endpoint: the owner types a free-text note ('our
    return window is 30 days', 'we're trying to sell more of the Retinol
    Cream'), and it gets folded into the Memory Box. Only facts explicitly
    present in the text are extracted — nothing is invented — and list
    fields (offers, FAQs, products, etc.) are merged additively rather than
    overwritten, so one message never erases previously stored facts.
    """
    existing = store.get_memory_box(business_id)
    if not existing:
        raise HTTPException(
            status_code=400,
            detail="No Memory Box found for this business yet. Create one first "
                   "(a name is enough) via PUT /memory-box/{business_id}.",
        )

    try:
        extracted, model_used = extract_facts_from_message(payload.text)
    except NvidiaAPIError as e:
        raise HTTPException(status_code=503, detail=f"AI extraction unavailable: {e}")

    updated_box = merge_extracted_facts(existing, extracted)
    store.save_memory_box(updated_box)

    return MemoryMessageResponse(
        memory_box=updated_box,
        extracted_fields=extracted,
        model_used=model_used,
        raw_note=payload.text,
    )


# ---------------------------------------------------------------------------
# AI Insights — generated strictly from the Memory Box + supplied conversations
# ---------------------------------------------------------------------------

@router.post("/insights/{business_id}/analyze", response_model=InsightsResult)
def analyze(business_id: str, payload: AnalyzeRequest):
    memory_box = store.get_memory_box(business_id)
    if not memory_box:
        raise HTTPException(
            status_code=400,
            detail="No Memory Box found for this business. Set one up with PUT /memory-box/{business_id} first.",
        )

    result = generate_insights(memory_box, payload.conversations)
    store.save_insights(result)
    return result


@router.get("/insights/{business_id}/dashboard")
def dashboard(business_id: str):
    """
    Shaped for direct rendering in an 'AI Insights' dashboard tab: key
    discoveries, recommended actions, product opportunities, customer
    questions, sales opportunities, and trends — in plain language.
    """
    latest = store.get_latest_insights(business_id)
    if not latest:
        return {
            "has_data": False,
            "message": "No insights generated yet. Run an analysis first.",
        }

    return {
        "has_data": True,
        "generated_at": latest.generated_at,
        "model_used": latest.model_used,
        "key_discoveries": {
            "summary": latest.summary,
            "sentiment_overview": latest.sentiment_overview,
            "trending_topics": latest.trending_topics,
        },
        "recommended_actions": latest.recommended_actions,
        "product_opportunities": latest.product_opportunities,
        "top_products_by_mentions": [
            {"product": m.product_name, "mentions": m.mention_count}
            for m in latest.computed_product_mentions[:10]
        ],
        "customer_questions": latest.common_questions,
        "objections": latest.objections,
        "why_customers_arent_buying": latest.why_customers_arent_buying,
        "what_customers_want": latest.what_customers_want,
        "sales_opportunities": latest.sales_opportunities,
        "leads": {
            "hot": [c for c in latest.customer_intents if c.intent == "hot"],
            "warm": [c for c in latest.customer_intents if c.intent == "warm"],
            "cold": [c for c in latest.customer_intents if c.intent == "cold"],
        },
    }


@router.get("/insights/{business_id}/history", response_model=list[InsightsResult])
def history(business_id: str, limit: int = 10):
    return store.get_insights_history(business_id, limit=limit)
