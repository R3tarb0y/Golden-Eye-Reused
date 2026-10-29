-- Golden Eye v153
-- Mark the physical DB60 mirror that now lives directly in Silver.
ALTER TABLE IF EXISTS control.sap_db60_sync_audit
    ADD COLUMN IF NOT EXISTS source_relation text;
ALTER TABLE IF EXISTS control.sap_db60_sync_audit
    ADD COLUMN IF NOT EXISTS target_relation text;
ALTER TABLE IF EXISTS control.sap_db60_sync_audit
    ADD COLUMN IF NOT EXISTS sync_mode text;
