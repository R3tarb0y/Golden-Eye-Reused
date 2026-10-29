param(
    [Parameter(Mandatory=$true)][string]$InputReport,
    [Parameter(Mandatory=$true)][string[]]$NasRoot,
    [int]$BatchSize = 32
)

$cmd = @(
    "py", "run_pipeline.py", "repair-report-images",
    "--source-group", "DWT",
    "--input-report", $InputReport,
    "--batch-size", "$BatchSize",
    "--apply"
)
foreach ($root in $NasRoot) {
    $cmd += @("--nas-root", $root)
}
Write-Host "TARGETED DWT REPORT IMAGE RECOVERY" -ForegroundColor Cyan
Write-Host "Hanya key DWT pada CSV yang akan dipulihkan; PWP/DIN/DAN dan row DWT sehat tidak disentuh." -ForegroundColor Yellow
Write-Host ($cmd -join ' ')
& $cmd[0] $cmd[1..($cmd.Length-1)]
