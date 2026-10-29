-- Index sumber yang aman: tidak menyentuh kolom BYTEA dan tidak mengubah data.
-- Tabel ini adalah kontrak sumber utama yang disebutkan pada kebutuhan proyek.

CREATE INDEX IF NOT EXISTS ix_image_all_source_transport_uid
    ON {{SILVER_SCHEMA}}.{{IMAGE_TABLE}} (source_group, is_transport, file_uid);

CREATE INDEX IF NOT EXISTS ix_image_all_source_name_upper
    ON {{SILVER_SCHEMA}}.{{IMAGE_TABLE}} (source_group, file_name_upper);

CREATE INDEX IF NOT EXISTS ix_image_all_sha256
    ON {{SILVER_SCHEMA}}.{{IMAGE_TABLE}} (file_sha256)
    WHERE file_sha256 IS NOT NULL;

ANALYZE {{SILVER_SCHEMA}}.{{IMAGE_TABLE}};
