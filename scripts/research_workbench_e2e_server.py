"""Run an isolated research-workbench app with deterministic model and Web driver.

This server exercises the production FastAPI, SQLite, SSE, run orchestration and
research storage paths. Only the external model and Web driver are replaced.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "api"))

os.environ.setdefault("LEARNING_REVIEW_ENABLED", "false")
os.environ.setdefault("MEMORY_BACKGROUND_REVIEW_ENABLED", "false")

from backend.app.main import create_app
from deepresearch_agent.config import settings
import test_research_workbench as fixture


fixture.SPEC = {
    **fixture.SPEC,
    "title": "三种研究框架恢复能力比较",
    "questions": ["比较 Alpha、Beta 与 Gamma 的恢复能力"],
    "allowed_domains": ["example.org"],
    "items": [
        {"id": "alpha", "name": "Alpha", "version": "1", "rationale": "代表框架 A"},
        {"id": "beta", "name": "Beta", "version": "1", "rationale": "代表框架 B"},
        {"id": "gamma", "name": "Gamma", "version": "1", "rationale": "代表框架 C"},
    ],
}
fixture.WebDriver.executions = []
settings.TAVILY_API_KEY = "local-e2e-fixture"


class E2EWebDriver(fixture.WebDriver):
    async def execute(self):
        await asyncio.sleep(float(os.environ.get('RESEARCH_E2E_DELAY', '8.0')))
        await super().execute()


class E2EResearchModel(fixture.FixedResearchModel):
    async def extract(self, spec, item, fields, results):
        cells = await super().extract(spec, item, fields, results)
        if os.environ.get('RESEARCH_E2E_INVALID_LOCATOR') == '1' and item['id'] == 'alpha':
            for cell in cells:
                cell['citations'][0]['locator'] = 'invalid fixture locator'
        return cells

run_root = Path(os.environ["RESEARCH_E2E_ROOT"]).resolve()
run_root.mkdir(parents=True, exist_ok=True)

app = create_app(
    database_url=f"sqlite+aiosqlite:///{(run_root / 'research.db').as_posix()}",
    artifact_root=run_root / "artifacts",
    skills_root=run_root / "skills",
    workflow_factory=E2EWebDriver,
    auto_resume=False,
)
app.state.run_service.research_model = E2EResearchModel()


@app.get("/__e2e/state")
async def e2e_state():
    return {"executions": list(fixture.WebDriver.executions), "database": str(run_root / "research.db")}
