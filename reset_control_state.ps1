$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$taskWorker = "Golden Eye Control Center Worker"

Write-Host "[1/4] Stop + disable Control Worker task..."
Stop-ScheduledTask -TaskName $taskWorker -ErrorAction SilentlyContinue
Disable-ScheduledTask -TaskName $taskWorker -ErrorAction SilentlyContinue | Out-Null

Write-Host "[2/4] Stop worker and heavy Golden Eye child processes..."
Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match 'run_control_worker\.py' -or
    ($_.CommandLine -match 'run_pipeline\.py' -and (
        $_.CommandLine -match 'merge-month' -or
        $_.CommandLine -match 'rebuild-company' -or
        $_.CommandLine -match 'score-transport-kt-before'
    ))
} | ForEach-Object {
    Write-Host "Stopping PID $($_.ProcessId): $($_.Name)"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 2

Write-Host "[3/4] Clear orphan DB state. History is kept as INTERRUPTED..."
& py .\control_center_reset_jobs.py

Write-Host "[4/4] Verify no worker/heavy pipeline remains..."
$remaining = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match 'run_control_worker\.py' -or
    ($_.CommandLine -match 'run_pipeline\.py' -and $_.CommandLine -match 'merge-month')
}
if ($remaining) {
    $remaining | Select-Object ProcessId,ParentProcessId,Name,CommandLine | Format-Table -Wrap
    Write-Host "WARNING: process masih hidup. Jangan buat job baru sebelum process di atas dihentikan." -ForegroundColor Yellow
    exit 2
}

Write-Host ""
Write-Host "RESET OK." -ForegroundColor Green
Write-Host "- Worker OFF"
Write-Host "- Heavy pipeline OFF"
Write-Host "- Stale DB jobs = INTERRUPTED"
Write-Host "- Job baru dengan periode/company yang sama sekarang boleh dibuat"
Write-Host ""
Write-Host "Buka web:"
Write-Host "  powershell -ExecutionPolicy Bypass -File .\run_control_center.ps1"
Write-Host "Saat siap processing, start worker secara eksplisit:"
Write-Host "  powershell -ExecutionPolicy Bypass -File .\run_control_center.ps1 -StartWorker"
