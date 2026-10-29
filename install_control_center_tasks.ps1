param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$taskWeb = "Golden Eye Control Center Web"
$taskWorker = "Golden Eye Control Center Worker"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskWeb -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskWorker -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Scheduled tasks Golden Eye Control Center dihapus."
    exit 0
}

if (-not (Test-Path ".env")) {
    throw "File .env belum ada. Siapkan .env sebelum memasang automatic startup."
}

$powerShell = (Get-Command powershell.exe).Source
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

$workerScript = Join-Path $PSScriptRoot "control_center_worker_loop.ps1"
$webScript = Join-Path $PSScriptRoot "control_center_web_loop.ps1"
$workerAction = New-ScheduledTaskAction -Execute $powerShell -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$workerScript`"" -WorkingDirectory $PSScriptRoot
$webAction = New-ScheduledTaskAction -Execute $powerShell -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$webScript`"" -WorkingDirectory $PSScriptRoot

Register-ScheduledTask -TaskName $taskWorker -Action $workerAction -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Register-ScheduledTask -TaskName $taskWeb -Action $webAction -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

Start-ScheduledTask -TaskName $taskWorker
Start-ScheduledTask -TaskName $taskWeb

Write-Host "DONE. Golden Eye Control Center otomatis aktif setiap Anda login Windows."
Write-Host "Web: http://127.0.0.1:8060"
Write-Host "Task: $taskWorker"
Write-Host "Task: $taskWeb"
