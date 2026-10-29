param(
    [Parameter(Mandatory=$true)]
    [string]$RunId
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
py .\audit_din_legacy_sap_normalization.py --run-id $RunId
