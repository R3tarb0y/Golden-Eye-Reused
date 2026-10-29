-- Metadata path NAS foto panen. Tidak menyimpan BYTEA.
CREATE TABLE IF NOT EXISTS processing.panen_nas_path_index (
    source_group text NOT NULL,
    period_year integer NOT NULL,
    file_name_upper text NOT NULL,
    file_name text NOT NULL,
    nas_path text NOT NULL,
    source_root text NOT NULL,
    file_size_bytes bigint,
    file_mtime_ns bigint,
    expected_sha256 text,
    resolution_status text NOT NULL,
    indexed_at timestamptz NOT NULL DEFAULT now(),
    is_active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (source_group, period_year, file_name_upper)
);

CREATE INDEX IF NOT EXISTS ix_panen_nas_path_active_name
    ON processing.panen_nas_path_index
       (source_group, period_year, is_active, file_name_upper);

ALTER TABLE processing.panen_nas_path_index
    ALTER COLUMN file_size_bytes DROP NOT NULL;

COMMENT ON TABLE processing.panen_nas_path_index IS
    'Exact filename-to-NAS-path registry foto panen; dashboard membaca file langsung dari NAS tanpa menyimpan BYTEA.';
