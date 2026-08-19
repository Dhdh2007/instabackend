"""
AI provider — everything in one file: the NVIDIA NIM fallback chain, the
model list, and the Gemini-first wrapper on top.

Order of attempts:
    1. Google Gemini (Google Gen AI SDK)
    2. NVIDIA NIM fallback chain (try each model in order until one works)

insights.py / qa_engine.py only ever call `call_model_json()` and only
ever need to catch `AIProviderError` — that's raised only if BOTH Gemini
and every model in the NVIDIA chain fail.

Env vars:
    GOOGLE_API_KEY   -- required for Gemini. Free key: https://aistudio.google.com/apikey
    NVIDIA_API_KEY   -- required for the NVIDIA fallback. Free key: https://build.nvidia.com

Install:
    pip install google-genai requests --break-system-packages
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

import requests
from google import genai
from google.genai import types

logger = logging.getLogger("ai_insights.ai_provider")


# ---------------------------------------------------------------------------
# Public error type — the only exception insights.py needs to catch.
# ---------------------------------------------------------------------------

class AIProviderError(Exception):
    """Raised only when Gemini AND the entire NVIDIA fallback chain fail."""


# ---------------------------------------------------------------------------
# NVIDIA NIM model chain
# ---------------------------------------------------------------------------
# NVIDIA's hosted inference API (build.nvidia.com / integrate.api.nvidia.com)
# is OpenAI-compatible: one endpoint, one API key, just change "model" to
# hit different (free-tier) models. Edit this list freely.
#
# NOTE: double check these IDs against https://build.nvidia.com/models —
# a couple of these (glm-5.2, diffusiongemma-26b-a4b-it, muse-glimmer-30b)
# don't match any NVIDIA NIM model I recognize. If they're wrong, every
# fallback run wastes a full request+timeout hitting a dead model before
# reaching a working one — which is likely part of why this feels slow.

def get_nvidia_model_chain() -> list[str]:
    """Your NVIDIA NIM model fallback chain, in try-first-to-last order."""
    return [
        "z-ai/glm-5.2",
        "google/diffusiongemma-26b-a4b-it",
        "meta/llama-3.1-70b-instruct",
        "meta/llama-3.3-70b-instruct",
        "mistralai/mixtral-8x22b-instruct-v0.1",
        "google/gemma-2-27b-it",
        "nvidia/nemotron-4-340b-instruct",
        "meta/muse-glimmer-30b",
    ]


NVIDIA_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
DEFAULT_MODEL_FALLBACK_CHAIN = get_nvidia_model_chain()


class NvidiaAPIError(Exception):
    """Raised when every model in the NVIDIA fallback chain fails."""


@dataclass
class ChatResult:
    text: str
    model_used: str


def _get_nvidia_api_key() -> str:
    api_key = os.getenv("NVIDIA_API_KEY")
    if not api_key:
        raise NvidiaAPIError(
            "NVIDIA_API_KEY environment variable is not set. "
            "Get a free key at https://build.nvidia.com"
        )
    return api_key


def _call_single_nvidia_model(
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
) -> str:
    api_key = _get_nvidia_api_key()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": 0.9,
    }

    resp = requests.post(NVIDIA_API_URL, headers=headers, json=payload, timeout=timeout)

    if resp.status_code != 200:
        raise RuntimeError(f"{model} returned HTTP {resp.status_code}: {resp.text[:300]}")

    body = resp.json()
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"{model} returned unexpected response shape: {body}") from e


def _strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t.lstrip("`")
        if t.endswith("```"):
            t = t.rsplit("```", 1)[0]
    return t.strip()


def chat_with_fallback(
    system_prompt: str,
    user_prompt: str,
    models: Optional[list[str]] = None,
    temperature: float = 0.2,
    max_tokens: int = 2500,
    timeout: int = 60,
) -> ChatResult:
    """Try each model in `models` (default: DEFAULT_MODEL_FALLBACK_CHAIN) in
    order. Returns the first successful completion. Raises NvidiaAPIError
    only if every model in the chain fails."""
    chain = models or DEFAULT_MODEL_FALLBACK_CHAIN
    errors: list[str] = []

    for model in chain:
        try:
            text = _call_single_nvidia_model(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            logger.info("NVIDIA call succeeded with model=%s", model)
            return ChatResult(text=text, model_used=model)
        except Exception as e:  # noqa: BLE001 - intentionally broad, this is a fallback loop
            logger.warning("Model %s failed: %s", model, e)
            errors.append(f"{model}: {e}")
            continue

    raise NvidiaAPIError(
        "All models in the fallback chain failed:\n" + "\n".join(errors)
    )


def chat_json_with_fallback(
    system_prompt: str,
    user_prompt: str,
    models: Optional[list[str]] = None,
    temperature: float = 0.2,
    max_tokens: int = 2500,
    timeout: int = 60,
) -> tuple[dict, str]:
    """Same as chat_with_fallback, but expects the model to return JSON and
    parses it. A model returning invalid JSON counts as a failure for that
    model and we move to the next one in the chain."""
    chain = models or DEFAULT_MODEL_FALLBACK_CHAIN
    errors: list[str] = []

    for model in chain:
        try:
            text = _call_single_nvidia_model(
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            cleaned = _strip_code_fences(text)
            parsed = json.loads(cleaned)
            logger.info("NVIDIA JSON call succeeded with model=%s", model)
            return parsed, model
        except Exception as e:  # noqa: BLE001
            logger.warning("Model %s failed or returned invalid JSON: %s", model, e)
            errors.append(f"{model}: {e}")
            continue

    raise NvidiaAPIError(
        "All models in the fallback chain failed to produce valid JSON:\n" + "\n".join(errors)
    )


# ---------------------------------------------------------------------------
# Gemini (tried first)
# ---------------------------------------------------------------------------

# Fast + cheap + good at "follow this JSON schema" instructions.
# Swap to "gemini-2.5-pro" if you want higher quality over speed.
GEMINI_MODEL = "gemini-2.5-flash"

# Gemini calls that take longer than this are treated as a failure and we
# fall through to NVIDIA instead of making the user wait.
GEMINI_TIMEOUT_SECONDS = 20

_gemini_client: "genai.Client | None" = None


def _get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            # Caller catches this and falls through to NVIDIA — so a missing
            # key just silently degrades to "NVIDIA only", it doesn't 500.
            raise RuntimeError("GOOGLE_API_KEY environment variable is not set.")
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


def _call_gemini_json_sync(system_prompt: str, user_prompt: str) -> dict:
    """Blocking call — run this inside asyncio.to_thread, never directly in an async def."""
    client = _get_gemini_client()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.2,
            response_mime_type="application/json",  # forces valid JSON back, no code-fence stripping needed
        ),
    )
    text = response.text
    if not text:
        raise RuntimeError("Gemini returned an empty response")
    return json.loads(text)


# ---------------------------------------------------------------------------
# Public entry point — this is what insights.py / qa_engine.py import.
# ---------------------------------------------------------------------------

async def call_model_json(system_prompt: str, user_prompt: str) -> dict:
    """
    Try Gemini first (with a hard timeout so a slow/hanging call doesn't
    stall the request). On ANY failure — missing key, API error, timeout,
    malformed JSON — log it and fall through to the NVIDIA chain. Only
    raises AIProviderError if both paths are exhausted.
    """
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_call_gemini_json_sync, system_prompt, user_prompt),
            timeout=GEMINI_TIMEOUT_SECONDS,
        )
        logger.info("Gemini call succeeded (model=%s)", GEMINI_MODEL)
        return result
    except Exception as e:  # noqa: BLE001 - intentionally broad, this is a fallback boundary
        logger.warning("Gemini failed (%s) — falling back to NVIDIA chain", e)

    try:
        parsed, model_used = await asyncio.to_thread(
            chat_json_with_fallback, system_prompt, user_prompt
        )
        logger.info("NVIDIA fallback succeeded (model=%s)", model_used)
        return parsed
    except NvidiaAPIError as e:
        raise AIProviderError(
            f"Gemini failed and the entire NVIDIA fallback chain also failed: {e}"
        ) from e