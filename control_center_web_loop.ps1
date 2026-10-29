$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
while ($true) {
    try {
        & py .\run_control_center.py
    }
    catch {
        Write-Host "[CONTROL WEB] $($_.Exception.Message)"
    }
    Start-Sleep -Seconds 5
}
