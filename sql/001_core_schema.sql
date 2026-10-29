CREATE SCHEMA IF NOT EXISTS processing;
CREATE SCHEMA IF NOT EXISTS gold;

CREATE OR REPLACE FUNCTION processing.basename_upper(value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT regexp_replace(
        upper(regexp_replace(replace(coalesce(value, ''), E'\\\\', '/'), '^.*/', '')),
        '^((BEFORE-AFTER_|BEFOREAFTER_|BEFORE_|AFTER_|BEFORE-|AFTER-))+',
        '',
        'g'
    )
$$;

CREATE OR REPLACE FUNCTION processing.normalize_oph(value text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT regexp_replace(upper(trim(coalesce(value, ''))), '[^A-Z0-9]', '', 'g')
$$;

CREATE TABLE IF NOT EXISTS processing.pipeline_run (
    run_id uuid PRIMARY KEY,
    source_group text NOT NULL,
    stage text NOT NULL DEFAULT 'CREATED',
    status text NOT NULL DEFAULT 'RUNNING',
    settings jsonb NOT NULL DEFAULT '{}'::jsonb,
    metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_message text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_pipeline_run_source_started
    ON processing.pipeline_run (source_group, started_at DESC);

CREATE TABLE IF NOT EXISTS processing.image_feature (
    file_uid text NOT NULL,
    model_version text NOT NULL,
    file_sha256 text,
    source_group text NOT NULL,
    file_name text NOT NULL,
    is_transport boolean NOT NULL DEFAULT false,
    embedding_dim integer,
    full_embedding bytea,
    center_embedding bytea,
    is_valid boolean NOT NULL DEFAULT false,
    error_message text,
    embedded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (file_uid, model_version)
);

ALTER TABLE processing.image_feature
    ADD COLUMN IF NOT EXISTS is_transport boolean NOT NULL DEFAULT false;
ALTER TABLE processing.image_feature
    ADD COLUMN IF NOT EXISTS center_embedding bytea;

CREATE INDEX IF NOT EXISTS ix_image_feature_source_valid
    ON processing.image_feature (source_group, model_version, is_valid);

CREATE TABLE IF NOT EXISTS processing.candidate_edge (
    run_id uuid NOT NULL REFERENCES processing.pipeline_run(run_id),
    file_uid_a text NOT NULL,
    file_uid_b text NOT NULL,
    similarity real NOT NULL,
    tier text NOT NULL CHECK (tier IN ('CONFIRMED', 'REVIEW')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, file_uid_a, file_uid_b)
);

CREATE INDEX IF NOT EXISTS ix_candidate_edge_run_tier
    ON processing.candidate_edge (run_id, tier, similarity DESC);

CREATE TABLE IF NOT EXISTS processing.candidate_group (
    run_id uuid NOT NULL REFERENCES processing.pipeline_run(run_id),
    candidate_group_id bigint NOT NULL,
    source_group text NOT NULL,
    representative_file_uid text NOT NULL,
    usage_count integer NOT NULL,
    max_edge_similarity real,
    mean_edge_similarity real,
    requires_oversize_validation boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, candidate_group_id)
);

CREATE TABLE IF NOT EXISTS processing.candidate_group_member (
    run_id uuid NOT NULL,
    candidate_group_id bigint NOT NULL,
    file_uid text NOT NULL,
    file_name text NOT NULL,
    source_group text NOT NULL,
    is_representative boolean NOT NULL DEFAULT false,
    PRIMARY KEY (run_id, candidate_group_id, file_uid),
    FOREIGN KEY (run_id, candidate_group_id)
        REFERENCES processing.candidate_group(run_id, candidate_group_id)
);

CREATE INDEX IF NOT EXISTS ix_candidate_member_file
    ON processing.candidate_group_member (run_id, file_uid);

CREATE TABLE IF NOT EXISTS processing.validated_group (
    run_id uuid NOT NULL REFERENCES processing.pipeline_run(run_id),
    validated_group_id bigint NOT NULL,
    source_group text NOT NULL,
    group_signature text NOT NULL,
    representative_file_uid text NOT NULL,
    usage_count integer NOT NULL,
    source_candidate_group_id bigint,
    validation_status text NOT NULL,
    mean_pair_score real,
    mean_embedding_similarity real,
    min_member_mean_similarity real,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, validated_group_id),
    UNIQUE (run_id, group_signature)
);

CREATE TABLE IF NOT EXISTS processing.validated_group_member (
    run_id uuid NOT NULL,
    validated_group_id bigint NOT NULL,
    file_uid text NOT NULL,
    file_name text NOT NULL,
    is_representative boolean NOT NULL DEFAULT false,
    member_validity real,
    member_mean_similarity real,
    PRIMARY KEY (run_id, validated_group_id, file_uid),
    FOREIGN KEY (run_id, validated_group_id)
        REFERENCES processing.validated_group(run_id, validated_group_id)
);

CREATE TABLE IF NOT EXISTS processing.validation_pair_audit (
    run_id uuid NOT NULL,
    candidate_group_id bigint NOT NULL,
    file_uid_a text NOT NULL,
    file_uid_b text NOT NULL,
    full_emb_sim real,
    center_emb_sim real,
    phash_dist integer,
    edgehash_dist integer,
    edgevec_sim real,
    hsvhist_sim real,
    raw_matches integer,
    inliers integer,
    inlier_ratio real,
    pair_score real,
    support_count integer,
    color_texture_trap boolean,
    same_object boolean,
    PRIMARY KEY (run_id, candidate_group_id, file_uid_a, file_uid_b)
);

CREATE TABLE IF NOT EXISTS processing.validation_progress (
    run_id uuid NOT NULL REFERENCES processing.pipeline_run(run_id),
    candidate_group_id bigint NOT NULL,
    status text NOT NULL,
    accepted_group_count integer NOT NULL DEFAULT 0,
    quarantined_group_count integer NOT NULL DEFAULT 0,
    removed_image_count integer NOT NULL DEFAULT 0,
    finished_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, candidate_group_id)
);

