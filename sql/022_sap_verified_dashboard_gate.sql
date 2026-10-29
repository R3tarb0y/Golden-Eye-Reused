-- Golden Eye v151
-- SAP-verified dashboard gate, compatibility-safe for existing databases.
--
-- IMPORTANT:
-- Existing Golden Eye databases may have extra columns added to Gold tables by
-- older/newer hotfixes. PostgreSQL CREATE OR REPLACE VIEW cannot drop/reorder
-- columns from an existing view. Therefore every compatibility view below keeps
-- EXACTLY the composite row shape of the underlying Gold table. Known values
-- such as usage_count and aggregated lists are overridden through
-- jsonb_populate_record(), while any unknown/legacy columns are preserved.

CREATE TABLE IF NOT EXISTS gold.sap_oph_scope_key (
    source_group text NOT NULL,
    period_year integer NOT NULL,
    oph_key text NOT NULL,
    prfnr text NOT NULL,
    sap_month text NOT NULL,
    refreshed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_group, period_year, oph_key, prfnr, sap_month)
);

CREATE INDEX IF NOT EXISTS ix_sap_oph_scope_key_lookup
    ON gold.sap_oph_scope_key (source_group, period_year, sap_month, oph_key);

CREATE INDEX IF NOT EXISTS ix_sap_oph_scope_key_oph
    ON gold.sap_oph_scope_key (source_group, period_year, oph_key);

