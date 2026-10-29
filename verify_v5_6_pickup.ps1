$ErrorActionPreference="Stop"
$Project=Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Project
if (Test-Path ".\.venv\Scripts\Activate.ps1") { . ".\.venv\Scripts\Activate.ps1" }
$env:PYTHONPATH="$Project\src;$Project"
python -c "from npb_linkage.sync_pickup_db45 import verify_source_columns; print('DB45 columns OK:', sorted(verify_source_columns()))"
$Psql="C:\Program Files\PostgreSQL\18\bin\psql.exe"
& $Psql -h localhost -U postgres -d golden_eye -c "SELECT company_code,COUNT(*) rows,COUNT(*) FILTER(WHERE pickup_start_time IS NOT NULL AND pickup_end_time IS NOT NULL) complete,MIN(pickup_start_time),MAX(pickup_end_time) FROM staging.db45_pickup_event GROUP BY company_code;"