CREATE TABLE IF NOT EXISTS processing.pipeline_exception (
    exception_id bigserial PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES processing.pipeline_run(run_id),
    source_group text NOT NULL,
    stage text NOT NULL,
    entity_type text NOT NULL,
    entity_key text NOT NULL,
    reason text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_pipeline_exception_run
    ON processing.pipeline_exception (run_id, stage, reason);

CREATE TABLE IF NOT EXISTS processing.image_context (
    file_uid text PRIMARY KEY,
    source_group text NOT NULL,
    company_code text,
    oph text,
    oph_key text,
    period_date date,
    estate text,
    afdeling text,
    blok text,
    tph text,
    pemanen text,
    kerani_panen text,
    jjg_kerani_panen numeric,
    mandor text,
    nik_pemanen text,
    nik_kerani text,
    nik_mandor text,
    npb text,
    latitude numeric,
    longitude numeric,
    brd numeric,
    bs numeric,
    bm numeric,
    bk numeric,
    tp numeric,
    source_table text,
    source_pk text,
    match_method text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_image_context_oph
    ON processing.image_context (source_group, oph_key);

CREATE TABLE IF NOT EXISTS processing.transport_context (
    file_uid text PRIMARY KEY,
    source_group text NOT NULL,
    oph text NOT NULL,
    oph_key text NOT NULL,
    transport_kind text NOT NULL CHECK (transport_kind IN ('DT', 'FT')),
    blok text,
    tph text,
    file_name text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_transport_context_oph
    ON processing.transport_context (source_group, oph_key, transport_kind);

CREATE TABLE IF NOT EXISTS gold.group_registry (
    group_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_group text NOT NULL,
    group_signature text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_group, group_signature)
);

CREATE TABLE IF NOT EXISTS gold.reused_photo_group (
    run_id uuid NOT NULL,
    group_id bigint NOT NULL REFERENCES gold.group_registry(group_id),
    source_group text NOT NULL,
    source_system text NOT NULL,
    source_company text NOT NULL,
    representative_file_uid text NOT NULL,
    representative_image text NOT NULL,
    usage_count integer NOT NULL,
    validation_status text NOT NULL,
    mean_pair_score real,
    mean_embedding_similarity real,
    min_member_mean_similarity real,
    is_active boolean NOT NULL DEFAULT true,
    published_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, group_id)
);

CREATE INDEX IF NOT EXISTS ix_gold_group_active_source
    ON gold.reused_photo_group (source_group, is_active, usage_count DESC);

CREATE TABLE IF NOT EXISTS gold.reused_photo_detail (
    run_id uuid NOT NULL,
    group_id bigint NOT NULL,
    file_uid text NOT NULL,
    image_name text NOT NULL,
    is_representative integer NOT NULL DEFAULT 0,
    source_group text NOT NULL,
    source_system text NOT NULL,
    source_company text NOT NULL,
    estate text,
    afdeling text,
    blok text,
    tph text,
    oph text,
    tanggal date,
    pemanen text,
    kerani_panen text,
    jjg_kerani_panen numeric,
    mandor text,
    kerani_ft text,
    jjg_ft numeric,
    kerani_dt text,
    jjg_dt numeric,
    similarity text,
    similarity_score real,
    bunch_pay numeric,
    foto_dt text,
    foto_dt_uid text,
    foto_ft text,
    foto_ft_uid text,
    transport_status text,
    match_source text,
    latitude numeric,
    longitude numeric,
    is_active boolean NOT NULL DEFAULT true,
    published_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, group_id, file_uid)
);

CREATE INDEX IF NOT EXISTS ix_gold_detail_active_group
    ON gold.reused_photo_detail (source_group, is_active, group_id);
CREATE INDEX IF NOT EXISTS ix_gold_detail_image
    ON gold.reused_photo_detail (upper(image_name));
CREATE INDEX IF NOT EXISTS ix_gold_detail_oph
    ON gold.reused_photo_detail (oph);

CREATE TABLE IF NOT EXISTS gold.reused_photo_group_summary (
    run_id uuid NOT NULL,
    group_id bigint NOT NULL,
    source_group text NOT NULL,
    source_system text NOT NULL,
    source_company text NOT NULL,
    representative_image text NOT NULL,
    representative_file_uid text NOT NULL,
    usage_count integer NOT NULL,
    matched_image_count integer NOT NULL,
    mean_embedding_similarity real,
    min_member_mean_similarity real,
    estate_list text,
    afdeling_list text,
    blok_list text,
    tph_list text,
    oph_list text,
    sql_date_list text,
    pemanen_list text,
    kerani_panen_list text,
    mandor_list text,
    kerani_ft_list text,
    kerani_dt_list text,
    status_transport_list text,
    similarity_list text,
    total_bunch_pay_group numeric,
    reuse_category text,
    date_category text,
    reuse_date_category text,
    is_active boolean NOT NULL DEFAULT true,
    published_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, group_id)
);

CREATE INDEX IF NOT EXISTS ix_gold_summary_active_source
    ON gold.reused_photo_group_summary (source_group, is_active, usage_count DESC);

CREATE TABLE IF NOT EXISTS gold.oph_audit (
    run_id uuid NOT NULL,
    source_group text NOT NULL,
    oph text NOT NULL,
    oph_key text NOT NULL,
    in_business_source boolean NOT NULL,
    in_sap boolean NOT NULL,
    has_image boolean NOT NULL,
    status text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, source_group, oph_key)
);
