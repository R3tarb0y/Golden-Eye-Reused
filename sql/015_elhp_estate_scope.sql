-- Quality gate ownership estate ELHP.
-- DAN hanya menerima PU*, sedangkan DWT hanya menerima ME*. Data Gold dasar
-- tetap utuh untuk audit; compatibility view dashboard/report yang dibatasi.

CREATE OR REPLACE FUNCTION processing.elhp_estate_matches_company(
    company_value text,
    estate_value text
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT CASE upper(btrim(coalesce(company_value, '')))
        WHEN 'DAN' THEN has_token AND all_pu
        WHEN 'DWT' THEN has_token AND all_me
        ELSE false
    END
    FROM (
        SELECT
            count(*) > 0 AS has_token,
            coalesce(bool_and(upper(btrim(token)) LIKE 'PU%'), false) AS all_pu,
            coalesce(bool_and(upper(btrim(token)) LIKE 'ME%'), false) AS all_me
        FROM regexp_split_to_table(
            coalesce(estate_value, ''),
            '\s*[,;|]\s*'
        ) AS token
        WHERE btrim(token) <> ''
    ) scoped
$$;

CREATE OR REPLACE VIEW gold.elhp_gt_group_summary_cleaned AS
SELECT *
FROM gold.reused_photo_group_summary
WHERE is_active=true
  AND (
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
FROM gold.reused_photo_detail
WHERE is_active=true
  AND (
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
FROM gold.reused_photo_dashboard_monthly
WHERE is_active=true
  AND (
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

CREATE OR REPLACE VIEW gold.elhp_gt_transport_status_summary AS
SELECT
    source_group,
    source_company AS company,
    'N/A - ELHP Tidak Ada Transport'::text AS status_transport,
    0::bigint AS total
FROM gold.reused_photo_group_summary
WHERE is_active=true
  AND (
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
  )
GROUP BY source_group, source_company;

COMMENT ON FUNCTION processing.elhp_estate_matches_company(text, text) IS
    'Strict ELHP ownership gate: DAN=PU*, DWT=ME*; supports comma/semicolon/pipe lists.';
