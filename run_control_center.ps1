param(
    [switch]$NoBrowser,
    [switch]$StartWorker
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Force Python CLI/log encoding on Windows. Scheduled worker children also set
# this internally, so this is an additional guard for manual launches.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if (-not (Test-Path ".env")) {
    throw "File .env belum ada. Copy .env.example menjadi .env dan isi password/root NAS terlebih dahulu."
}

Write-Host "[1/3] Memastikan schema database termasuk control center..."
& py .\run_pipeline.py init-db
if ($LASTEXITCODE -ne 0) { throw "init-db gagal dengan exit code $LASTEXITCODE" }

$taskWorker = "Golden Eye Control Center Worker"
$taskWeb = "Golden Eye Control Center Web"
$workerTask = Get-ScheduledTask -TaskName $taskWorker -ErrorAction SilentlyContinue
$webTask = Get-ScheduledTask -TaskName $taskWeb -ErrorAction SilentlyContinue

Write-Host "[2/3] Worker policy..."
$workerText = "PAUSED"
if ($StartWorker) {
    if ($workerTask) {
        if ($workerTask.State -eq "Disabled") {
            Enable-ScheduledTask -TaskName $taskWorker | Out-Null
        }
        Start-ScheduledTask -TaskName $taskWorker
        $workerText = "Scheduled Task START REQUESTED"
    } else {
        $worker = Start-Process -FilePath "py" -ArgumentList ".\run_control_worker.py" -WorkingDirectory $PSScriptRoot -PassThru -WindowStyle Hidden
        $workerText = "PID $($worker.Id)"
    }
} else {
    Write-Host "Worker TIDAK dijalankan. Membuka web tidak pernah otomatis memulai processing."
    if ($workerTask -and $workerTask.State -eq "Running") {
        Write-Host "WARNING: Worker task sudah Running dari sebelumnya. Gunakan pause_control_center.ps1 untuk menghentikannya."
        $workerText = "ALREADY RUNNING (not started by this script)"
    } elseif ($workerTask -and $workerTask.State -eq "Disabled") {
        $workerText = "PAUSED / TASK DISABLED"
    } else {
        $workerText = "PAUSED"
    }
}

Write-Host "[3/3] Memastikan Control Center Web aktif di http://127.0.0.1:8060 ..."
if ($webTask) {
    if ($webTask.State -eq "Disabled") {
        $web = Start-Process -FilePath "py" -ArgumentList ".\run_control_center.py" -WorkingDirectory $PSScriptRoot -PassThru -WindowStyle Hidden
        $webText = "PID $($web.Id)"
    } elseif ($webTask.State -ne "Running") {
        Start-ScheduledTask -TaskName $taskWeb
        $webText = "Scheduled Task"
    } else {
        $webText = "Scheduled Task (Running)"
    }
} else {
    $web = Start-Process -FilePath "py" -ArgumentList ".\run_control_center.py" -WorkingDirectory $PSScriptRoot -PassThru -WindowStyle Hidden
    $webText = "PID $($web.Id)"
}

Start-Sleep -Seconds 2
if (-not $NoBrowser) { Start-Process "http://127.0.0.1:8060" }

Write-Host ""
Write-Host "Control Center Web aktif."
Write-Host "Worker : $workerText"
Write-Host "Web    : $webText"
Write-Host "Browser: http://127.0.0.1:8060"
Write-Host ""
Write-Host "Untuk START processing secara eksplisit:"
Write-Host "  powershell -ExecutionPolicy Bypass -File .\run_control_center.ps1 -StartWorker"
