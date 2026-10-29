param(
    [ValidateSet("ALL","PWP","DIN","DAN","DWT")]
    [string]$Company = "ALL",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Get-DotEnvValue([string]$Key) {
    $envPath = Join-Path $Root ".env"
    if (-not (Test-Path $envPath)) { return "" }
    $match = Get-Content $envPath | Where-Object { $_ -match "^\s*$([regex]::Escape($Key))\s*=" } | Select-Object -Last 1
    if (-not $match) { return "" }
    return (($match -split "=",2)[1]).Trim().Trim('"').Trim("'")
}

Write-Host "[1/2] Memastikan schema geospatial..." -ForegroundColor Cyan
py .\run_pipeline.py init-db
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$companies = if ($Company -eq "ALL") { @("PWP","DIN","DAN","DWT") } else { @($Company) }
Write-Host "[2/2] Sinkronisasi boundary + titik referensi TPH..." -ForegroundColor Cyan

foreach ($c in $companies) {
    $key = "GEO_${c}_BOUNDARY_FILE"
    $value = Get-DotEnvValue $key
    if ([string]::IsNullOrWhiteSpace($value)) {
        Write-Host "[$c] SKIP - $key belum diisi" -ForegroundColor Yellow
        continue
    }
    Write-Host "[$c] source=$value" -ForegroundColor Gray
    $args = @(".\run_pipeline.py", "sync-geospatial", "--company", $c)
    if (-not $DryRun) { $args += "--apply" }
    & py @args
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[$c] FAILED" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

if ($DryRun) {
    Write-Host "Geospatial dry-run selesai. Tidak ada perubahan DB." -ForegroundColor Green
} else {
    Write-Host "Geospatial sync selesai. Restart dashboard; peta muncul di Detail Reused Group." -ForegroundColor Green
}
