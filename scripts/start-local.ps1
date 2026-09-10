param([switch]$SkipNeo4j)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $ProjectRoot
# src 布局：后端进程需要从 src 目录定位 deepresearch_agent 包
$env:PYTHONPATH = Join-Path $ProjectRoot "src"

if (-not (Test-Path -LiteralPath ".env")) { throw "Missing .env. Copy .env.example and configure backend keys first." }
$PythonExecutable = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonExecutable)) { $PythonExecutable = "python" }
$PrivateBackend = & $PythonExecutable -c "from deepresearch_agent.config import settings; print(settings.PRIVATE_RETRIEVAL_BACKEND)"
if ($LASTEXITCODE -ne 0 -or $PrivateBackend -notin @("hybrid", "graphrag")) {
  throw "Could not read PRIVATE_RETRIEVAL_BACKEND. Check Python dependencies and .env."
}
if ($PrivateBackend -eq "graphrag" -and -not $SkipNeo4j) {
  docker compose --profile graph up -d neo4j
  if ($LASTEXITCODE -ne 0) { throw "Neo4j startup failed." }
}
& $PythonExecutable -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw "Database migration failed." }

$RunDir = Join-Path $ProjectRoot ".local-run"
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
$Backend = Start-Process -FilePath $PythonExecutable -ArgumentList @("-m", "backend.server") -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru
$Frontend = Start-Process -FilePath "npm.cmd" -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1") -WorkingDirectory (Join-Path $ProjectRoot "frontend") -WindowStyle Hidden -PassThru
Set-Content -LiteralPath (Join-Path $RunDir "backend.pid") -Value $Backend.Id
Set-Content -LiteralPath (Join-Path $RunDir "frontend.pid") -Value $Frontend.Id
Write-Host "Started http://127.0.0.1:5173 (backend PID $($Backend.Id), frontend PID $($Frontend.Id))"
