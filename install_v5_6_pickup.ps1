$ErrorActionPreference="Stop"
$Project=Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Project
if (Test-Path ".\.venv\Scripts\Activate.ps1") { . ".\.venv\Scripts\Activate.ps1" }
$env:PYTHONPATH="$Project\src;$Project"
$Psql="C:\Program Files\PostgreSQL\18\bin\psql.exe"
& $Psql -h localhost -U postgres -d golden_eye -v ON_ERROR_STOP=1 -f "$Project\sql\060_db45_pickup_local.sql"
if ($LASTEXITCODE -ne 0) { throw "Pickup schema migration gagal." }
python -m npb_linkage.sync_pickup_db45 --config .\config.yaml --mode resume --batch 5000 --refresh-days 14
if ($LASTEXITCODE -ne 0) { throw "Initial pickup sync gagal." }
