param(
    [Parameter(Mandatory=$true)]
    [string]$ThroughMonth,
    [string]$SourceGroup = "ALL",
    [string]$StartMonth = "",
    [int]$BatchSize = 10000,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$argsList = @(
    ".\run_pipeline.py", "sync-sap-db60",
    "--through-month", $ThroughMonth,
    "--source-group", $SourceGroup,
    "--batch-size", "$BatchSize"
)
if ($StartMonth) { $argsList += @("--start-month", $StartMonth) }
if (-not $DryRun) { $argsList += "--apply" }

py @argsList
exit $LASTEXITCODE
