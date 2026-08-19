-- ============================================================================
-- DM TRIGGER BOT — MIGRATION 002: ANALYTICS
-- Run this AFTER sql/schema.sql, in the Supabase SQL Editor.
-- Adds an event log so the dashboard can show a real trend chart and
-- activity feed instead of the frontend's placeholder data.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. DM_EVENTS TABLE
-- ----------------------------------------------------------------------------
-- profiles.dms_sent_count is a running total — great for the O(1) limit
-- check, useless for "show me a 7-day trend" or "what just happened".
-- This table is the append-only log that answers those questions: one
-- row per DM actually sent.
-- ----------------------------------------------------------------------------
CREATE TABLE public.dm_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    campaign_id UUID NOT NULL REFERENCES public.campaigns(id) ON DELETE CASCADE,
    trigger_word TEXT NOT NULL,
    commenter_username TEXT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The analytics endpoint's hot path is "give me this user's events from
-- the last N days, newest first" — this index serves that directly.
CREATE INDEX idx_dm_events_user_sent_at
    ON public.dm_events (user_id, sent_at DESC);

ALTER TABLE public.dm_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view their own DM events"
    ON public.dm_events FOR SELECT
    USING (auth.uid() = user_id);

-- ----------------------------------------------------------------------------
-- 2. record_dm_sent — REPLACES increment_dm_count AS THE WEBHOOK'S CALL
-- ----------------------------------------------------------------------------
-- Same atomicity guarantee as increment_dm_count (see sql/schema.sql for
-- the full explanation), but now the counter bump AND the event-log
-- insert happen inside the SAME function, which Postgres runs as a
-- single transaction. That matters: if we instead did the increment in
-- one round-trip and the event insert in a second round-trip from
-- Python, a crash or network blip between the two would leave the
-- counter and the event log disagreeing with each other. Wrapping both
-- writes in one function makes them succeed or fail together.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.record_dm_sent(
    p_user_id UUID,
    p_campaign_id UUID,
    p_trigger_word TEXT,
    p_commenter_username TEXT,
    p_limit INTEGER DEFAULT 100
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_new_count INTEGER;
BEGIN
    UPDATE public.profiles
    SET dms_sent_count = dms_sent_count + 1
    WHERE id = p_user_id
      AND dms_sent_count < p_limit
    RETURNING dms_sent_count INTO v_new_count;

    -- NULL means the limit check failed — bail out WITHOUT logging an
    -- event, since no DM actually went out.
    IF v_new_count IS NULL THEN
        RETURN NULL;
    END IF;

    INSERT INTO public.dm_events (user_id, campaign_id, trigger_word, commenter_username)
    VALUES (p_user_id, p_campaign_id, p_trigger_word, p_commenter_username);

    RETURN v_new_count;
END;
$$;
