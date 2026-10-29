-- Karantina source BYTEA yang pointer-nya ada tetapi isi TOAST tidak dapat dibaca.
-- Row sumber tidak dihapus/diubah; pipeline hanya melewati exception aktif.

CREATE TABLE IF NOT EXISTS processing.source_bytea_exception (
    source_group text NOT NULL,
    file_uid text NOT NULL,
    file_name text,
    error_code text NOT NULL DEFAULT 'DB_BYTEA_READ_ERROR',
    error_message text,
    first_detected_at timestamptz NOT NULL DEFAULT now(),
    last_checked_at timestamptz NOT NULL DEFAULT now(),
    is_active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (source_group, file_uid)
);

CREATE INDEX IF NOT EXISTS ix_source_bytea_exception_active
    ON processing.source_bytea_exception (source_group, is_active, file_uid);
