CREATE SCHEMA IF NOT EXISTS reference;

CREATE TABLE IF NOT EXISTS reference.geo_block_boundary (
    boundary_id text PRIMARY KEY,
    company text NOT NULL,
    estate text,
    afdeling text,
    blok text,
    company_norm text NOT NULL,
    estate_norm text NOT NULL DEFAULT '',
    afdeling_norm text NOT NULL DEFAULT '',
    blok_norm text NOT NULL DEFAULT '',
    geometry_geojson jsonb NOT NULL,
    centroid_lat double precision,
    centroid_lon double precision,
    area_ha double precision,
    source_path text NOT NULL,
    source_feature_index integer,
    source_mtime double precision,
    source_attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_active boolean NOT NULL DEFAULT true,
    imported_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_geo_block_boundary_company_active
    ON reference.geo_block_boundary (company_norm, is_active);
CREATE INDEX IF NOT EXISTS ix_geo_block_boundary_match
    ON reference.geo_block_boundary (company_norm, estate_norm, afdeling_norm, blok_norm)
    WHERE is_active;
CREATE INDEX IF NOT EXISTS ix_geo_block_boundary_block
    ON reference.geo_block_boundary (company_norm, blok_norm)
    WHERE is_active;

CREATE TABLE IF NOT EXISTS reference.geo_import_log (
    import_id bigserial PRIMARY KEY,
    company text NOT NULL,
    source_path text NOT NULL,
    feature_count integer NOT NULL DEFAULT 0,
    active_count integer NOT NULL DEFAULT 0,
    status text NOT NULL,
    message text,
    imported_at timestamptz NOT NULL DEFAULT now()
);
