-- ============================================================================
-- DM TRIGGER BOT — DATABASE SCHEMA
-- Run this in the Supabase SQL Editor (Project -> SQL Editor -> New Query)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. PROFILES TABLE
-- ----------------------------------------------------------------------------
-- Supabase Auth already creates a table called `auth.users` for you (email,
-- password hash, etc.) but you can't easily add custom columns to it, and
-- you shouldn't — it's managed by the Auth service. The standard pattern is
-- to create a `public.profiles` table with a 1:1 relationship to auth.users,
-- linked by the same UUID primary key. This is where WE store app-specific
-- data like the DM counter and tier.
-- ----------------------------------------------------------------------------
CREATE TABLE public.profiles (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'free' CHECK (tier IN ('free', 'pro')),
    dms_sent_count INTEGER NOT NULL DEFAULT 0 CHECK (dms_sent_count >= 0),
    -- The Instagram Business Account ID this user has connected. The
    -- incoming webhook payload tells us WHICH Instagram account received
    -- the comment; we use this column to map that back to one of our
    -- users. Nullable because a user may not have connected Instagram yet.
    instagram_account_id TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- 2. AUTO-CREATE A PROFILE ROW WHENEVER A NEW USER SIGNS UP
-- ----------------------------------------------------------------------------
-- Supabase Auth writes new signups into `auth.users`. This trigger listens
-- for that INSERT and automatically creates a matching row in our
-- `public.profiles` table, so your backend never has to worry about a user
-- existing in Auth but not existing in your app's data model.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER  -- runs with the privileges of the function owner, not the caller
SET search_path = public
AS $$
BEGIN
    INSERT INTO public.profiles (id, email)
    VALUES (NEW.id, NEW.email);
    RETURN NEW;
END;
$$;

CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- ----------------------------------------------------------------------------
-- 3. CAMPAIGNS TABLE
-- ----------------------------------------------------------------------------
-- A "campaign" is one trigger rule: "when someone comments <trigger_word>,
-- send them <destination_link> via DM".
-- ----------------------------------------------------------------------------
CREATE TABLE public.campaigns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    trigger_word TEXT NOT NULL,
    destination_link TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index because every webhook hit will query "find campaigns for this
-- trigger word that are active" — this is the hot path, so it needs to be fast.
CREATE INDEX idx_campaigns_trigger_lookup
    ON public.campaigns (trigger_word, is_active);

CREATE INDEX idx_campaigns_user_id
    ON public.campaigns (user_id);

-- ----------------------------------------------------------------------------
-- 4. ROW LEVEL SECURITY (RLS)
-- ----------------------------------------------------------------------------
-- RLS is Postgres-level access control. Even if someone got hold of your
-- Supabase anon key (which is public/client-safe by design), RLS ensures
-- a user can only ever SELECT/INSERT/UPDATE/DELETE rows they own.
-- Our FastAPI backend uses the SERVICE ROLE key (bypasses RLS) because it
-- has already authenticated the user itself via JWT — but enabling RLS is
-- still critical defense-in-depth in case any client ever talks to Supabase
-- directly, and it's a Supabase best practice regardless.
-- ----------------------------------------------------------------------------
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.campaigns ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view their own profile"
    ON public.profiles FOR SELECT
    USING (auth.uid() = id);

CREATE POLICY "Users can view their own campaigns"
    ON public.campaigns FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Users can insert their own campaigns"
    ON public.campaigns FOR INSERT
    WITH CHECK (auth.uid() = user_id);

-- ----------------------------------------------------------------------------
-- 5. THE ATOMIC INCREMENT FUNCTION — THE MOST IMPORTANT PART OF THIS FILE
-- ----------------------------------------------------------------------------
-- This is how we solve the race condition described in the deep-dive lesson.
-- Instead of the backend doing:
--     1. SELECT dms_sent_count FROM profiles WHERE id = X   (read)
--     2. if count < 100: UPDATE profiles SET count = count+1 (write)
-- ...which has a gap between read and write where two requests can both
-- read "99" and both decide to proceed, we push the ENTIRE check-and-increment
-- into a single atomic SQL statement that Postgres itself guarantees is
-- serialized per-row via row-level locking.
--
-- UPDATE ... WHERE dms_sent_count < 100 RETURNING dms_sent_count
--
-- Either the row's counter is still below 100 at the exact instant Postgres
-- executes the update (in which case it increments and returns the new
-- count), or it isn't (in which case zero rows match, nothing is updated,
-- and RETURNING gives us NULL). There is no window where two callers can
-- both "pass" the check.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.increment_dm_count(p_user_id UUID, p_limit INTEGER DEFAULT 100)
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

    -- v_new_count is NULL if no row matched (limit already reached, or
    -- user doesn't exist). The Python layer checks for this NULL.
    RETURN v_new_count;
END;
$$;
