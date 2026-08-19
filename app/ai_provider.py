"""
ai_provider.py

Thin wrapper so the rest of the backend doesn't care whether a call
goes to Google AI Studio (Gemini, via the official google-genai SDK)
or an NVIDIA NIM model (via raw httpx).

BEHAVIOR (this version):
  - Google is tried FIRST (fast, cheap, good default).
  - If Google fails (missing key, HTTP error, blocked response, bad
    JSON) OR isn't configured at all, we fall through to your NVIDIA
    fallback chain from nvidia_models.py's b(), tried one model at a
    time in order.
  - We only give up once Google AND every model in the NVIDIA chain
    have failed. All individual errors are collected so the final
    AIProviderError tells you exactly what went wrong at each step —
    check `str(exc)` (e.g. log it) instead of just seeing a bare 502.

Required env vars (set these in your backend's .env — NEVER in the
Next.js frontend, and never commit them):

    GOOGLE_API_KEY=...            # from Google AI Studio
    GOOGLE_MODEL=gemini-2.5-flash # cheap + fast, good enough for scoring
    NVIDIA_API_KEY=...            # from build.nvidia.com
    NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1

AI_PROVIDER env var is no longer used to pick ONE path — both are
tried now, Google first, NVIDIA chain as fallback. NVIDIA model IDs
come from nvidia_models.py's b(), tried in order.

Install:  pip install google-genai
(this is the new unified SDK — do NOT install the older, deprecated
google-generativeai package)
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from google import genai
from google.genai import types

from .model import b

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GOOGLE_MODEL = os.getenv("GOOGLE_MODEL", "gemini-3.1-flash-lite")

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")

# Created once at module level so the SDK can reuse its internal
# connection pool instead of reconnecting on every call.
_genai_client = genai.Client(api_key=GOOGLE_API_KEY) if GOOGLE_API_KEY else None


class AIProviderError(RuntimeError):
    pass


def _parse_json_reply(raw: str) -> Any:
    raw = raw.strip()
    # Models sometimes wrap JSON in ```json fences despite instructions.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)  # may raise json.JSONDecodeError


# ---------------------------------------------------------------------------
# Google (via google-genai SDK)
# ---------------------------------------------------------------------------

async def _call_google(system_prompt: str, user_prompt: str) -> str:
    if not _genai_client:
        raise AIProviderError("GOOGLE_API_KEY is not set on the backend.")

    try:
        response = await _genai_client.aio.models.generate_content(
            model=GOOGLE_MODEL,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.2,
                response_mime_type="application/json",
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
    except Exception as exc:
        raise AIProviderError(f"Google GenAI error: {exc}") from exc

    if not response.text:
        # Usually means the response got safety-blocked — finish_reason
        # tells you why when this happens.
        reason = None
        if response.candidates:
            reason = response.candidates[0].finish_reason
        raise AIProviderError(f"Empty response from Google (finish_reason={reason}).")

    return response.text


# ---------------------------------------------------------------------------
# NVIDIA NIM (raw httpx, fallback chain)
# ---------------------------------------------------------------------------

async def _call_nvidia_one(model: str, system_prompt: str, user_prompt: str) -> str:
    """Calls a single NVIDIA NIM model. Raises AIProviderError on any failure."""
    url = f"{NVIDIA_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        # Most NIM chat models honor this; harmless if ignored.
        "response_format": {"type": "json_object"},
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, headers=headers, json=payload)

    if resp.status_code != 200:
        raise AIProviderError(f"NVIDIA NIM error ({model}) {resp.status_code}: {resp.text}")

    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise AIProviderError(f"Unexpected NVIDIA response shape ({model}): {data}") from exc


# ---------------------------------------------------------------------------
# Public entrypoint — Google first, NVIDIA chain as fallback
# ---------------------------------------------------------------------------

async def call_model_json(system_prompt: str, user_prompt: str) -> Any:
    """
    Tries Google first. If it fails or isn't configured, walks the
    NVIDIA fallback chain (nvidia_models.b()) one model at a time.
    A model "failing" includes HTTP errors AND replies that don't
    parse as valid JSON — either one moves on to the next option.

    Raises AIProviderError only if EVERY option failed, with all the
    individual errors collected so you can see exactly what broke.
    """
    errors: list[str] = []

    if GOOGLE_API_KEY:
        try:
            raw = await _call_google(system_prompt, user_prompt)
            return _parse_json_reply(raw)
        except (AIProviderError, json.JSONDecodeError) as exc:
            errors.append(f"google/{GOOGLE_MODEL}: {exc}")
    else:
        errors.append("google: GOOGLE_API_KEY not set, skipped")

    if NVIDIA_API_KEY:
        chain = b()
        if not chain:
            errors.append("nvidia: fallback chain from nvidia_models.b() is empty")
        else:
            for model in chain:
                try:
                    raw = await _call_nvidia_one(model, system_prompt, user_prompt)
                    return _parse_json_reply(raw)
                except (AIProviderError, json.JSONDecodeError) as exc:
                    errors.append(f"nvidia/{model}: {exc}")
                    continue
    else:
        errors.append("nvidia: NVIDIA_API_KEY not set, skipped")

    raise AIProviderError(
        "All providers failed. Tried Google then the full NVIDIA chain. "
        f"Errors: {errors}"
    )