CREATE OR REPLACE FUNCTION processing.canonical_source_group(value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT CASE upper(btrim(coalesce(value, '')))
        WHEN 'ELHP_DAN' THEN 'DAN'
        WHEN 'ELHP_DWT' THEN 'DWT'
        ELSE upper(btrim(coalesce(value, '')))
    END
$$;

-- Exact SAP membership + exact month.  The output is d.* so its signature is
-- always identical to gold.reused_photo_detail, including legacy extra columns.
CREATE OR REPLACE VIEW gold.reused_photo_detail_sap_matched AS
SELECT d.*
FROM gold.reused_photo_detail d
WHERE d.is_active=true
  AND d.tanggal IS NOT NULL
  AND nullif(processing.normalize_oph(d.oph), '') IS NOT NULL
  AND EXISTS (
      SELECT 1
      FROM gold.sap_oph_scope_key k
      WHERE k.source_group=processing.canonical_source_group(d.source_group)
        AND k.period_year=extract(year FROM d.tanggal)::integer
        AND k.sap_month=to_char(d.tanggal, 'YYYY-MM')
        AND k.oph_key=processing.normalize_oph(d.oph)
  );

-- A reused finding still needs at least two SAP-proven transaction usages.
CREATE OR REPLACE VIEW gold.reused_photo_detail_sap_verified AS
WITH eligible AS (
    SELECT run_id, group_id
    FROM gold.reused_photo_detail_sap_matched
    GROUP BY run_id, group_id
    HAVING count(*) >= 2
)
SELECT d.*
FROM gold.reused_photo_detail_sap_matched d
JOIN eligible e USING (run_id, group_id);

-- Rebuild group-level business metadata from only SAP-verified members, but
-- preserve the exact physical table composite shape. This prevents
-- "cannot drop columns from view" on databases carrying legacy extra columns.
CREATE OR REPLACE VIEW gold.reused_photo_group_summary_sap_verified AS
WITH a AS (
    SELECT
        d.run_id,
        d.group_id,
        (array_agg(d.image_name ORDER BY d.is_representative DESC, d.tanggal NULLS LAST, d.file_uid))[1] AS representative_image,
        (array_agg(d.file_uid ORDER BY d.is_representative DESC, d.tanggal NULLS LAST, d.file_uid))[1] AS representative_file_uid,
        count(*)::integer AS usage_count,
        count(*) FILTER (
            WHERE coalesce(btrim(d.match_source), '') <> ''
              AND upper(coalesce(btrim(d.match_source), '')) <> 'NOT_MATCH_DB_CONTEXT'
        )::integer AS matched_image_count,
        string_agg(DISTINCT btrim(d.estate), ', ') FILTER (WHERE nullif(btrim(d.estate), '') IS NOT NULL) AS estate_list,
        string_agg(DISTINCT btrim(d.afdeling), ', ') FILTER (WHERE nullif(btrim(d.afdeling), '') IS NOT NULL) AS afdeling_list,
        string_agg(DISTINCT btrim(d.blok), ', ') FILTER (WHERE nullif(btrim(d.blok), '') IS NOT NULL) AS blok_list,
        string_agg(DISTINCT btrim(d.tph), ', ') FILTER (WHERE nullif(btrim(d.tph), '') IS NOT NULL) AS tph_list,
        string_agg(DISTINCT btrim(d.oph), ', ') FILTER (WHERE nullif(btrim(d.oph), '') IS NOT NULL) AS oph_list,
        string_agg(DISTINCT d.tanggal::text, ', ') FILTER (WHERE d.tanggal IS NOT NULL) AS sql_date_list,
        string_agg(DISTINCT btrim(d.pemanen), ', ') FILTER (WHERE nullif(btrim(d.pemanen), '') IS NOT NULL) AS pemanen_list,
        string_agg(DISTINCT btrim(d.kerani_panen), ', ') FILTER (WHERE nullif(btrim(d.kerani_panen), '') IS NOT NULL) AS kerani_panen_list,
        string_agg(DISTINCT btrim(d.mandor), ', ') FILTER (WHERE nullif(btrim(d.mandor), '') IS NOT NULL) AS mandor_list,
        string_agg(DISTINCT btrim(d.kerani_ft), ', ') FILTER (WHERE nullif(btrim(d.kerani_ft), '') IS NOT NULL) AS kerani_ft_list,
        string_agg(DISTINCT btrim(d.kerani_dt), ', ') FILTER (WHERE nullif(btrim(d.kerani_dt), '') IS NOT NULL) AS kerani_dt_list,
        string_agg(DISTINCT btrim(d.transport_status), ', ') FILTER (WHERE nullif(btrim(d.transport_status), '') IS NOT NULL) AS status_transport_list,
        string_agg(DISTINCT btrim(d.similarity), ', ') FILTER (WHERE nullif(btrim(d.similarity), '') IS NOT NULL) AS similarity_list,
        coalesce(sum(d.bunch_pay), 0)::numeric AS total_bunch_pay_group,
        CASE
            WHEN count(DISTINCT nullif(btrim(d.estate), '')) > 1 THEN 'Estate Berbeda'
            WHEN count(DISTINCT nullif(btrim(d.afdeling), '')) > 1 THEN 'Afdeling Berbeda'
            WHEN count(DISTINCT nullif(btrim(d.blok), '')) > 1 THEN 'Blok Berbeda'
            WHEN count(DISTINCT nullif(btrim(d.tph), '')) > 1 THEN 'Blok Sama / TPH Berbeda'
            ELSE 'TPH Sama'
        END AS reuse_category,
        CASE
            WHEN count(DISTINCT d.tanggal) <= 1 THEN 'Tanggal Sama'
            ELSE 'Tanggal Berbeda'
        END AS date_category,
        max(d.published_at) AS published_at
    FROM gold.reused_photo_detail_sap_verified d
    GROUP BY d.run_id, d.group_id
)
SELECT (
    jsonb_populate_record(
        NULL::gold.reused_photo_group_summary,
        to_jsonb(s) || jsonb_build_object(
            'representative_image', a.representative_image,
            'representative_file_uid', a.representative_file_uid,
            'usage_count', a.usage_count,
            'matched_image_count', a.matched_image_count,
            'estate_list', a.estate_list,
            'afdeling_list', a.afdeling_list,
            'blok_list', a.blok_list,
            'tph_list', a.tph_list,
            'oph_list', a.oph_list,
            'sql_date_list', a.sql_date_list,
            'pemanen_list', a.pemanen_list,
            'kerani_panen_list', a.kerani_panen_list,
            'mandor_list', a.mandor_list,
            'kerani_ft_list', a.kerani_ft_list,
            'kerani_dt_list', a.kerani_dt_list,
            'status_transport_list', a.status_transport_list,
            'similarity_list', a.similarity_list,
            'total_bunch_pay_group', a.total_bunch_pay_group,
            'reuse_category', a.reuse_category,
            'date_category', a.date_category,
            'reuse_date_category', a.reuse_category || ' | ' || a.date_category,
            'is_active', true,
            'published_at', a.published_at
        )
    )
).*
FROM gold.reused_photo_group_summary s
JOIN a
  ON a.run_id=s.run_id
 AND a.group_id=s.group_id
WHERE s.is_active=true;

-- Monthly rows keep exactly the base monthly table shape and only override
-- usage_count with the count of SAP-verified detail members for the same grain.
CREATE OR REPLACE VIEW gold.reused_photo_dashboard_monthly_sap_verified AS
WITH a AS (
    SELECT
        d.run_id,
        d.group_id,
        d.source_group,
        d.source_system,
        d.source_company,
        date_trunc('month', d.tanggal)::date AS month,
        coalesce(btrim(d.estate), '') AS estate,
        coalesce(btrim(d.afdeling), '') AS afdeling,
        coalesce(btrim(d.kerani_panen), '') AS kerani_panen,
        coalesce(btrim(d.mandor), '') AS mandor,
        coalesce(btrim(d.pemanen), '') AS pemanen,
        count(*)::integer AS usage_count,
        max(d.published_at) AS published_at
    FROM gold.reused_photo_detail_sap_verified d
    GROUP BY
        d.run_id, d.group_id, d.source_group, d.source_system, d.source_company,
        date_trunc('month', d.tanggal)::date,
        coalesce(btrim(d.estate), ''), coalesce(btrim(d.afdeling), ''),
        coalesce(btrim(d.kerani_panen), ''), coalesce(btrim(d.mandor), ''),
        coalesce(btrim(d.pemanen), '')
)
SELECT (
    jsonb_populate_record(
        NULL::gold.reused_photo_dashboard_monthly,
        to_jsonb(m) || jsonb_build_object(
            'usage_count', a.usage_count,
            'is_active', true,
            'published_at', a.published_at
        )
    )
).*
FROM gold.reused_photo_dashboard_monthly m
JOIN a
  ON a.run_id=m.run_id
 AND a.group_id=m.group_id
 AND a.source_group=m.source_group
 AND a.source_system=m.source_system
 AND a.source_company=m.source_company
 AND a.month=m.month
 AND a.estate=m.estate
 AND a.afdeling=m.afdeling
 AND a.kerani_panen=m.kerani_panen
 AND a.mandor=m.mandor
 AND a.pemanen=m.pemanen
WHERE m.is_active=true;

-- HARMONY compatibility views. Their column signature remains identical to the
-- pre-v150 views, therefore init-db is safely repeatable.
CREATE OR REPLACE VIEW gold.reused_photo_group_summary_cleaned AS
SELECT *
FROM gold.reused_photo_group_summary_sap_verified
WHERE upper(btrim(source_group)) IN ('PWP', 'DIN')
   OR upper(btrim(source_company)) IN ('PWP', 'DIN');

CREATE OR REPLACE VIEW gold.reused_photo_detail_transaction AS
SELECT *
FROM gold.reused_photo_detail_sap_verified
WHERE upper(btrim(source_group)) IN ('PWP', 'DIN')
   OR upper(btrim(source_company)) IN ('PWP', 'DIN');

CREATE OR REPLACE VIEW gold.reused_photo_dashboard_monthly_cleaned AS
SELECT *
FROM gold.reused_photo_dashboard_monthly_sap_verified
WHERE upper(btrim(source_group)) IN ('PWP', 'DIN')
   OR upper(btrim(source_company)) IN ('PWP', 'DIN');

-- ELHP keeps strict company/estate ownership on top of exact SAP proof.
CREATE OR REPLACE VIEW gold.elhp_gt_group_summary_cleaned AS
SELECT *
FROM gold.reused_photo_group_summary_sap_verified
WHERE (
      upper(btrim(source_group)) IN ('DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT')
      OR upper(btrim(source_company)) IN ('DAN', 'DWT')
  )
  AND processing.elhp_estate_matches_company(
      CASE
          WHEN upper(btrim(source_group)) IN ('DAN', 'ELHP_DAN') THEN 'DAN'
          WHEN upper(btrim(source_group)) IN ('DWT', 'ELHP_DWT') THEN 'DWT'
          ELSE upper(btrim(source_company))
      END,
      estate_list
  );

CREATE OR REPLACE VIEW gold.elhp_gt_detail_transaction AS
SELECT *
FROM gold.reused_photo_detail_sap_verified
WHERE (
      upper(btrim(source_group)) IN ('DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT')
      OR upper(btrim(source_company)) IN ('DAN', 'DWT')
  )
  AND processing.elhp_estate_matches_company(
      CASE
          WHEN upper(btrim(source_group)) IN ('DAN', 'ELHP_DAN') THEN 'DAN'
          WHEN upper(btrim(source_group)) IN ('DWT', 'ELHP_DWT') THEN 'DWT'
          ELSE upper(btrim(source_company))
      END,
      estate
  );

CREATE OR REPLACE VIEW gold.elhp_dashboard_monthly_cleaned AS
SELECT *
FROM gold.reused_photo_dashboard_monthly_sap_verified
WHERE (
      upper(btrim(source_group)) IN ('DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT')
      OR upper(btrim(source_company)) IN ('DAN', 'DWT')
  )
  AND processing.elhp_estate_matches_company(
      CASE
          WHEN upper(btrim(source_group)) IN ('DAN', 'ELHP_DAN') THEN 'DAN'
          WHEN upper(btrim(source_group)) IN ('DWT', 'ELHP_DWT') THEN 'DWT'
          ELSE upper(btrim(source_company))
      END,
      estate
  );

CREATE OR REPLACE VIEW gold.reused_photo_transport_status_summary AS
SELECT
    source_group,
    source_company AS company,
    transport_status AS status_transport,
    count(*)::bigint AS total
FROM gold.reused_photo_detail_transaction
GROUP BY source_group, source_company, transport_status;

CREATE OR REPLACE VIEW gold.elhp_gt_transport_status_summary AS
SELECT
    source_group,
    source_company AS company,
    'N/A - ELHP Tidak Ada Transport'::text AS status_transport,
    0::bigint AS total
FROM gold.elhp_gt_group_summary_cleaned
GROUP BY source_group, source_company;

COMMENT ON TABLE gold.sap_oph_scope_key IS
    'Exact normalized OPH membership inside each Golden Eye company SAP scope, including SAP month.';
COMMENT ON VIEW gold.reused_photo_detail_sap_verified IS
    'Only reused detail members proven by exact scoped SAP OPH + same month; group must retain >=2 usages.';
