CREATE SCHEMA IF NOT EXISTS control;

CREATE TABLE IF NOT EXISTS control.pipeline_job (
    job_id uuid PRIMARY KEY,
    pipeline_name text NOT NULL,
    target_month varchar(7) NOT NULL,
    companies text[] NOT NULL DEFAULT ARRAY[]::text[],
    status text NOT NULL DEFAULT 'PENDING',
    current_step text,
    progress_pct numeric(6,2) NOT NULL DEFAULT 0,
    requested_by text NOT NULL DEFAULT 'web',
    request_source text NOT NULL DEFAULT 'WEB',
    retry_of uuid REFERENCES control.pipeline_job(job_id),
    cancel_requested boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    heartbeat_at timestamptz,
    error_message text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT ck_control_job_status CHECK (
        status IN ('PENDING','RUNNING','PAUSED','INTERRUPTED','SUCCESS','FAILED','PARTIAL','CANCELLED')
    ),
    CONSTRAINT ck_control_job_month CHECK (target_month ~ '^20[0-9]{2}-(0[1-9]|1[0-2])$')
);

CREATE TABLE IF NOT EXISTS control.pipeline_job_step (
    step_id bigserial PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES control.pipeline_job(job_id) ON DELETE CASCADE,
    step_order integer NOT NULL,
    step_key text NOT NULL,
    step_name text NOT NULL,
    company text,
    status text NOT NULL DEFAULT 'WAITING',
    command_text text,
    started_at timestamptz,
    finished_at timestamptz,
    return_code integer,
    log_path text,
    log_tail text,
    error_message text,
    attempt integer NOT NULL DEFAULT 1,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE(job_id, step_order),
    CONSTRAINT ck_control_step_status CHECK (
        status IN ('WAITING','RUNNING','SUCCESS','FAILED','SKIPPED','CANCELLED')
    )
);

CREATE TABLE IF NOT EXISTS control.pipeline_schedule (
    schedule_id integer PRIMARY KEY DEFAULT 1,
    enabled boolean NOT NULL DEFAULT false,
    pipeline_name text NOT NULL DEFAULT 'MONTHLY_FULL',
    companies text[] NOT NULL DEFAULT ARRAY['PWP','DIN','DAN','DWT']::text[],
    run_day smallint NOT NULL DEFAULT 5,
    run_hour smallint NOT NULL DEFAULT 2,
    run_minute smallint NOT NULL DEFAULT 0,
    target_mode text NOT NULL DEFAULT 'PREVIOUS_MONTH',
    last_enqueued_month varchar(7),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_control_schedule_singleton CHECK (schedule_id = 1),
    CONSTRAINT ck_control_schedule_day CHECK (run_day BETWEEN 1 AND 28),
    CONSTRAINT ck_control_schedule_hour CHECK (run_hour BETWEEN 0 AND 23),
    CONSTRAINT ck_control_schedule_minute CHECK (run_minute BETWEEN 0 AND 59)
);

INSERT INTO control.pipeline_schedule(schedule_id)
VALUES (1)
ON CONFLICT (schedule_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS control.worker_state (
    worker_id text PRIMARY KEY,
    hostname text,
    pid integer,
    status text NOT NULL DEFAULT 'OFFLINE',
    current_job_id uuid,
    started_at timestamptz NOT NULL DEFAULT now(),
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    last_error text
);

CREATE INDEX IF NOT EXISTS ix_control_job_created
    ON control.pipeline_job(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_control_job_status
    ON control.pipeline_job(status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_control_step_job
    ON control.pipeline_job_step(job_id, step_order);

-- Cegah job aktif identik dibuat dua kali dari double-click atau scheduler.
CREATE UNIQUE INDEX IF NOT EXISTS uq_control_active_job_scope
    ON control.pipeline_job(pipeline_name, target_month, companies)
    WHERE status IN ('PENDING','RUNNING');

-- V139: checkpoint/resume state. ALTER dipakai agar instalasi v136-v138 bisa
-- di-upgrade in-place tanpa menghapus history job lama.
ALTER TABLE control.pipeline_job_step
    ADD COLUMN IF NOT EXISTS resume_supported boolean NOT NULL DEFAULT false;
ALTER TABLE control.pipeline_job_step
    ADD COLUMN IF NOT EXISTS golden_run_id uuid;
ALTER TABLE control.pipeline_job_step
    ADD COLUMN IF NOT EXISTS checkpoint_stage text;
ALTER TABLE control.pipeline_job_step
    ADD COLUMN IF NOT EXISTS raw_complete boolean NOT NULL DEFAULT false;
ALTER TABLE control.pipeline_job_step
    ADD COLUMN IF NOT EXISTS silver_complete boolean NOT NULL DEFAULT false;
ALTER TABLE control.pipeline_job_step
    ADD COLUMN IF NOT EXISTS checkpoint_detail text;
ALTER TABLE control.pipeline_job_step
    ADD COLUMN IF NOT EXISTS last_checkpoint_at timestamptz;
ALTER TABLE control.pipeline_job_step
    ADD COLUMN IF NOT EXISTS resume_count integer NOT NULL DEFAULT 0;


-- V142: explicit PAUSED state. Stop means park checkpoint, never auto-resume.
ALTER TABLE control.pipeline_job DROP CONSTRAINT IF EXISTS ck_control_job_status;
ALTER TABLE control.pipeline_job ADD CONSTRAINT ck_control_job_status CHECK (
    status IN ('PENDING','RUNNING','PAUSED','INTERRUPTED','SUCCESS','FAILED','PARTIAL','CANCELLED')
);
DROP INDEX IF EXISTS control.uq_control_active_job_scope;
CREATE UNIQUE INDEX IF NOT EXISTS uq_control_active_job_scope
    ON control.pipeline_job(pipeline_name, target_month, companies)
    WHERE status IN ('PENDING','RUNNING');
