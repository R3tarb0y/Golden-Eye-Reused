param(
    [switch]$IncludeWeb
)

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$taskWorker = "Golden Eye Control Center Worker"
$taskWeb = "Golden Eye Control Center Web"

Write-Host "[1/5] Disable worker Scheduled Task..."
Stop-ScheduledTask -TaskName $taskWorker -ErrorAction SilentlyContinue
Disable-ScheduledTask -TaskName $taskWorker -ErrorAction SilentlyContinue | Out-Null

Write-Host "[2/5] Stop Control Worker processes..."
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'run_control_worker\.py' } | ForEach-Object {
    Write-Host "Stopping worker PID $($_.ProcessId)"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

Write-Host "[3/5] Stop Golden Eye heavy child processes..."
Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match 'run_pipeline\.py' -and
    ($_.CommandLine -match 'merge-month' -or $_.CommandLine -match 'rebuild-company' -or $_.CommandLine -match 'score-transport-kt-before')
} | ForEach-Object {
    Write-Host "Stopping pipeline PID $($_.ProcessId): $($_.CommandLine)"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

Write-Host "[4/5] Park active DB jobs as PAUSED; checkpoint tetap disimpan..."
& py .\control_center_pause_jobs.py

if ($IncludeWeb) {
    Write-Host "[5/5] Stop Control Center Web..."
    Stop-ScheduledTask -TaskName $taskWeb -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'run_control_center\.py' } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "[5/5] Web tetap aktif untuk monitoring."
}

Write-Host ""
Write-Host "PAUSED SAFE. Worker tidak aktif dan job tidak akan auto-resume."
Write-Host "Verifikasi worker/merge-month (harus kosong):"
Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match 'run_control_worker\.py' -or
    ($_.CommandLine -match 'run_pipeline\.py' -and $_.CommandLine -match 'merge-month')
} | Select-Object ProcessId, ParentProcessId, Name, CommandLine | Format-Table -Wrap
