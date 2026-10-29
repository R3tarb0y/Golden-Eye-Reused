CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS control;

CREATE TABLE IF NOT EXISTS staging.db45_pickup_event (
    db45_id                text PRIMARY KEY,
    source_id_bigint       bigint,

    company_code           text NOT NULL,
    period_year            integer,
    period_month           integer,
    period_day             integer,
    period_date            date,

    department_code        text,
    afdeling_code          text,
    block                   text,
    harvest_ticket          text,
    npb_no                  text,
    npb_date                timestamp without time zone,

    nik_kr                  text,
    name_kr                 text,
    pickup_start_time       timestamp without time zone,
    pickup_end_time         timestamp without time zone,
    bunch_kt                numeric(18,3),
    brd_kt                  numeric(18,3),

    generated_at            timestamp without time zone,

    source_loaded_at        timestamptz NOT NULL DEFAULT now(),
    source_updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_db45_pickup_source_id
ON staging.db45_pickup_event(source_id_bigint)
WHERE source_id_bigint IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_db45_pickup_npb_time
ON staging.db45_pickup_event(company_code,npb_no,pickup_start_time,pickup_end_time);

CREATE INDEX IF NOT EXISTS ix_db45_pickup_oph
ON staging.db45_pickup_event(company_code,harvest_ticket);

CREATE INDEX IF NOT EXISTS ix_db45_pickup_period
ON staging.db45_pickup_event(company_code,period_date);

CREATE TABLE IF NOT EXISTS control.db45_pickup_sync_state (
    source_name             text PRIMARY KEY,
    company_code            text NOT NULL,
    period_year             integer NOT NULL,
    last_source_id          bigint NOT NULL DEFAULT 0,
    backfill_completed      boolean NOT NULL DEFAULT false,
    last_sync_started_at    timestamptz,
    last_sync_finished_at   timestamptz,
    last_sync_status        text,
    rows_fetched            bigint NOT NULL DEFAULT 0,
    rows_upserted           bigint NOT NULL DEFAULT 0,
    last_error              text,
    updated_at              timestamptz NOT NULL DEFAULT now()
);

INSERT INTO control.db45_pickup_sync_state(
    source_name,company_code,period_year,last_source_id,backfill_completed,last_sync_status
)
VALUES('DB45_PICKUP_PWP_2026','PWP',2026,0,false,'NOT_STARTED')
ON CONFLICT(source_name) DO NOTHING;

ANALYZE staging.db45_pickup_event;
