-- Golden Eye v152
-- Observability for SAP DB60 -> local PostgreSQL catch-up.
CREATE SCHEMA IF NOT EXISTS control;

CREATE TABLE IF NOT EXISTS control.sap_db60_sync_audit (
    audit_id bigserial PRIMARY KEY,
    source_group text NOT NULL,
    target_month text NOT NULL,
    remote_rows bigint NOT NULL DEFAULT 0,
    fetched_rows bigint NOT NULL DEFAULT 0,
    upserted_rows bigint NOT NULL DEFAULT 0,
    local_rows_after bigint NOT NULL DEFAULT 0,
    local_max_budat date,
    remote_max_budat date,
    status text NOT NULL DEFAULT 'SUCCESS',
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz NOT NULL DEFAULT now(),
    message text
);

CREATE INDEX IF NOT EXISTS ix_sap_db60_sync_audit_scope
    ON control.sap_db60_sync_audit (source_group, target_month, finished_at DESC);
