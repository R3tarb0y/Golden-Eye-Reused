$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
while ($true) {
    try {
        & py .\run_control_worker.py
        $code = $LASTEXITCODE
        if ($code -eq 23) {
            Write-Host "[CONTROL WORKER] Worker lain sudah aktif. Wrapper berhenti agar tidak membuat worker duplikat."
            exit 0
        }
    }
    catch {
        Write-Host "[CONTROL WORKER] $($_.Exception.Message)"
    }
    Start-Sleep -Seconds 5
}
