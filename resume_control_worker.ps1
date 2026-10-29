$ErrorActionPreference = "Stop"
$taskWorker = "Golden Eye Control Center Worker"
$task = Get-ScheduledTask -TaskName $taskWorker -ErrorAction SilentlyContinue
if (-not $task) {
    throw "Scheduled Task '$taskWorker' tidak ditemukan. Jalankan install_control_center_tasks.ps1 terlebih dahulu."
}
Enable-ScheduledTask -TaskName $taskWorker | Out-Null
Start-ScheduledTask -TaskName $taskWorker
Write-Host "Worker enabled dan started."
Write-Host "Gunakan ini hanya setelah checkpoint/resume pipeline sudah diverifikasi."
