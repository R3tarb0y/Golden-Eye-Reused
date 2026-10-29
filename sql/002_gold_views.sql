CREATE OR REPLACE VIEW gold.reused_photo_group_summary_cleaned AS
SELECT *
FROM gold.reused_photo_group_summary
WHERE is_active = true AND source_system = 'HARMONY';

CREATE OR REPLACE VIEW gold.reused_photo_detail_transaction AS
SELECT *
FROM gold.reused_photo_detail
WHERE is_active = true AND source_system = 'HARMONY';

CREATE OR REPLACE VIEW gold.elhp_gt_group_summary_cleaned AS
SELECT *
FROM gold.reused_photo_group_summary
WHERE is_active = true AND source_system = 'ELHP';

CREATE OR REPLACE VIEW gold.elhp_gt_detail_transaction AS
SELECT *
FROM gold.reused_photo_detail
WHERE is_active = true AND source_system = 'ELHP';

CREATE OR REPLACE VIEW gold.reused_photo_transport_status_summary AS
SELECT
    source_group,
    source_company AS company,
    transport_status AS status_transport,
    count(*)::bigint AS total
FROM gold.reused_photo_detail
WHERE is_active = true AND source_system = 'HARMONY'
GROUP BY source_group, source_company, transport_status;

CREATE OR REPLACE VIEW gold.elhp_gt_transport_status_summary AS
SELECT
    source_group,
    source_company AS company,
    'N/A - ELHP Tidak Ada Transport'::text AS status_transport,
    0::bigint AS total
FROM gold.reused_photo_group_summary
WHERE is_active = true AND source_system = 'ELHP'
GROUP BY source_group, source_company;

CREATE OR REPLACE VIEW gold.audit_download_report_vs_sap_total AS
SELECT * FROM gold.oph_audit
WHERE is_active = true;

-- Compatibility objects below are intentionally based on the new DB audit,
-- although several old names still contain "excel" or "false_alarm".
CREATE OR REPLACE VIEW gold.audit_harmony_no_image_final AS
SELECT * FROM gold.oph_audit
WHERE is_active = true AND has_image = false;

CREATE OR REPLACE VIEW gold.reused_photo_oph_validation_from_excel AS
SELECT * FROM gold.oph_audit WHERE is_active = true;

CREATE OR REPLACE VIEW gold.reused_photo_false_alarm_raw AS
SELECT
    exception_id,
    run_id,
    source_group,
    entity_key AS representative_image,
    reason,
    payload,
    created_at
FROM processing.pipeline_exception
WHERE stage = 'VALIDATE';

CREATE OR REPLACE VIEW gold.elhp_oph_visual_vs_sap_validation_file AS
SELECT * FROM gold.oph_audit
WHERE is_active = true AND source_group IN ('DAN', 'DWT');

CREATE OR REPLACE VIEW gold.elhp_download_report_vs_sap_validation_file AS
SELECT * FROM gold.oph_audit
WHERE is_active = true AND source_group IN ('DAN', 'DWT');

CREATE OR REPLACE VIEW gold.pipeline_health AS
SELECT
    r.run_id,
    r.source_group,
    r.stage,
    r.status,
    r.started_at,
    r.finished_at,
    r.metrics,
    count(e.exception_id) AS exception_count
FROM processing.pipeline_run r
LEFT JOIN processing.pipeline_exception e ON e.run_id = r.run_id
GROUP BY r.run_id, r.source_group, r.stage, r.status,
         r.started_at, r.finished_at, r.metrics;
