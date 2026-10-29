-- Golden Eye v156
-- DIN historical data may exist under legacy source_group ELHP_DIN.
-- Keep the physical source_group for lineage, but canonicalise business ownership
-- to DIN/HARMONY so historical context can donate metadata to the current DIN run.

CREATE OR REPLACE FUNCTION processing.canonical_source_group(value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT CASE upper(btrim(coalesce(value, '')))
        WHEN 'ELHP_DIN' THEN 'DIN'
        WHEN 'ELHP_DAN' THEN 'DAN'
        WHEN 'ELHP_DWT' THEN 'DWT'
        ELSE upper(btrim(coalesce(value, '')))
    END
$$;

UPDATE gold.reused_photo_group
SET source_system='HARMONY', source_company='DIN'
WHERE is_active=true AND upper(btrim(source_group))='ELHP_DIN';

UPDATE gold.reused_photo_group_summary
SET source_system='HARMONY', source_company='DIN'
WHERE is_active=true AND upper(btrim(source_group))='ELHP_DIN';

UPDATE gold.reused_photo_detail
SET source_system='HARMONY', source_company='DIN'
WHERE is_active=true AND upper(btrim(source_group))='ELHP_DIN';

UPDATE gold.reused_photo_dashboard_monthly
SET source_system='HARMONY', source_company='DIN'
WHERE is_active=true AND upper(btrim(source_group))='ELHP_DIN';

UPDATE gold.sap_oph_kpi_summary
SET source_system='HARMONY', source_company='DIN'
WHERE is_active=true AND upper(btrim(source_group))='ELHP_DIN';
