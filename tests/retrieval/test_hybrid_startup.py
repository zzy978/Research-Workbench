import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_compose_default_services_do_not_require_graph():
    services = yaml.safe_load((ROOT / "docker-compose.yaml").read_text(encoding="utf-8"))["services"]
    assert set(services) == {"neo4j", "backend", "frontend"}
    assert services["neo4j"]["profiles"] == ["graph"]
    assert "neo4j" not in services["backend"].get("depends_on", {})
    assert ":?" not in services["neo4j"]["environment"]["NEO4J_AUTH"]
    assert "build_rag_index.py" in (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")


@pytest.mark.parametrize("profile,expected", [(None, {"backend", "frontend"}), ("graph", {"backend", "frontend", "neo4j"})])
def test_compose_consumes_profiles_without_default_graph_password(tmp_path, profile, expected):
    docker = shutil.which("docker")
    if not docker or not (ROOT / ".env").exists():
        pytest.skip("Docker Compose and local env_file are needed for configuration validation")
    env_file = tmp_path / "empty.env"
    env_file.write_text("", encoding="utf-8")
    command = [docker, "compose", "--env-file", str(env_file)]
    if profile:
        command += ["--profile", profile]
    result = subprocess.run(command + ["config", "--services"], cwd=ROOT,
                            env={**os.environ, "NEO4J_PASSWORD": "", "COMPOSE_PROFILES": ""},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert set(result.stdout.split()) == expected


@pytest.mark.parametrize("backend,skip,expected", [("hybrid", False, False), ("graphrag", False, True), ("graphrag", True, False)])
def test_local_startup_selects_graph_only_when_requested(tmp_path, backend, skip, expected):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is needed for startup script execution")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / ".env").write_text("", encoding="utf-8")
    shutil.copyfile(ROOT / "scripts/start-local.ps1", tmp_path / "scripts/start-local.ps1")
    wrapper = tmp_path / "verify.ps1"
    wrapper.write_text('''
$global:LASTEXITCODE = 0
function python {
  if ($args[0] -eq '-c') { Write-Output $env:TEST_PRIVATE_BACKEND }
}
function docker { Add-Content -LiteralPath 'docker.log' -Value ($args -join ' ') }
function Start-Process { [pscustomobject]@{ Id = 100 } }
& "$PSScriptRoot/scripts/start-local.ps1" ''' + ("-SkipNeo4j" if skip else ""), encoding="utf-8")
    result = subprocess.run([shell, "-NoProfile", "-File", str(wrapper)], cwd=tmp_path,
                            env={**os.environ, "TEST_PRIVATE_BACKEND": backend}, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "docker.log").exists() is expected
    if expected:
        assert "--profile graph up -d neo4j" in (tmp_path / "docker.log").read_text(encoding="utf-8-sig")


def test_local_startup_prefers_project_virtual_environment():
    script = (ROOT / "scripts/start-local.ps1").read_text(encoding="utf-8")
    assert '.venv\\Scripts\\python.exe' in script
    assert 'Start-Process -FilePath $PythonExecutable' in script
