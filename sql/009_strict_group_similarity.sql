-- Exact reused-group quality gate.
-- pair_score adalah fusion multimodal 0..1 dan tidak sama dengan cosine
-- similarity persen. Kolom berikut menyimpan kualitas seluruh pasangan final
-- memakai min(full_embedding_similarity, center_embedding_similarity).

ALTER TABLE processing.validated_group
    ADD COLUMN IF NOT EXISTS mean_embedding_similarity real,
    ADD COLUMN IF NOT EXISTS min_member_mean_similarity real;

ALTER TABLE processing.validated_group_member
    ADD COLUMN IF NOT EXISTS member_mean_similarity real;

ALTER TABLE gold.reused_photo_group
    ADD COLUMN IF NOT EXISTS mean_embedding_similarity real,
    ADD COLUMN IF NOT EXISTS min_member_mean_similarity real;

ALTER TABLE gold.reused_photo_group_summary
    ADD COLUMN IF NOT EXISTS mean_embedding_similarity real,
    ADD COLUMN IF NOT EXISTS min_member_mean_similarity real;

-- Pada database lama, view SELECT * yang dibuat sebelum ALTER TABLE tidak
-- otomatis mengekspos kolom baru. Recreate kedua view group setelah kolom
-- quality tersedia agar dashboard dapat menampilkan similarity reused group.
CREATE OR REPLACE VIEW gold.reused_photo_group_summary_cleaned AS
SELECT *
FROM gold.reused_photo_group_summary
WHERE is_active = true AND source_system = 'HARMONY';

CREATE OR REPLACE VIEW gold.elhp_gt_group_summary_cleaned AS
SELECT *
FROM gold.reused_photo_group_summary
WHERE is_active = true AND source_system = 'ELHP';

CREATE INDEX IF NOT EXISTS ix_gold_group_active_embedding_quality
    ON gold.reused_photo_group
       (source_group, mean_embedding_similarity, min_member_mean_similarity)
    WHERE is_active=true;
