$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
py .\audit_din_dual_source.py
