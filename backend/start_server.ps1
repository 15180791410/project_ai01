param(
    [int]$Port = 8000,
    [switch]$NoReload
)

# 安全启动：不自动提权，也不强杀端口上的其他进程。
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $proc = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    Write-Host "Port $Port is already in use by PID $($listener.OwningProcess) ($($proc.ProcessName))." -ForegroundColor Red
    Write-Host "Close that process yourself, or start this server on another port: .\start_server.ps1 -Port 8001" -ForegroundColor Yellow
    exit 1
}

$uvicornArgs = @("-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "$Port")
if (-not $NoReload) { $uvicornArgs += "--reload" }
$env:YUEJIAO_PORT = "$Port"
Write-Host "Starting backend on 0.0.0.0:$Port ..." -ForegroundColor Green
& python @uvicornArgs
