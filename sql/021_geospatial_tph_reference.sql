CREATE SCHEMA IF NOT EXISTS reference;

CREATE TABLE IF NOT EXISTS reference.geo_tph_point (
    tph_point_id text PRIMARY KEY,
    company text NOT NULL,
    estate text,
    afdeling text,
    blok text,
    tph text NOT NULL,
    company_norm text NOT NULL,
    estate_norm text NOT NULL DEFAULT '',
    afdeling_norm text NOT NULL DEFAULT '',
    blok_norm text NOT NULL DEFAULT '',
    tph_norm text NOT NULL,
    latitude double precision NOT NULL,
    longitude double precision NOT NULL,
    observation_count integer NOT NULL DEFAULT 0,
    dispersion_p90_m double precision,
    confidence text NOT NULL DEFAULT 'LOW',
    first_observed_date date,
    last_observed_date date,
    source_relation text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    refreshed_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_geo_tph_point_company_active
    ON reference.geo_tph_point (company_norm, is_active);
CREATE INDEX IF NOT EXISTS ix_geo_tph_point_match
    ON reference.geo_tph_point (company_norm, estate_norm, afdeling_norm, blok_norm, tph_norm)
    WHERE is_active;
CREATE INDEX IF NOT EXISTS ix_geo_tph_point_block_tph
    ON reference.geo_tph_point (company_norm, blok_norm, tph_norm)
    WHERE is_active;
