$ErrorActionPreference="Stop"
$Project=Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Project
if (Test-Path ".\.venv\Scripts\Activate.ps1") { . ".\.venv\Scripts\Activate.ps1" }
$env:PYTHONPATH="$Project\src;$Project"
python -m dashboard_journey.app_pickup_gis
