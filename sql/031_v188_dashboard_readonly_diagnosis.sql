-- V188 READ-ONLY diagnosis: execute against golden_eye in DBeaver.
-- This script does NOT change Gold data, view definitions, or indexes.
SET statement_timeout = '30s';
-- 1) Confirm the actual deployed view definitions. File SQL alone is not proof.
SELECT schemaname, viewname, LEFT(definition, 1500) AS definition_preview
FROM pg_views WHERE schemaname='gold' AND viewname IN (
  'reused_photo_group_summary_cleaned',
  'reused_photo_group_summary_visual_clean',
  'reused_photo_detail_visual_clean',
  'reused_photo_dashboard_monthly_cleaned',
  'elhp_gt_group_summary_cleaned'
) ORDER BY viewname;
-- 2) Compare row distribution in the physical Gold layer.
SELECT source_group, is_active, COUNT(*) AS rows
FROM gold.reused_photo_group_summary
WHERE source_group IN ('PWP','DIN','DAN','DWT')
GROUP BY source_group, is_active ORDER BY source_group, is_active;
-- 3) The estimated plan of the exact slow Gold read, without executing it.
EXPLAIN (VERBOSE, COSTS, FORMAT TEXT)
SELECT * FROM gold.reused_photo_group_summary_cleaned
WHERE is_active=true AND (
 upper(btrim(source_company::text))=ANY(ARRAY['PWP'])
 OR upper(btrim(source_group::text))=ANY(ARRAY['PWP','HARMONY_PWP'])
);
-- 4) Check existing indexes before proposing another one.
SELECT schemaname, tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname IN ('gold','processing')
  AND tablename IN ('reused_photo_detail','reused_photo_group_summary','transport_kt_before_context','gambar_reused')
ORDER BY tablename,indexname;
