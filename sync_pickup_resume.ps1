$ErrorActionPreference="Stop"
$Project=Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Project
if (Test-Path ".\.venv\Scripts\Activate.ps1") { . ".\.venv\Scripts\Activate.ps1" }
$env:PYTHONPATH="$Project\src;$Project"
python -m npb_linkage.sync_pickup_db45 --config .\config.yaml --mode resume --batch 5000 --refresh-days 14
