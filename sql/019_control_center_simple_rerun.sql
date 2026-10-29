-- V143: simplify reruns. Only genuinely active PENDING/RUNNING jobs block a scope.
-- PAUSED/orphaned jobs may be superseded and historical rows remain for audit.
ALTER TABLE control.pipeline_job DROP CONSTRAINT IF EXISTS ck_control_job_status;
ALTER TABLE control.pipeline_job ADD CONSTRAINT ck_control_job_status CHECK (
    status IN ('PENDING','RUNNING','PAUSED','INTERRUPTED','SUCCESS','FAILED','PARTIAL','CANCELLED')
);

DROP INDEX IF EXISTS control.uq_control_active_job_scope;
CREATE UNIQUE INDEX IF NOT EXISTS uq_control_active_job_scope
    ON control.pipeline_job(pipeline_name, target_month, companies)
    WHERE status IN ('PENDING','RUNNING');
