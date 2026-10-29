-- Canonicalise active Gold ownership and make dashboard compatibility views
-- resilient to legacy labels (ELHP_DAN / ELHP_DWT) and stale source_system.
--
-- source_group is the authoritative company key.  We intentionally do not
-- rewrite source_group because it participates in run history and audit rows.

UPDATE gold.reused_photo_group
SET source_system = CASE
        WHEN upper(btrim(source_group)) IN ('DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT') THEN 'ELHP'
        ELSE 'HARMONY'
    END,
    source_company = CASE
        WHEN upper(btrim(source_group)) IN ('DAN', 'ELHP_DAN') THEN 'DAN'
        WHEN upper(btrim(source_group)) IN ('DWT', 'ELHP_DWT') THEN 'DWT'
        WHEN upper(btrim(source_group)) = 'DIN' THEN 'DIN'
        ELSE 'PWP'
    END
WHERE is_active = true
  AND upper(btrim(source_group)) IN
      ('PWP', 'DIN', 'DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT');

UPDATE gold.reused_photo_group_summary
SET source_system = CASE
        WHEN upper(btrim(source_group)) IN ('DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT') THEN 'ELHP'
        ELSE 'HARMONY'
    END,
    source_company = CASE
        WHEN upper(btrim(source_group)) IN ('DAN', 'ELHP_DAN') THEN 'DAN'
        WHEN upper(btrim(source_group)) IN ('DWT', 'ELHP_DWT') THEN 'DWT'
        WHEN upper(btrim(source_group)) = 'DIN' THEN 'DIN'
        ELSE 'PWP'
    END
WHERE is_active = true
  AND upper(btrim(source_group)) IN
      ('PWP', 'DIN', 'DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT');

UPDATE gold.reused_photo_detail
SET source_system = CASE
        WHEN upper(btrim(source_group)) IN ('DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT') THEN 'ELHP'
        ELSE 'HARMONY'
    END,
    source_company = CASE
        WHEN upper(btrim(source_group)) IN ('DAN', 'ELHP_DAN') THEN 'DAN'
        WHEN upper(btrim(source_group)) IN ('DWT', 'ELHP_DWT') THEN 'DWT'
        WHEN upper(btrim(source_group)) = 'DIN' THEN 'DIN'
        ELSE 'PWP'
    END
WHERE is_active = true
  AND upper(btrim(source_group)) IN
      ('PWP', 'DIN', 'DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT');

UPDATE gold.reused_photo_dashboard_monthly
SET source_system = CASE
        WHEN upper(btrim(source_group)) IN ('DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT') THEN 'ELHP'
        ELSE 'HARMONY'
    END,
    source_company = CASE
        WHEN upper(btrim(source_group)) IN ('DAN', 'ELHP_DAN') THEN 'DAN'
        WHEN upper(btrim(source_group)) IN ('DWT', 'ELHP_DWT') THEN 'DWT'
        WHEN upper(btrim(source_group)) = 'DIN' THEN 'DIN'
        ELSE 'PWP'
    END
WHERE is_active = true
  AND upper(btrim(source_group)) IN
      ('PWP', 'DIN', 'DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT');

UPDATE gold.sap_oph_kpi_summary
SET source_system = CASE
        WHEN upper(btrim(source_group)) IN ('DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT') THEN 'ELHP'
        ELSE 'HARMONY'
    END,
    source_company = CASE
        WHEN upper(btrim(source_group)) IN ('DAN', 'ELHP_DAN') THEN 'DAN'
        WHEN upper(btrim(source_group)) IN ('DWT', 'ELHP_DWT') THEN 'DWT'
        WHEN upper(btrim(source_group)) = 'DIN' THEN 'DIN'
        ELSE 'PWP'
    END
WHERE is_active = true
  AND upper(btrim(source_group)) IN
      ('PWP', 'DIN', 'DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT');

-- Do not trust one historical label for visibility.  source_group and
-- source_company are accepted as recovery keys, while still keeping the two
-- dashboard systems disjoint.
CREATE OR REPLACE VIEW gold.reused_photo_group_summary_cleaned AS
SELECT *
FROM gold.reused_photo_group_summary
WHERE is_active = true
  AND (
      upper(btrim(source_group)) IN ('PWP', 'DIN')
      OR upper(btrim(source_company)) IN ('PWP', 'DIN')
  );

CREATE OR REPLACE VIEW gold.reused_photo_detail_transaction AS
SELECT *
FROM gold.reused_photo_detail
WHERE is_active = true
  AND (
      upper(btrim(source_group)) IN ('PWP', 'DIN')
      OR upper(btrim(source_company)) IN ('PWP', 'DIN')
  );

CREATE OR REPLACE VIEW gold.elhp_gt_group_summary_cleaned AS
SELECT *
FROM gold.reused_photo_group_summary
WHERE is_active = true
  AND (
      upper(btrim(source_group)) IN ('DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT')
      OR upper(btrim(source_company)) IN ('DAN', 'DWT')
  );

CREATE OR REPLACE VIEW gold.elhp_gt_detail_transaction AS
SELECT *
FROM gold.reused_photo_detail
WHERE is_active = true
  AND (
      upper(btrim(source_group)) IN ('DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT')
      OR upper(btrim(source_company)) IN ('DAN', 'DWT')
  );

CREATE OR REPLACE VIEW gold.reused_photo_dashboard_monthly_cleaned AS
SELECT *
FROM gold.reused_photo_dashboard_monthly
WHERE is_active = true
  AND (
      upper(btrim(source_group)) IN ('PWP', 'DIN')
      OR upper(btrim(source_company)) IN ('PWP', 'DIN')
  );

CREATE OR REPLACE VIEW gold.elhp_dashboard_monthly_cleaned AS
SELECT *
FROM gold.reused_photo_dashboard_monthly
WHERE is_active = true
  AND (
      upper(btrim(source_group)) IN ('DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT')
      OR upper(btrim(source_company)) IN ('DAN', 'DWT')
  );

CREATE OR REPLACE VIEW gold.reused_photo_transport_status_summary AS
SELECT
    source_group,
    source_company AS company,
    transport_status AS status_transport,
    count(*)::bigint AS total
FROM gold.reused_photo_detail
WHERE is_active = true
  AND (
      upper(btrim(source_group)) IN ('PWP', 'DIN')
      OR upper(btrim(source_company)) IN ('PWP', 'DIN')
  )
GROUP BY source_group, source_company, transport_status;

CREATE OR REPLACE VIEW gold.elhp_gt_transport_status_summary AS
SELECT
    source_group,
    source_company AS company,
    'N/A - ELHP Tidak Ada Transport'::text AS status_transport,
    0::bigint AS total
FROM gold.reused_photo_group_summary
WHERE is_active = true
  AND (
      upper(btrim(source_group)) IN ('DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT')
      OR upper(btrim(source_company)) IN ('DAN', 'DWT')
  )
GROUP BY source_group, source_company;

CREATE OR REPLACE VIEW gold.elhp_oph_visual_vs_sap_validation_file AS
SELECT *
FROM gold.oph_audit
WHERE is_active = true
  AND upper(btrim(source_group)) IN ('DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT');

CREATE OR REPLACE VIEW gold.elhp_download_report_vs_sap_validation_file AS
SELECT *
FROM gold.oph_audit
WHERE is_active = true
  AND upper(btrim(source_group)) IN ('DAN', 'DWT', 'ELHP_DAN', 'ELHP_DWT');
