-- Legacy migration marker.
--
-- Versi sebelumnya membuat processing.source_bytea_recovery_v2. Runtime baru
-- tidak lagi membuat atau membaca tabel tersebut. Hot BYTEA store final dibuat
-- oleh 013_gambar_reused.sql.
--
-- Tabel v1/v2 yang sudah ada sengaja tidak dihapus otomatis oleh init-db.
-- Penghapusan hanya boleh dilakukan setelah audit coverage gambar_reused lulus.

DO $legacy_recovery_v2$
BEGIN
    RAISE NOTICE
        'source_bytea_recovery_v2 legacy: gunakan processing.gambar_reused';
END
$legacy_recovery_v2$;
