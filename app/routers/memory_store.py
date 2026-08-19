"""
Storage layer, kept behind a small interface so router.py and
insights_engine.py never touch storage details directly.

JSONFileStore (below) is what you started with — it's fine for local dev,
but on Render its file lives on ephemeral disk: it's wiped on every
redeploy/restart, and isn't shared if you ever scale past one instance.
That means every Memory Box fact your users type in, and their whole
insights history, can vanish without warning. SupabaseMemoryStore replaces
it with the same interface, backed by two tables — see
sql/003_memory_box_and_insights.sql for the schema this expects.
"""

from __future__ import annotations
import json
import os
import threading
from datetime import datetime
from typing import Optional

from app.schemas import BusinessMemoryBox, InsightsResult

_LOCK = threading.Lock()


class MemoryStore:
    """Abstract interface — implement these five methods against your real DB."""

    def get_memory_box(self, business_id: str) -> Optional[BusinessMemoryBox]:
        raise NotImplementedError

    def save_memory_box(self, box: BusinessMemoryBox) -> None:
        raise NotImplementedError

    def save_insights(self, result: InsightsResult) -> None:
        raise NotImplementedError

    def get_latest_insights(self, business_id: str) -> Optional[InsightsResult]:
        raise NotImplementedError

    def get_insights_history(self, business_id: str, limit: int = 10) -> list[InsightsResult]:
        raise NotImplementedError


class JSONFileStore(MemoryStore):
    """
    Simple local persistence for development ONLY. Do not run this in
    production on Render — see module docstring above.
    """

    def __init__(self, path: str = "ai_insights_data.json"):
        self.path = path
        if not os.path.exists(self.path):
            self._write({"memory_boxes": {}, "insights": {}})

    def _read(self) -> dict:
        with open(self.path, "r") as f:
            return json.load(f)

    def _write(self, data: dict) -> None:
        with open(self.path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def get_memory_box(self, business_id: str) -> Optional[BusinessMemoryBox]:
        with _LOCK:
            data = self._read()
            raw = data["memory_boxes"].get(business_id)
            return BusinessMemoryBox(**raw) if raw else None

    def save_memory_box(self, box: BusinessMemoryBox) -> None:
        with _LOCK:
            data = self._read()
            data["memory_boxes"][box.business_id] = json.loads(box.model_dump_json())
            self._write(data)

    def save_insights(self, result: InsightsResult) -> None:
        with _LOCK:
            data = self._read()
            data["insights"].setdefault(result.business_id, [])
            data["insights"][result.business_id].append(json.loads(result.model_dump_json()))
            data["insights"][result.business_id] = data["insights"][result.business_id][-50:]
            self._write(data)

    def get_latest_insights(self, business_id: str) -> Optional[InsightsResult]:
        with _LOCK:
            data = self._read()
            history = data["insights"].get(business_id, [])
            if not history:
                return None
            return InsightsResult(**history[-1])

    def get_insights_history(self, business_id: str, limit: int = 10) -> list[InsightsResult]:
        with _LOCK:
            data = self._read()
            history = data["insights"].get(business_id, [])
            return [InsightsResult(**h) for h in history[-limit:]]


class SupabaseMemoryStore(MemoryStore):
    """
    Real persistence, backed by two Supabase tables:
      - public.business_memory_boxes  (one row per business, whole box as jsonb)
      - public.ai_insights_results    (one row per generated insights run)

    Each method fetches a fresh admin client via get_supabase_admin_client_fn
    rather than holding one long-lived client, matching how the rest of your
    app (main.py's Depends(get_supabase_admin_client)) already does it.
    """

    def __init__(self, get_supabase_admin_client_fn):
        self._get_client = get_supabase_admin_client_fn

    def get_memory_box(self, business_id: str) -> Optional[BusinessMemoryBox]:
        db = self._get_client()
        try:
            res = (
                db.table("business_memory_boxes")
                .select("data")
                .eq("business_id", business_id)
                .maybe_single()
                .execute()
            )
        except Exception as e:
            # postgrest-py's maybe_single() can raise when the underlying
            # query itself fails — e.g. business_id isn't a valid uuid for
            # this column (Postgres error 22P02). Surface that clearly
            # instead of letting a bare AttributeError bubble up later.
            raise ValueError(
                f"Could not fetch memory box for business_id={business_id!r}: {e}"
            ) from e

        # Some postgrest-py versions return None (instead of a response
        # object with data=None) when maybe_single() finds zero rows or the
        # query errors — guard for that explicitly rather than assuming
        # res is always a response object.
        if res is None or not res.data:
            return None
        return BusinessMemoryBox(**res.data["data"])

    def save_memory_box(self, box: BusinessMemoryBox) -> None:
        db = self._get_client()
        payload = {
            "business_id": box.business_id,
            "business_name": box.business_name,
            "data": json.loads(box.model_dump_json()),
            "updated_at": datetime.utcnow().isoformat(),
        }
        db.table("business_memory_boxes").upsert(payload, on_conflict="business_id").execute()

    def save_insights(self, result: InsightsResult) -> None:
        db = self._get_client()
        payload = {
            "business_id": result.business_id,
            "model_used": result.model_used,
            "generated_at": result.generated_at.isoformat(),
            "data": json.loads(result.model_dump_json()),
        }
        db.table("ai_insights_results").insert(payload).execute()
        # Keep only the most recent 50 runs per business, mirroring
        # JSONFileStore's behaviour so the table doesn't grow unbounded.
        old_res = (
            db.table("ai_insights_results")
            .select("id")
            .eq("business_id", result.business_id)
            .order("generated_at", desc=True)
            .execute()
        )
        stale_ids = [row["id"] for row in (old_res.data or [])[50:]]
        if stale_ids:
            db.table("ai_insights_results").delete().in_("id", stale_ids).execute()

    def get_latest_insights(self, business_id: str) -> Optional[InsightsResult]:
        db = self._get_client()
        res = (
            db.table("ai_insights_results")
            .select("data")
            .eq("business_id", business_id)
            .order("generated_at", desc=True)
            .limit(1)
            .execute()
        )
        if not res.data:
            return None
        return InsightsResult(**res.data[0]["data"])

    def get_insights_history(self, business_id: str, limit: int = 10) -> list[InsightsResult]:
        db = self._get_client()
        res = (
            db.table("ai_insights_results")
            .select("data")
            .eq("business_id", business_id)
            .order("generated_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [InsightsResult(**row["data"]) for row in (res.data or [])]


def _build_store() -> MemoryStore:
    """
    Defaults to Supabase. Set MEMORY_STORE_BACKEND=json locally if you want
    the old zero-infra file store for quick local runs without touching
    your Supabase project.
    """
    if os.getenv("MEMORY_STORE_BACKEND", "supabase").lower() == "json":
        return JSONFileStore()

    from app.config import get_supabase_admin_client  # local import avoids a
    # hard dependency on config.py for anyone still using JSONFileStore only.
    return SupabaseMemoryStore(get_supabase_admin_client)


# Singleton used by the router.
store = _build_store()