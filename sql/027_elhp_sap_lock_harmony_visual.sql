-- Golden Eye v162
-- Final visibility split:
--   HARMONY (PWP/DIN) = visual findings are the main dashboard population.
--   ELHP (DAN/DWT)    = main dashboard is LOCKED to exact SAP OPH + same month
--                       + reused group >=2 + strict estate ownership.
--
-- Important:
--   * DAN/DWT visual findings are NOT deleted. They remain available in the
--     Visual Audit panel / base visual views for investigation.
--   * DAN January 2026 is NOT bypassed in the main dashboard. January is kept
--     under audit because historical ownership/context is inconsistent.
--   * This file runs after 026 and intentionally overrides the v161 ELHP main
--     views while preserving DIN visual visibility.

CREATE OR REPLACE FUNCTION processing.reused_visual_display_scope_ok(
    p_source_group text,
    p_tanggal date,
    p_estate text
)
RETURNS boolean
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT CASE processing.canonical_source_group(p_source_group)
        WHEN 'DIN' THEN true
        WHEN 'DAN' THEN processing.elhp_estate_matches_company('DAN', p_estate)
        WHEN 'DWT' THEN processing.elhp_estate_matches_company('DWT', p_estate)
        WHEN 'PWP' THEN nullif(btrim(coalesce(p_estate, '')), '') IS NOT NULL
        ELSE nullif(btrim(coalesce(p_estate, '')), '') IS NOT NULL
    END
$$;

COMMENT ON FUNCTION processing.reused_visual_display_scope_ok(text,date,text) IS
    'v162 visual audit scope: DIN free; DAN strict PU* all months; DWT strict ME* all months; DAN Jan-2026 is audit-only, not a main-dashboard bypass.';

-- HARMONY stays visual-primary. 026 already points these compatibility views to
-- visual_clean; recreate explicitly so later maintenance is unambiguous.
CREATE OR REPLACE VIEW gold.reused_photo_group_summary_cleaned AS
SELECT *
FROM gold.reused_photo_group_summary_visual_clean
WHERE processing.canonical_source_group(source_group) IN ('PWP', 'DIN')
   OR upper(btrim(source_company)) IN ('PWP', 'DIN');

CREATE OR REPLACE VIEW gold.reused_photo_detail_transaction AS
SELECT *
FROM gold.reused_photo_detail_visual_clean
WHERE processing.canonical_source_group(source_group) IN ('PWP', 'DIN')
   OR upper(btrim(source_company)) IN ('PWP', 'DIN');

CREATE OR REPLACE VIEW gold.reused_photo_dashboard_monthly_cleaned AS
SELECT *
FROM gold.reused_photo_dashboard_monthly_visual_clean
WHERE processing.canonical_source_group(source_group) IN ('PWP', 'DIN')
   OR upper(btrim(source_company)) IN ('PWP', 'DIN');

-- ELHP MAIN = SAP VERIFIED. This is the anti-spike lock.
CREATE OR REPLACE VIEW gold.elhp_gt_group_summary_cleaned AS
SELECT *
FROM gold.reused_photo_group_summary_sap_verified
WHERE (
      processing.canonical_source_group(source_group) IN ('DAN', 'DWT')
      OR upper(btrim(source_company)) IN ('DAN', 'DWT')
  )
  AND processing.elhp_estate_matches_company(
      CASE
          WHEN processing.canonical_source_group(source_group)='DAN' THEN 'DAN'
          WHEN processing.canonical_source_group(source_group)='DWT' THEN 'DWT'
          ELSE upper(btrim(source_company))
      END,
      estate_list
  );

CREATE OR REPLACE VIEW gold.elhp_gt_detail_transaction AS
SELECT *
FROM gold.reused_photo_detail_sap_verified
WHERE (
      processing.canonical_source_group(source_group) IN ('DAN', 'DWT')
      OR upper(btrim(source_company)) IN ('DAN', 'DWT')
  )
  AND processing.elhp_estate_matches_company(
      CASE
          WHEN processing.canonical_source_group(source_group)='DAN' THEN 'DAN'
          WHEN processing.canonical_source_group(source_group)='DWT' THEN 'DWT'
          ELSE upper(btrim(source_company))
      END,
      estate
  );

CREATE OR REPLACE VIEW gold.elhp_dashboard_monthly_cleaned AS
SELECT *
FROM gold.reused_photo_dashboard_monthly_sap_verified
WHERE (
      processing.canonical_source_group(source_group) IN ('DAN', 'DWT')
      OR upper(btrim(source_company)) IN ('DAN', 'DWT')
  )
  AND processing.elhp_estate_matches_company(
      CASE
          WHEN processing.canonical_source_group(source_group)='DAN' THEN 'DAN'
          WHEN processing.canonical_source_group(source_group)='DWT' THEN 'DWT'
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
FROM gold.elhp_gt_group_summary_cleaned
GROUP BY source_group, source_company;

COMMENT ON VIEW gold.elhp_gt_group_summary_cleaned IS
    'v162 ELHP main groups: exact SAP same-month reused group>=2 plus strict ownership. Visual-only findings remain in Visual Audit.';
COMMENT ON VIEW gold.elhp_gt_detail_transaction IS
    'v162 ELHP main detail: exact SAP same-month plus strict DAN=PU*/DWT=ME* ownership.';
COMMENT ON VIEW gold.elhp_dashboard_monthly_cleaned IS
    'v162 ELHP main monthly dashboard is SAP-locked to prevent DAN/DWT visual spikes.';
