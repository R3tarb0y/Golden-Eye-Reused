param(
    [ValidateSet("ALL","PWP","DIN")][string]$Company = "ALL",
    [int]$Year = 2026,
    [string]$Month = "",
    [string]$OutputDir = ""
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $root "dashboard\generate_transport_validation_report.py"
$argsList = @($script, "--company", $Company, "--year", "$Year")
if ($Month) { $argsList += @("--month", $Month) }
if ($OutputDir) { $argsList += @("--output-dir", $OutputDir) }

Write-Host "Generate report terpisah Panen vs Transport..." -ForegroundColor Cyan
Write-Host ("py " + ($argsList -join " "))
& py @argsList
exit $LASTEXITCODE
