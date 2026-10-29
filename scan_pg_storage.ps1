param(
    [switch]$Deep
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "============================================================"
Write-Host "Golden Eye - PostgreSQL Storage Audit (READ ONLY)"
Write-Host "============================================================"
Write-Host "Tidak ada DELETE / UPDATE / TRUNCATE / VACUUM."
Write-Host ""

$pythonArgs = @(".\scan_pg_storage.py")
if ($Deep) { $pythonArgs += "--deep" }

& py @pythonArgs
if ($LASTEXITCODE -ne 0) {
    throw "Storage audit gagal. Exit code=$LASTEXITCODE"
}
