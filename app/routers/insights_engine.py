"""
This is where the actual "AI Insights" logic lives.

Design principle: anything that CAN be computed deterministically (product
mention counts, message counts, etc.) IS computed in Python, not asked of
the LLM. The LLM is only used for the parts that genuinely need language
understanding (intent, sentiment, summarization, recommendations) — and
even there, it is given the Business Memory Box as its ONLY source of truth
about the business, with an explicit instruction to never invent facts.
"""

from __future__ import annotations
import json
from datetime import datetime

from app.schemas import (
    BusinessMemoryBox,
    Conversation,
    InsightsResult,
    ProductMentionStat,
)
from .nvidia_client import chat_json_with_fallback, NvidiaAPIError


# ---------------------------------------------------------------------------
# Deterministic, non-AI computations
# ---------------------------------------------------------------------------

def compute_product_mentions(
    memory_box: BusinessMemoryBox, conversations: list[Conversation]
) -> list[ProductMentionStat]:
    all_items = [p.name for p in memory_box.products] + [s.name for s in memory_box.services]
    counts = {name: 0 for name in all_items}

    for convo in conversations:
        for msg in convo.messages:
            if msg.role != "customer":
                continue
            text_lower = msg.text.lower()
            for name in all_items:
                if name.lower() in text_lower:
                    counts[name] += 1

    return [
        ProductMentionStat(product_name=name, mention_count=count)
        for name, count in sorted(counts.items(), key=lambda kv: -kv[1])
    ]


# ---------------------------------------------------------------------------
# Grounding context — this is the ONLY business information the model sees.
# ---------------------------------------------------------------------------

def _fmt_list(items: list[str]) -> str:
    return "\n".join(f"  - {i}" for i in items) if items else "  (none provided)"


def build_grounding_context(memory_box: BusinessMemoryBox) -> str:
    products = "\n".join(
        f"  - {p.name} | price: {p.price or 'not provided'} | "
        f"category: {p.category or 'n/a'} | best seller: {p.is_best_seller} | "
        f"wants to sell more: {p.wants_to_sell_more}"
        for p in memory_box.products
    ) or "  (none provided)"

    services = "\n".join(
        f"  - {s.name} | price: {s.price or 'not provided'}"
        for s in memory_box.services
    ) or "  (none provided)"

    faqs = "\n".join(f"  - Q: {f.question} | A: {f.answer}" for f in memory_box.faqs) or "  (none provided)"

    return f"""
BUSINESS NAME: {memory_box.business_name}

PRODUCTS:
{products}

SERVICES:
{services}

TARGET CUSTOMERS: {memory_box.target_customers or "not provided"}
BRAND VOICE: {memory_box.brand_voice or "not provided"}

OFFERS:
{_fmt_list(memory_box.offers)}
DISCOUNTS:
{_fmt_list(memory_box.discounts)}
PROMOTIONS:
{_fmt_list(memory_box.promotions)}

FAQS:
{faqs}

SHIPPING POLICY: {memory_box.shipping_policy or "not provided"}
RETURN POLICY: {memory_box.return_policy or "not provided"}

COMPETITORS:
{_fmt_list(memory_box.competitors)}

BUSINESS GOALS:
{_fmt_list(memory_box.goals)}

KNOWN CUSTOMER PAIN POINTS (owner-reported):
{_fmt_list(memory_box.customer_pain_points)}

COMMON QUESTIONS (owner-reported):
{_fmt_list(memory_box.common_questions)}

KEYWORDS:
{_fmt_list(memory_box.keywords)}

BUSINESS RULES (must always be respected):
{_fmt_list(memory_box.business_rules)}
""".strip()


def _fmt_conversations(conversations: list[Conversation]) -> str:
    blocks = []
    for c in conversations:
        lines = [f"[{m.role}]: {m.text}" for m in c.messages]
        blocks.append(f"--- Conversation with customer {c.customer_id} ({c.source}) ---\n" + "\n".join(lines))
    return "\n\n".join(blocks) if blocks else "(no conversations supplied)"


