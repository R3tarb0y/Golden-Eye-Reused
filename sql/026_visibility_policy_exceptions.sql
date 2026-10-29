-- Golden Eye v161
-- Explicit VISUAL DISPLAY policy exceptions requested by audit owner.
--
-- MAIN DISPLAY policy:
--   * DIN: visual findings are free from estate/SAP visibility gating.  They
--     remain visual findings; SAP verification stays a secondary status only.
--   * DAN January 2026: legacy month is allowed through the visual ownership
--     gate, because historical context/estate ownership is incomplete.  This is
--     an explicit display exception, NOT a statement that those rows are SAP
--     verified or correctly owned.
--   * DAN Feb+ and all DWT: strict ELHP estate ownership remains active.
--   * Reused definition remains >=2 display-eligible members per group.
--
-- The SAP-verified views from 022_sap_verified_dashboard_gate.sql are not
-- loosened.  They continue to be available as an independent audit signal.

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
        WHEN 'DAN' THEN
            CASE
                WHEN p_tanggal >= DATE '2026-01-01' AND p_tanggal < DATE '2026-02-01'
                    THEN true
                ELSE processing.elhp_estate_matches_company('DAN', p_estate)
            END
        WHEN 'DWT' THEN processing.elhp_estate_matches_company('DWT', p_estate)
        WHEN 'PWP' THEN nullif(btrim(coalesce(p_estate, '')), '') IS NOT NULL
        ELSE nullif(btrim(coalesce(p_estate, '')), '') IS NOT NULL
    END
$$;

COMMENT ON FUNCTION processing.reused_visual_display_scope_ok(text,date,text) IS
    'v161 visual-display policy: DIN free display; DAN Jan-2026 legacy display bypass; DAN Feb+ strict PU ownership; DWT strict ME ownership; SAP verification remains secondary.';

CREATE OR REPLACE VIEW gold.reused_photo_detail_visual_clean AS
WITH scoped AS (
    SELECT d.*,
           processing.canonical_source_group(d.source_group) AS canonical_company
    FROM gold.reused_photo_detail d
    WHERE d.is_active=true
      AND processing.reused_visual_display_scope_ok(d.source_group, d.tanggal, d.estate)
), eligible AS (
    SELECT run_id, group_id
    FROM scoped
    GROUP BY run_id, group_id
    HAVING count(*) >= 2
)
SELECT (
    jsonb_populate_record(
        NULL::gold.reused_photo_detail,
        to_jsonb(s) - 'canonical_company'
    )
).*
FROM scoped s
JOIN eligible e USING (run_id, group_id);

CREATE OR REPLACE VIEW gold.reused_photo_group_summary_visual_clean AS
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
        CASE WHEN count(DISTINCT d.tanggal) <= 1 THEN 'Tanggal Sama' ELSE 'Tanggal Berbeda' END AS date_category,
        max(d.published_at) AS published_at
    FROM gold.reused_photo_detail_visual_clean d
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
JOIN a ON a.run_id=s.run_id AND a.group_id=s.group_id
WHERE s.is_active=true;

CREATE OR REPLACE VIEW gold.reused_photo_dashboard_monthly_visual_clean AS
WITH eligible AS (
    SELECT DISTINCT run_id, group_id
    FROM gold.reused_photo_detail_visual_clean
)
SELECT m.*
FROM gold.reused_photo_dashboard_monthly m
JOIN eligible e USING (run_id, group_id)
WHERE m.is_active=true
  AND processing.reused_visual_display_scope_ok(m.source_group, m.month, m.estate);

-- MAIN/HARMONY dashboard: visual truth. DIN is fully free at display layer.
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

-- ELHP dashboard: DAN Jan-2026 has a legacy visual-display exception; DAN
-- Feb+ and all DWT remain strict through processing.reused_visual_display_scope_ok.
CREATE OR REPLACE VIEW gold.elhp_gt_group_summary_cleaned AS
SELECT *
FROM gold.reused_photo_group_summary_visual_clean
WHERE processing.canonical_source_group(source_group) IN ('DAN', 'DWT')
   OR upper(btrim(source_company)) IN ('DAN', 'DWT');

CREATE OR REPLACE VIEW gold.elhp_gt_detail_transaction AS
SELECT *
FROM gold.reused_photo_detail_visual_clean
WHERE processing.canonical_source_group(source_group) IN ('DAN', 'DWT')
   OR upper(btrim(source_company)) IN ('DAN', 'DWT');

CREATE OR REPLACE VIEW gold.elhp_dashboard_monthly_cleaned AS
SELECT *
FROM gold.reused_photo_dashboard_monthly_visual_clean
WHERE processing.canonical_source_group(source_group) IN ('DAN', 'DWT')
   OR upper(btrim(source_company)) IN ('DAN', 'DWT');

COMMENT ON VIEW gold.reused_photo_detail_visual_clean IS
    'v161 main visual display: DIN free; DAN Jan-2026 legacy bypass; DAN Feb+ strict PU; DWT strict ME; group must retain >=2 display-eligible members.';
COMMENT ON VIEW gold.reused_photo_dashboard_monthly_visual_clean IS
    'v161 main monthly visual source. SAP verification is secondary and does not hide visual findings.';
