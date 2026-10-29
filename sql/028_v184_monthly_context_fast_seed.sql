-- V184 monthly Context fast seed indexes. Run once with Auto Commit ON.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_image_context_month_missing_v184
    ON processing.image_context (source_group, period_date, file_uid)
    WHERE nullif(btrim(coalesce(estate, '')), '') IS NULL
       OR nullif(btrim(coalesce(kerani_panen, '')), '') IS NULL
       OR nullif(btrim(coalesce(mandor, '')), '') IS NULL;

ANALYZE processing.image_context;
ANALYZE silver.image_store_all_files;
