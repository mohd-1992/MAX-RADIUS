-- Additive migration: NULL retains current package behavior; zero means unlimited.
-- No balances or existing timestamps are rewritten.
ALTER TABLE wisp_subscribers
    ADD COLUMN IF NOT EXISTS snap_volume_quota_mb BIGINT NULL DEFAULT NULL;
