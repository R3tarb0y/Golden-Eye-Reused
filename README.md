# Golden Eye — Audit Analytics Platform

Source-only snapshot of the Golden Eye PostgreSQL pipeline, dashboard, and Control Center.

## Source layout

- `src/golden_eye/`: ingestion, context, image features, similarity, validation, publish, storage recovery
- `dashboard/`: Dash application, database query adapters, report generation, front-end assets
- `control_center/`: web UI, job orchestration, audit utilities, PostgreSQL storage management
- `sql/`: schema and versioned SQL migrations/views
- `tests/`: Python regression tests
- `deploy/`: optional deployment definitions (not required for native Windows)
- `tools/`: Git preflight and source-only export tools
- `run_pipeline.py`, `run_dashboard.py`, `run_control_center.py`, `run_control_worker.py`: primary entrypoints

## Local setup

1. Use Python with dependencies from `requirements.txt`.
2. Copy `.env.example` to a local `.env` and supply **your own** authorized connections, company SAP scope, credentials, and NAS mounts.
3. Ensure PostgreSQL schemas are provisioned using the project's SQL migrations; do not run migrations blindly on a production database.
4. Run `python run_dashboard.py` for the native dashboard or the Control Center launchers for pipeline operations.
5. Never commit real `.env`, exported data, image payloads, shapefiles, model weights, or PDF reports.

This is **code**, not a self-contained dataset or a runnable database backup. Business data remains in controlled infrastructure.

See [`README_GIT.md`](README_GIT.md) for code-only export and dual GitHub/GitLab push. See [`SECURITY.md`](SECURITY.md) for safe handling of credentials and internal business metadata.