SYSTEM_PROMPT = """You are an analytics engine for an Instagram DM automation SaaS.

You will be given:
1. A BUSINESS MEMORY BOX — the complete, authoritative set of facts about this
   business (products, pricing, policies, FAQs, rules, etc).
2. A set of real customer DM/comment conversations.

Your job is to analyze the conversations THROUGH THE LENS of the Business
Memory Box and produce structured insights for the business owner.

ABSOLUTE RULES:
- NEVER invent, guess, or assume a price, policy, product, service, or any
  business fact that is not explicitly present in the Business Memory Box.
  If something relevant is missing from the Memory Box, say so explicitly
  (e.g. "Customers are asking about return windows, but no return policy is
  stored in the Memory Box yet.") instead of making one up.
- Base customer intent, sentiment, objections, and recommendations ONLY on
  what appears in the supplied conversations plus the Memory Box context.
- Do not fabricate customers, conversations, or statistics that weren't given
  to you.
- customer_intents MUST contain one entry per customer_id that actually
  appears in the supplied conversations — copy each customer_id EXACTLY as
  given, never invent, alter, or abbreviate one. Do not include a
  customer_id that wasn't in the conversations you were given.
- "intent" must be exactly one of: hot, warm, cold — no other value.
- "lead_score" must be a plain integer from 0 to 100 — no strings, no
  decimals, no percent signs.

INTENT RUBRIC — apply this consistently, don't invent your own criteria:
- "hot" (lead_score roughly 70-100): the customer explicitly signaled they
  want to buy now — asked how/where to pay, asked for a checkout or order
  link, said something like "I'll take it" / "how do I order", or asked a
  purely logistical question (delivery time, stock availability) about a
  purchase they've clearly already decided to make.
- "warm" (lead_score roughly 30-69): the customer showed real interest —
  asked about a specific product/price/feature, compared options, or raised
  a question or objection that hasn't been resolved — but did not commit.
- "cold" (lead_score roughly 0-29): generic browsing, a one-off question
  with no follow-up, a complaint unrelated to purchasing, or a message that
  gives no signal of purchase interest at all.
Base the score on what THIS customer actually said, not on how good the
product sounds — a customer who never engaged after one price question is
cold even if the product is great.

FIELD DEFINITIONS — each of the fields below answers a different question.
Do not repeat the same observation across multiple fields; assign it to
the single field it fits best:
- "objections": specific stated reservations tied to an identifiable
  customer/message (e.g. "customer_2 said the price felt too high").
- "why_customers_arent_buying": the AGGREGATE pattern across multiple
  customers, not restating individual objections one by one (e.g.
  "several customers stalled after asking about returns, and no return
  policy is on file to answer them").
- "what_customers_want": features, products, or policies customers are
  asking for — especially ones the Memory Box shows the business doesn't
  currently offer or document.
- "trending_topics": recurring subjects/keywords across the conversations,
  regardless of whether they relate to a sale.
- "sales_opportunities": concrete, near-term actions tied to a SPECIFIC
  customer or message ("follow up with customer_4 about the bulk order
  they asked about").
- "recommended_actions": actions for the OWNER'S BUSINESS ITSELF, not tied
  to one customer (e.g. "add a stated return policy to the Memory Box" or
  "reply to the unanswered DM from customer_1").
- "product_opportunities": gaps in the product/service lineup revealed by
  what customers asked for, that the Memory Box shows isn't currently sold.

- If no conversations were supplied, say so plainly in "summary" and return
  empty arrays/objects for every per-customer field rather than guessing.
- Respond with ONLY a single JSON object. The first character of your
  entire response must be "{" and the last character must be "}" — no
  markdown fences, no preamble, no closing remarks, no commentary before
  or after the JSON, matching exactly this schema:

{
  "summary": "string - 2-4 sentence plain-language overview",
  "sentiment_overview": "string - overall customer sentiment in plain language",
  "customer_intents": [
    {"customer_id": "string", "intent": "hot|warm|cold", "lead_score": 0-100, "reasoning": "string"}
  ],
  "common_questions": ["string", ...],
  "objections": ["string", ...],
  "why_customers_arent_buying": ["string", ...],
  "what_customers_want": ["string", ...],
  "trending_topics": ["string", ...],
  "sales_opportunities": ["string", ...],
  "recommended_actions": ["string", ...],
  "product_opportunities": ["string", ...]
}

Write every string in simple, plain language a busy small-business owner can
read in a few seconds and immediately act on. No jargon."""


