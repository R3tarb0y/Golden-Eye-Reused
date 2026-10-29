param(
    [Alias("source-group")]
    [string]$SourceGroup = "ALL"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
py .\audit_dashboard_sap_visibility.py --source-group $SourceGroup
exit $LASTEXITCODE
