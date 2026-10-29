param(
    [ValidateSet("ALL","DAN","DWT")]
    [string]$SourceGroup = "ALL",
    [int]$Year = 2026,
    [string]$Month = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$argsList = @("audit_elhp_reused_visibility.py", "--source-group", $SourceGroup, "--year", "$Year")
if ($Month) {
    $argsList += @("--month", $Month)
}

& py @argsList
exit $LASTEXITCODE
