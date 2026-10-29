-- Legacy migration placeholder.
--
-- Sejak v24, runtime tidak memakai processing.source_bytea_recovery karena
-- relation/TOAST v1 dapat rusak secara fisik. Migration ini sengaja tidak
-- menjalankan DDL terhadap object lama. Tabel aktif dibuat oleh
-- 013_gambar_reused.sql.

SELECT 1;
