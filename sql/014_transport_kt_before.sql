-- Metadata ringan FFB_EVIDENCE_KT_BEFORE untuk seluruh OPH foto panen Harmony.
-- BYTEA transport tetap disimpan satu kali di processing.gambar_reused.

CREATE TABLE IF NOT EXISTS processing.transport_kt_before_context (
    source_group text NOT NULL,
    oph text NOT NULL,
    oph_key text NOT NULL,
    period_year integer NOT NULL,
    period_date date,
    evidence_path text NOT NULL,
    evidence_file_name text NOT NULL,
    evidence_key text NOT NULL,
    file_uid text NOT NULL,
    source_table text NOT NULL,
    source_row_count integer NOT NULL DEFAULT 1,
    load_status text NOT NULL,
    nas_path text,
    last_error text,
    synced_at timestamptz NOT NULL DEFAULT now(),
    is_active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (source_group, oph_key, evidence_key),
    CONSTRAINT ck_transport_kt_year CHECK (period_year BETWEEN 2000 AND 2100)
);

CREATE INDEX IF NOT EXISTS ix_transport_kt_context_active_oph
    ON processing.transport_kt_before_context
       (source_group, is_active, oph_key, period_year);

CREATE INDEX IF NOT EXISTS ix_transport_kt_context_file_uid
    ON processing.transport_kt_before_context (source_group, file_uid)
    WHERE is_active=true;

COMMENT ON TABLE processing.transport_kt_before_context IS
    'Lineage MSSQL FFB_EVIDENCE_KT_BEFORE untuk seluruh OPH foto panen Harmony; payload berada di processing.gambar_reused.';
