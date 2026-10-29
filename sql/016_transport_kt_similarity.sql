-- Hasil perbandingan foto panen (KP) dengan FFB_EVIDENCE_KT_BEFORE.
-- Tabel hanya menyimpan metadata/skor; BYTEA tetap satu kali di gambar_reused.

CREATE TABLE IF NOT EXISTS processing.transport_kt_similarity (
    source_group text NOT NULL,
    period_year integer NOT NULL,
    oph_key text NOT NULL,
    panen_file_uid text NOT NULL,
    panen_file_name text NOT NULL,
    transport_file_uid text NOT NULL,
    transport_file_name text NOT NULL,
    model_version text NOT NULL,
    similarity_score real,
    similarity_category text NOT NULL,
    is_same boolean,
    is_review boolean NOT NULL DEFAULT false,
    calculated_at timestamptz NOT NULL DEFAULT now(),
    is_active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (
        source_group, period_year, panen_file_uid, transport_file_uid
    )
);

CREATE INDEX IF NOT EXISTS ix_transport_kt_similarity_active_oph
    ON processing.transport_kt_similarity
       (source_group, period_year, is_active, oph_key);

COMMENT ON TABLE processing.transport_kt_similarity IS
    'Cosine similarity embedding foto KP vs KT Before pada OPH yang sama.';
