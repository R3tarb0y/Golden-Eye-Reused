-- V182 - Monthly RAW Context performance indexes.
-- Run with autocommit ON because CREATE INDEX CONCURRENTLY cannot run in a transaction block.

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_image_context_source_period_file
    ON processing.image_context (source_group, period_date, file_uid);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_image_context_missing_source_period
    ON processing.image_context (source_group, period_date, file_uid)
    WHERE nullif(btrim(coalesce(estate, '')), '') IS NULL
       OR nullif(btrim(coalesce(kerani_panen, '')), '') IS NULL
       OR nullif(btrim(coalesce(mandor, '')), '') IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gold_detail_file_uid_recent
    ON gold.reused_photo_detail (file_uid, is_active DESC, published_at DESC)
    INCLUDE (image_name);

ANALYZE processing.image_context;
ANALYZE gold.reused_photo_detail;
