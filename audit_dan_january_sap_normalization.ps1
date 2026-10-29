param([int]$Year=2026,[int]$Month=1)
$ErrorActionPreference='Stop'
py "$PSScriptRoot\audit_dan_january_sap_normalization.py" --year $Year --month $Month
exit $LASTEXITCODE
