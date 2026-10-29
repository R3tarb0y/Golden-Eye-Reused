-- Audit evidence for the v1.3.15 low-light/night false-positive guard.
-- Existing audit rows remain valid; new columns are populated on a new VALIDATE run.
ALTER TABLE processing.validation_pair_audit
    ADD COLUMN IF NOT EXISTS raw_pair_score real,
    ADD COLUMN IF NOT EXISTS low_light_pair boolean,
    ADD COLUMN IF NOT EXISTS low_light_guard_pass boolean,
    ADD COLUMN IF NOT EXISTS left_luma_mean real,
    ADD COLUMN IF NOT EXISTS right_luma_mean real,
    ADD COLUMN IF NOT EXISTS left_dark_fraction real,
    ADD COLUMN IF NOT EXISTS right_dark_fraction real;

CREATE INDEX IF NOT EXISTS ix_validation_pair_audit_low_light
    ON processing.validation_pair_audit (run_id, low_light_pair, low_light_guard_pass)
    WHERE low_light_pair=true;
