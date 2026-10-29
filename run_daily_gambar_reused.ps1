param(
    [string]$PwpRoot = "H:\Data Harmony dan ELHP (Gambar Jan - Mei)\ffb_evidence_download",
    [string]$DinRoot = "H:\Data Harmony dan ELHP (Gambar Jan - Mei)\ffb_evidence_download_DIN",
    [string]$DanRoot = "H:\Data Harmony dan ELHP (Gambar Jan - Mei)\DAN",
    [string]$DwtRoot = "H:\Data Harmony dan ELHP (Gambar Jan - Mei)\DWT",
    [switch]$DeepAudit
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$jobs = @(
    @{ Source = "PWP"; Root = $PwpRoot },
    @{ Source = "DIN"; Root = $DinRoot },
    @{ Source = "DAN"; Root = $DanRoot },
    @{ Source = "DWT"; Root = $DwtRoot }
)

foreach ($job in $jobs) {
    $source = $job.Source
    $root = $job.Root
    if (-not (Test-Path -LiteralPath $root)) {
        throw "Root $source tidak tersedia: $root"
    }

    Write-Host ("=" * 78)
    Write-Host "SYNC GAMBAR REUSED $source"
    Write-Host ("=" * 78)
    & py recover_corrupt_bytea.py `
        --source-group $source `
        --active-gold `
        --nas-root $root `
        --batch-size 256 `
        --report "daily_gambar_reused_$($source.ToLower()).csv" `
        --apply
    if ($LASTEXITCODE -ne 0) {
        throw "Sync gambar_reused $source gagal dengan exit code $LASTEXITCODE"
    }
}

$diagnoseArgs = @(
    "dashboard_diagnose.py",
    "--audit-all-images",
    "--source-group",
    "ALL",
    "--output-dir",
    "dashboard_image_audit"
)
if ($DeepAudit) {
    $diagnoseArgs += "--deep-image-read"
}

& py @diagnoseArgs
if ($LASTEXITCODE -ne 0) {
    throw "Audit dashboard gambar_reused gagal dengan exit code $LASTEXITCODE"
}

Write-Host "DONE: gambar_reused sinkron; row yang sudah ada tidak ditulis ulang."
