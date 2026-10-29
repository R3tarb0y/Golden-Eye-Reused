-- Golden Eye V180 - SAP DB60 live-view timestamp compatibility repair
-- Safe: does not reload/delete SAP rows. It only refreshes the compatibility view.

SELECT table_schema, table_name, column_name, data_type
FROM information_schema.columns
WHERE column_name = 'migrated_at'
  AND table_schema = 'silver'
  AND table_name IN ('zpay_harvesting_db60','v_zpay_harvesting_live')
ORDER BY table_name;

CREATE OR REPLACE VIEW silver.v_zpay_harvesting_live AS
SELECT
    prfnr, budat, oph, bukrs, tph, estnr, block,
    NULL::text AS estate,
    NULL::text AS block_lapangan,
    id,
    migrated_at::timestamp without time zone AS migrated_at
FROM silver.zpay_harvesting_db60
WHERE nullif(btrim(oph), '') IS NOT NULL
  AND nullif(btrim(prfnr), '') IS NOT NULL
  AND nullif(btrim(budat), '') IS NOT NULL;

SELECT table_schema, table_name, column_name, data_type
FROM information_schema.columns
WHERE column_name = 'migrated_at'
  AND table_schema = 'silver'
  AND table_name IN ('zpay_harvesting_db60','v_zpay_harvesting_live')
ORDER BY table_name;

SELECT COUNT(*) AS september_2026_rows
FROM silver.zpay_harvesting_db60
WHERE CASE
        WHEN btrim(budat) ~ '^\d{8}$' THEN to_date(btrim(budat), 'YYYYMMDD')
        WHEN btrim(budat) ~ '^\d{4}-\d{2}-\d{2}' THEN left(btrim(budat),10)::date
        ELSE NULL
      END >= DATE '2026-09-01'
  AND CASE
        WHEN btrim(budat) ~ '^\d{8}$' THEN to_date(btrim(budat), 'YYYYMMDD')
        WHEN btrim(budat) ~ '^\d{4}-\d{2}-\d{2}' THEN left(btrim(budat),10)::date
        ELSE NULL
      END < DATE '2026-10-01';
