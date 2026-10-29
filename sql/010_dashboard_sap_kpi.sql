-- Snapshot kecil untuk KPI SAP. Dibentuk sekali pada PUBLISH agar callback
-- dashboard tidak menjalankan COUNT DISTINCT pada jutaan row SAP berulang kali.

CREATE TABLE IF NOT EXISTS gold.sap_oph_kpi_summary (
    run_id uuid NOT NULL,
    source_group text NOT NULL,
    source_system text NOT NULL,
    source_company text NOT NULL,
    period_year integer NOT NULL,
    prfnr_scope text[] NOT NULL DEFAULT '{}',
    sap_evaluated boolean NOT NULL DEFAULT false,
    total_sap_oph bigint,
    monthly_sap_oph jsonb NOT NULL DEFAULT '{}'::jsonb,
    estate_sap_oph jsonb NOT NULL DEFAULT '{}'::jsonb,
    estate_monthly_sap_oph jsonb NOT NULL DEFAULT '{}'::jsonb,
    transaction_oph bigint NOT NULL DEFAULT 0,
    transaction_oph_in_sap bigint NOT NULL DEFAULT 0,
    transaction_oph_not_in_sap bigint NOT NULL DEFAULT 0,
    transaction_match_pct numeric(8,3),
    sap_used_by_transaction_pct numeric(8,3),
    is_active boolean NOT NULL DEFAULT true,
    published_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, source_group)
);

ALTER TABLE gold.sap_oph_kpi_summary
    ADD COLUMN IF NOT EXISTS monthly_sap_oph jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE gold.sap_oph_kpi_summary
    ADD COLUMN IF NOT EXISTS estate_sap_oph jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE gold.sap_oph_kpi_summary
    ADD COLUMN IF NOT EXISTS estate_monthly_sap_oph jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE UNIQUE INDEX IF NOT EXISTS ux_gold_sap_kpi_active_source
    ON gold.sap_oph_kpi_summary (source_group)
    WHERE is_active=true;

CREATE INDEX IF NOT EXISTS ix_gold_sap_kpi_active_system_company
    ON gold.sap_oph_kpi_summary (source_system, source_company)
    WHERE is_active=true;
