param(
    [int]$Year = 2026
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

py .\audit_din_reused_loss.py --year $Year
