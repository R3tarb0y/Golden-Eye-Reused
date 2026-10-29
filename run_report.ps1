param(
    [int]$Year,
    [int]$Month,
    [string]$PT = "",
    [string]$Estate = "",
    [string]$Progeny = "",
    [string]$Status = "",
    [int]$Evidence = 10,
    [switch]$PreviousMonth,
    [switch]$NoImages,
    [switch]$SkipDependencyCheck
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Python = Join-Path $Root ".venv_runtime\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

function Test-ReportDependencies {
    param([string]$PythonExe)

    $checkCode = @"
import importlib.util
import sys
mods = ['matplotlib', 'pandas', 'PIL', 'reportlab', 'openpyxl']
missing = [m for m in mods if importlib.util.find_spec(m) is None]
print(','.join(missing))
sys.exit(1 if missing else 0)
"@

    $missing = (& $PythonExe -c $checkCode 2>$null | Out-String).Trim()
    return [PSCustomObject]@{
        Ok = ($LASTEXITCODE -eq 0)
        Missing = $missing
    }
}

if (-not $SkipDependencyCheck) {
    $dep = Test-ReportDependencies -PythonExe $Python
    if (-not $dep.Ok) {
        Write-Host "[REPORT] Dependency Python belum lengkap: $($dep.Missing)" -ForegroundColor Yellow
        Write-Host "[REPORT] Meng-install dependency report ke interpreter yang sama:" -ForegroundColor Cyan
        Write-Host "         $Python" -ForegroundColor DarkGray

        $Requirements = Join-Path $Root "requirements-report.txt"
        if (-not (Test-Path $Requirements)) {
            throw "requirements-report.txt tidak ditemukan di $Root"
        }

        # Pastikan pip tersedia pada interpreter/venv yang benar.
        & $Python -m pip --version *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[REPORT] pip belum tersedia; mencoba ensurepip..." -ForegroundColor Yellow
            & $Python -m ensurepip --upgrade
            if ($LASTEXITCODE -ne 0) {
                throw "pip tidak tersedia pada $Python. Jalankan: $Python -m ensurepip --upgrade"
            }
        }

        & $Python -m pip install --disable-pip-version-check --prefer-binary -r $Requirements
        if ($LASTEXITCODE -ne 0) {
            throw "Install dependency report gagal. Coba manual: $Python -m pip install -r `"$Requirements`""
        }

        $dep = Test-ReportDependencies -PythonExe $Python
        if (-not $dep.Ok) {
            throw "Dependency report masih belum lengkap setelah install: $($dep.Missing)"
        }
        Write-Host "[REPORT] Dependency report lengkap." -ForegroundColor Green
    }
}

$argsList = @("report_cli.py", "--evidence", "$Evidence")
if ($PreviousMonth -or (($Year -eq 0) -and ($Month -eq 0))) {
    $argsList += "--previous-month"
} else {
    $argsList += @("--year", "$Year", "--month", "$Month")
}
if ($PT) { $argsList += @("--pt", $PT) }
if ($Estate) { $argsList += @("--estate", $Estate) }
if ($Progeny) { $argsList += @("--progeny", $Progeny) }
if ($Status) { $argsList += @("--status", $Status) }
if ($NoImages) { $argsList += "--no-images" }

& $Python @argsList
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
