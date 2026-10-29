param(
    [Parameter(Mandatory=$true)][string]$SourceGroup,
    [Parameter(Mandatory=$true)][string[]]$NasRoot,
    [ValidateSet("ALL","PANEN","TRANSPORT")][string]$Mode = "ALL",
    [string]$InputReport = "",
    [int]$BatchSize = 64
)

$cmd = @("py", "recover_corrupt_bytea.py", "--source-group", $SourceGroup, "--batch-size", "$BatchSize", "--apply")
if ($InputReport) {
    $cmd += @("--input-report", $InputReport, "--targeted-overlay")
} else {
    $cmd += "--active-gold"
    if ($Mode -eq "PANEN") {
        $cmd += "--panen-only"
    } elseif ($Mode -eq "TRANSPORT") {
        $cmd += "--transport-only"
    }
}
foreach ($root in $NasRoot) {
    $cmd += @("--nas-root", $root)
}
Write-Host "Menjalankan recovery evidence report:" -ForegroundColor Cyan
Write-Host ($cmd -join ' ')
& $cmd[0] $cmd[1..($cmd.Length-1)]
