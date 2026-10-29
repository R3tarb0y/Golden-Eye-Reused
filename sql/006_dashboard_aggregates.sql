-- Exact, compact monthly metadata for dashboard charts.
-- No BYTEA is stored here. One row represents an aggregate of Gold detail.
CREATE TABLE IF NOT EXISTS gold.reused_photo_dashboard_monthly (
    run_id uuid NOT NULL,
    group_id bigint NOT NULL,
    source_group text NOT NULL,
    source_system text NOT NULL,
    source_company text NOT NULL,
    month date NOT NULL,
    estate text NOT NULL DEFAULT '',
    afdeling text NOT NULL DEFAULT '',
    kerani_panen text NOT NULL DEFAULT '',
    mandor text NOT NULL DEFAULT '',
    pemanen text NOT NULL DEFAULT '',
    usage_count integer NOT NULL CHECK (usage_count > 0),
    is_active boolean NOT NULL DEFAULT true,
    published_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (
        run_id, group_id, month, estate, afdeling,
        kerani_panen, mandor, pemanen
    )
);

CREATE INDEX IF NOT EXISTS ix_gold_dashboard_monthly_active
    ON gold.reused_photo_dashboard_monthly
        (source_system, source_company, is_active, month, group_id);

-- Backfill current active snapshot so the dashboard becomes fast immediately
-- after init-db, including for a snapshot published before this hotfix.
INSERT INTO gold.reused_photo_dashboard_monthly
    (run_id, group_id, source_group, source_system, source_company, month,
     estate, afdeling, kerani_panen, mandor, pemanen, usage_count,
     is_active, published_at)
SELECT
    run_id,
    group_id,
    source_group,
    source_system,
    source_company,
    date_trunc('month', tanggal)::date AS month,
    coalesce(btrim(estate), ''),
    coalesce(btrim(afdeling), ''),
    coalesce(btrim(kerani_panen), ''),
    coalesce(btrim(mandor), ''),
    coalesce(btrim(pemanen), ''),
    count(*)::integer,
    true,
    max(published_at)
FROM gold.reused_photo_detail
WHERE is_active=true
  AND tanggal IS NOT NULL
  AND nullif(btrim(coalesce(estate, '')), '') IS NOT NULL
GROUP BY
    run_id, group_id, source_group, source_system, source_company,
    date_trunc('month', tanggal)::date,
    coalesce(btrim(estate), ''), coalesce(btrim(afdeling), ''),
    coalesce(btrim(kerani_panen), ''), coalesce(btrim(mandor), ''),
    coalesce(btrim(pemanen), '')
ON CONFLICT (
    run_id, group_id, month, estate, afdeling,
    kerani_panen, mandor, pemanen
) DO UPDATE SET
    usage_count=excluded.usage_count,
    is_active=true,
    published_at=excluded.published_at;

CREATE OR REPLACE VIEW gold.reused_photo_dashboard_monthly_cleaned AS
SELECT *
FROM gold.reused_photo_dashboard_monthly
WHERE is_active=true AND source_system='HARMONY';

CREATE OR REPLACE VIEW gold.elhp_dashboard_monthly_cleaned AS
SELECT *
FROM gold.reused_photo_dashboard_monthly
WHERE is_active=true AND source_system='ELHP';
