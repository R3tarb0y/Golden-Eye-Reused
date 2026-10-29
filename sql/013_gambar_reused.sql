-- Hot BYTEA store khusus seluruh evidence yang dipakai Gold aktif.
--
-- Tabel ini menjadi sumber gambar dashboard dan PDF. Ia sengaja tidak diisi
-- dari Silver/recovery lama karena relation tersebut dapat rusak secara fisik.
-- Isi hanya melalui recover_corrupt_bytea.py --active-gold dari file asli.

CREATE TABLE IF NOT EXISTS processing.gambar_reused (
    source_group text NOT NULL,
    file_uid text NOT NULL,
    file_name text NOT NULL,
    mime_type text,
    file_sha256 text NOT NULL,
    bytea_len bigint NOT NULL,
    image_bytes bytea NOT NULL,
    recovery_source text NOT NULL,
    match_method text NOT NULL,
    recovered_at timestamptz NOT NULL DEFAULT now(),
    last_verified_at timestamptz NOT NULL DEFAULT now(),
    is_active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (source_group, file_uid),
    CONSTRAINT ck_gambar_reused_len
        CHECK (bytea_len > 0 AND octet_length(image_bytes) = bytea_len)
);

CREATE INDEX IF NOT EXISTS ix_gambar_reused_active_name
    ON processing.gambar_reused
       (source_group, is_active, upper(trim(file_name)));

-- ix_gambar_reused_hash sengaja tidak dibuat otomatis. Dashboard, report,
-- sync transport, dan scoring memakai primary key/nama file. Pada storage yang
-- pernah rusak, indeks non-esensial ini menjadi titik kegagalan INSERT tanpa
-- memberi manfaat pada query produksi.

COMMENT ON TABLE processing.gambar_reused IS
    'Canonical hot BYTEA store khusus gambar Gold aktif untuk dashboard/PDF.';
