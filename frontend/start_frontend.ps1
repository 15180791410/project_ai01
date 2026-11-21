param([int]$Port = 8080)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$env:YUEJIAO_FRONTEND_PORT = "$Port"
python -X utf8 server.py
