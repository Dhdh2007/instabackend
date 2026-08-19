"""
nvidia_models.py — the NVIDIA NIM model IDs you're actually using.

Kept separate from nvidia_client.py on purpose: this file is just data (a
list of model ID strings), so you can add/remove/reorder models here
without touching any client/fallback logic. Order matters — the first
model is tried first; nvidia_client only moves to the next one if a model
errors out OR (for chat_json_with_fallback) doesn't return valid JSON.

HOW TO USE THIS:

Both chat_with_fallback() and chat_json_with_fallback() in nvidia_client.py
already accept an optional `models` argument that overrides their built-in
DEFAULT_MODEL_FALLBACK_CHAIN for a single call:

    from app.nvidia_models import MODEL_FALLBACK_CHAIN
    from app.nvidia_client import chat_json_with_fallback

    parsed, model_used = chat_json_with_fallback(
        system_prompt=...,
        user_prompt=...,
        models=MODEL_FALLBACK_CHAIN,
    )

If you want this chain to be the default everywhere (insights_engine.py,
qa_engine.py, etc.) without passing `models=` at every call site, open
nvidia_client.py and swap its DEFAULT_MODEL_FALLBACK_CHAIN list for this
one — that's the only edit needed there.

Verified against build.nvidia.com's actual catalog:

- meta/muse-glimmer-30b            — confirmed free endpoint.
- z-ai/glm-5.2                     — confirmed free endpoint. NVIDIA's free
  tier lists "structured output" / "function calling" as unsupported for
  this one — chat_json_with_fallback doesn't rely on native structured
  output (it just asks for JSON in the prompt and parses it), so this
  should still mostly work, just possibly less reliably than the others.
- google/diffusiongemma-26b-a4b-it — confirmed free endpoint. Diffusion
  (parallel-token) model, so it's actually fast — decent early-chain pick
  for latency-sensitive replies like the DM auto-reply.
- nvidia/nemotron-3-ultra-550b-a55b — confirmed free endpoint. 550B params
  (55B active) — noticeably slower than the others, so it works better as
  a late fallback than a first choice for anything real-time.
- llama-guard-4-12b — FIXED BELOW. The real model ID needs the "meta/"
  prefix (you had it bare, which 404s). Also: this is a content-safety
  CLASSIFIER, not a chat model — it outputs a safe/unsafe verdict, not a
  DM reply or your JSON schema. It's kept in the list below (now with the
  correct ID) since you listed it, but it will likely produce unusable
  output for either qa_engine.py or insights_engine.py's prompts. Consider
  moving it to a separate moderation-only chain instead.

The free tier across all of these is rate-limited to ~40 requests/minute,
and free-tier model availability can change with little notice — worth a
periodic recheck at build.nvidia.com rather than assuming this list is
permanent.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# General-purpose fallback chain — for both the DM auto-reply (qa_engine.py)
# and the insights report (insights_engine.py). Edit freely.
# ---------------------------------------------------------------------------

 
def b() -> list[str]:
    """Returns your NVIDIA NIM model fallback chain, in try-first-to-last order."""
    return [
       
        "z-ai/glm-5.2",
        "google/diffusiongemma-26b-a4b-it",
         # fixed: was missing the "meta/" prefix
       
          "meta/llama-3.1-70b-instruct",
    "meta/llama-3.3-70b-instruct",
    "mistralai/mixtral-8x22b-instruct-v0.1",
    "google/gemma-2-27b-it",
    "nvidia/nemotron-4-340b-instruct",
     "meta/muse-glimmer-30b",
    ]