param(
    [int]$Year = 2026,
    [int]$Month = 1
)
$ErrorActionPreference = 'Stop'
py "$PSScriptRoot\audit_dan_january_2026.py" --year $Year --month $Month
