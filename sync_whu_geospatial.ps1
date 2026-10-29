param(
    [switch]$Apply,
    [switch]$SyncTphPoints
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$argsList = @("run_pipeline.py", "sync-whu-geospatial")
if ($SyncTphPoints) { $argsList += "--sync-tph-points" }
if ($Apply) { $argsList += "--apply" }

Write-Host "Golden Eye WHU Geospatial: DAN + DIN + DWT" -ForegroundColor Cyan
Write-Host "PWP tidak disentuh; tetap memakai source geospatial existing." -ForegroundColor DarkGray
& python @argsList
exit $LASTEXITCODE
