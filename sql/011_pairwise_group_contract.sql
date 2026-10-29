-- Golden Eye v1.3.17: strict all-pair reused-group contract.
-- A group is publishable only when every unique image pair has
-- min(full_embedding_similarity, center_embedding_similarity) >= configured
-- VALIDATION_MIN_PAIR_SIMILARITY (default 93.0).

ALTER TABLE processing.validated_group
    ADD COLUMN IF NOT EXISTS min_pair_similarity real;

ALTER TABLE gold.reused_photo_group
    ADD COLUMN IF NOT EXISTS min_pair_similarity real;

ALTER TABLE gold.reused_photo_group_summary
    ADD COLUMN IF NOT EXISTS min_pair_similarity real;

CREATE OR REPLACE VIEW gold.reused_photo_group_summary_cleaned AS
SELECT *
FROM gold.reused_photo_group_summary
WHERE is_active = true AND source_system = 'HARMONY';

CREATE OR REPLACE VIEW gold.elhp_gt_group_summary_cleaned AS
SELECT *
FROM gold.reused_photo_group_summary
WHERE is_active = true AND source_system = 'ELHP';

CREATE INDEX IF NOT EXISTS ix_gold_group_active_min_pair
    ON gold.reused_photo_group (source_group, min_pair_similarity)
    WHERE is_active=true;
