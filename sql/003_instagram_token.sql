-- Run in Supabase SQL Editor after schema.sql and 002_analytics.sql.
-- Adds storage for the Page access token used to actually send Instagram DMs.

ALTER TABLE public.profiles
    ADD COLUMN IF NOT EXISTS instagram_access_token TEXT;

-- This token is sensitive (it can send messages as the connected account),
-- so it must never be selectable by the anon/authenticated role — only the
-- backend's service role key reads it. No SELECT RLS policy is added for it
-- on purpose; the existing "Users can view their own profile" policy should
-- be scoped to exclude this column if you ever expose profiles to the
-- frontend directly via supabase-js instead of through your FastAPI backend.