def build_user_prompt(memory_box: BusinessMemoryBox, conversations: list[Conversation]) -> str:
    return f"""BUSINESS MEMORY BOX:
{build_grounding_context(memory_box)}

CUSTOMER CONVERSATIONS:
{_fmt_conversations(conversations)}

Analyze the conversations using only the facts above. Return the JSON object now."""


_UPDATABLE_LIST_FIELDS = {
    "offers", "discounts", "promotions", "competitors", "goals",
    "customer_pain_points", "common_questions", "keywords", "business_rules",
}
_UPDATABLE_SCALAR_FIELDS = {
    "business_name", "target_customers", "brand_voice", "shipping_policy", "return_policy",
}

EXTRACTION_SYSTEM_PROMPT = """You extract structured business facts from a short
note written by a small business owner into their "Memory Box".

RULES:
- Extract ONLY facts explicitly stated in the note. Never infer, guess, or
  add anything not literally present in the text.
- If the note mentions nothing extractable, return an empty JSON object: {}
- Respond with ONLY a single JSON object, no markdown fences, no commentary,
  using ONLY these keys (omit any key with nothing to report):

{
  "business_name": "string, only if explicitly stated",
  "target_customers": "string, only if explicitly stated",
  "brand_voice": "string, only if explicitly stated",
  "shipping_policy": "string, only if explicitly stated",
  "return_policy": "string, only if explicitly stated",
  "offers": ["string", ...],
  "discounts": ["string", ...],
  "promotions": ["string", ...],
  "competitors": ["string", ...],
  "goals": ["string", ...],
  "customer_pain_points": ["string", ...],
  "common_questions": ["string", ...],
  "keywords": ["string", ...],
  "business_rules": ["string", ...],
  "products": [{"name": "string", "price": "string or omit", "category": "string or omit",
                "is_best_seller": true/false, "wants_to_sell_more": true/false}],
  "services": [{"name": "string", "price": "string or omit"}],
  "faqs": [{"question": "string", "answer": "string"}]
}

CLASSIFICATION PROCEDURE — before writing any output, go sentence by
sentence through the note and decide EXACTLY ONE category for each fact,
using this order of questions:

1. Is this naming a specific physical item the business sells, with (or
   without) a price/category/description attached? -> "products".
   Is it naming a specific service instead? -> "services".
   Any price, category, or description in the SAME sentence about that item
   belongs INSIDE that same product/service object — never as a separate
   top-level fact.
2. Is this phrased as a question-and-answer, or "customers often ask X" ->
   "faqs" (write it as a question/answer pair, inventing a natural question
   if only the answer was given, e.g. a stated return policy is NOT
   automatically an FAQ — see rule 3 first).
3. Is this a general fulfillment or return rule that applies broadly, not
   tied to one product? -> "shipping_policy" / "return_policy". This takes
   priority over turning it into a "faqs" entry.
4. Is this a time-limited or conditional deal (e.g. "10% off this week",
   "buy 2 get 1 free")? -> "offers" / "discounts" / "promotions".
5. Is this a hard constraint on what the AI itself must or must never say
   or promise? -> "business_rules".
6. Is this a statement of identity/positioning (who the business is, who
   it's for, how it should sound)? -> "business_name" / "target_customers"
   / "brand_voice".
7. Otherwise, does it match "competitors", "goals", "customer_pain_points",
   "common_questions", or "keywords" as explicitly framed in the note?

If a piece of text doesn't clearly fit exactly one category, LEAVE IT OUT
entirely rather than forcing it into "products" or any other field — a
missing fact is far better than a wrongly classified one. Never let a
price, policy, or FAQ leak into the products/services list just because it
appeared near a product name in the same note.

WORKED EXAMPLES:

Note: "Our return window is 30 days and the Vitamin C Serum is $25."
Output: {"return_policy": "30 day return window", "products": [{"name": "Vitamin C Serum", "price": "$25"}]}

Note: "Do you ship internationally? No, only within India."
Output: {"faqs": [{"question": "Do you ship internationally?", "answer": "No, only within India."}]}

Note: "We sell hand-thrown ceramic mugs for $18 each, and this week only
it's 20% off any order over $50. We never promise a delivery date since
it's all handmade to order."
Output: {"products": [{"name": "ceramic mugs", "price": "$18", "description": "hand-thrown"}], "promotions": ["20% off any order over $50, this week only"], "business_rules": ["Never promise a delivery date"]}

Note: "We're a small candle shop mainly for people who like cozy home
decor."
Output: {"target_customers": "people who like cozy home decor"}
(No product was named here, so nothing goes in "products" — do not invent one.)"""


