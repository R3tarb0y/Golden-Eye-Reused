-- Compact dashboard lookup for SAP coverage. The dashboard reads active Gold
-- audit rows; it never scans the large Silver SAP table per browser callback.
CREATE INDEX IF NOT EXISTS ix_gold_oph_audit_active_source_key
    ON gold.oph_audit (source_group, oph_key)
    INCLUDE (in_sap, status)
    WHERE is_active=true;
