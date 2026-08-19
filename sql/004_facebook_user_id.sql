-- Run in Supabase SQL Editor after 003_instagram_token.sql.
--
-- Meta's Data Deletion Callback (see app/main.py: facebook_data_deletion_callback)
-- fires when a user removes your app from Facebook's "Apps and Websites"
-- settings, bypassing your own UI entirely. The signed_request Meta sends
-- contains the person's Facebook user_id — NOT the Instagram Business
-- Account id we already store in instagram_account_id. Without this
-- column, that callback has no reliable way to know which of your users
-- to delete.

ALTER TABLE public.profiles
    ADD COLUMN IF NOT EXISTS facebook_user_id TEXT;

CREATE INDEX IF NOT EXISTS profiles_facebook_user_id_idx
    ON public.profiles (facebook_user_id);

-- Same reasoning as instagram_access_token in 003: this is an identifier
-- tied to a real person's Facebook account, only the backend's service
-- role key should ever read/write it.
