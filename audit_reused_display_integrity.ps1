param(
    [ValidateSet("ALL","PWP","DIN","DAN","DWT")]
    [string]$SourceGroup = "ALL",
    [int]$Year = 2026
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
& py .\audit_reused_display_integrity.py --source-group $SourceGroup --year $Year
exit $LASTEXITCODE