def extract_facts_from_message(text: str) -> tuple[dict, str]:
    parsed, model_used = chat_json_with_fallback(
        system_prompt=EXTRACTION_SYSTEM_PROMPT,
        user_prompt=f"Owner's note:\n\"{text}\"\n\nReturn the JSON object now.",
        temperature=0.0,
        # Raised from 1200: the first model in the fallback chain was
        # getting cut off mid-JSON on notes with several facts, which
        # forced a fallback to a weaker model that classifies worse.
        max_tokens=2000,
    )
    return parsed, model_used


def merge_extracted_facts(box: BusinessMemoryBox, extracted: dict) -> BusinessMemoryBox:
    data = box.model_dump()

    for field in _UPDATABLE_SCALAR_FIELDS:
        if extracted.get(field):
            data[field] = extracted[field]

    for field in _UPDATABLE_LIST_FIELDS:
        new_items = extracted.get(field) or []
        if new_items:
            existing = data.get(field, [])
            merged = existing + [i for i in new_items if i not in existing]
            data[field] = merged

    if extracted.get("products"):
        existing_names = {p["name"].lower() for p in data["products"]}
        for p in extracted["products"]:
            if p.get("name") and p["name"].lower() not in existing_names:
                data["products"].append(p)
                existing_names.add(p["name"].lower())

    if extracted.get("services"):
        existing_names = {s["name"].lower() for s in data["services"]}
        for s in extracted["services"]:
            if s.get("name") and s["name"].lower() not in existing_names:
                data["services"].append(s)
                existing_names.add(s["name"].lower())

    if extracted.get("faqs"):
        existing_qs = {f["question"].lower() for f in data["faqs"]}
        for f in extracted["faqs"]:
            if f.get("question") and f["question"].lower() not in existing_qs:
                data["faqs"].append(f)
                existing_qs.add(f["question"].lower())

    data["updated_at"] = datetime.utcnow()
    return BusinessMemoryBox(**data)


def generate_insights(
    memory_box: BusinessMemoryBox, conversations: list[Conversation]
) -> InsightsResult:
    computed_mentions = compute_product_mentions(memory_box, conversations)

    user_prompt = build_user_prompt(memory_box, conversations)

    try:
        parsed, model_used = chat_json_with_fallback(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            # Lower temperature: this is a classification/analysis task, not
            # creative writing — consistency matters more than variety here.
            temperature=0.1,
            # Raised from 3000: a long conversation set plus 10 output fields
            # can run the model out of room mid-JSON, same truncation issue
            # fixed in extract_facts_from_message().
            max_tokens=4000,
        )
    except NvidiaAPIError as e:
        return InsightsResult(
            business_id=memory_box.business_id,
            model_used="none (all providers failed)",
            computed_product_mentions=computed_mentions,
            summary=f"AI analysis unavailable right now ({e}). Showing computed stats only.",
            sentiment_overview="unavailable",
            customer_intents=[],
        )

    return InsightsResult(
        business_id=memory_box.business_id,
        model_used=model_used,
        computed_product_mentions=computed_mentions,
        summary=parsed.get("summary", ""),
        sentiment_overview=parsed.get("sentiment_overview", ""),
        customer_intents=parsed.get("customer_intents", []),
        common_questions=parsed.get("common_questions", []),
        objections=parsed.get("objections", []),
        why_customers_arent_buying=parsed.get("why_customers_arent_buying", []),
        what_customers_want=parsed.get("what_customers_want", []),
        trending_topics=parsed.get("trending_topics", []),
        sales_opportunities=parsed.get("sales_opportunities", []),
        recommended_actions=parsed.get("recommended_actions", []),
        product_opportunities=parsed.get("product_opportunities", []),
    )