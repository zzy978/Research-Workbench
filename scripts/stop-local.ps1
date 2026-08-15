$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RunDir = Join-Path $ProjectRoot ".local-run"
foreach ($Name in @("backend", "frontend")) {
  $PidFile = Join-Path $RunDir "$Name.pid"
  if (Test-Path -LiteralPath $PidFile) {
    $ProcessId = [int](Get-Content -LiteralPath $PidFile)
    Stop-Process -Id $ProcessId -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PidFile -Force
  }
}
Write-Host "Local backend/frontend stopped. Use 'docker compose stop neo4j' to stop Neo4j."
