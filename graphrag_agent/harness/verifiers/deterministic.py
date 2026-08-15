from __future__ import annotations

import json
import re
from typing import Any

from graphrag_agent.harness.contracts import ContractCheckData, SourceMode

_CITATION = re.compile(r"\[\^?(ev_[A-Za-z0-9_-]+)\]")


class DeterministicVerifiers:
    version = "1.0"

    def __init__(self, *, run_id: str, source_mode: SourceMode, report: str, evidence: list[Any]):
        self.run_id = run_id
        self.source_mode = source_mode
        self.report = report or ""
        self.evidence = evidence
        self._by_id = {str(self._get(item, "evidence_id", "")): item for item in evidence}

    @staticmethod
    def _get(item: Any, key: str, default: Any = None) -> Any:
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

    def _check(self, kind: str, passed: bool, *, expected: Any, observed: Any, explanation: str, required: bool = True, threshold: float | None = None) -> ContractCheckData:
        return ContractCheckData(
            check_id=f"contract_{self.run_id}_{kind}", run_id=self.run_id, kind=kind,
            required=required, threshold=threshold, verifier="deterministic", verifier_version=self.version,
            passed=passed, observed={"expected": expected, "actual": observed}, explanation=explanation,
        )

    def source_match(self) -> ContractCheckData:
        raw_modes = [self._get(item, "source_mode", "") for item in self.evidence]
        modes = [mode.value if hasattr(mode, "value") else str(mode) for mode in raw_modes]
        passed = all(mode == self.source_mode.value for mode in modes)
        return self._check("source_match", passed, expected=self.source_mode.value, observed=modes, explanation="全部证据必须匹配冻结的 Run source_mode")

    def min_evidence(self, minimum: int) -> ContractCheckData:
        count = len(self.evidence)
        return self._check("min_evidence", count >= minimum, expected=minimum, observed=count, threshold=float(minimum), explanation="有效 Evidence Ledger 记录数量")

    def citation_integrity(self) -> ContractCheckData:
        citations = _CITATION.findall(self.report)
        missing = sorted({citation for citation in citations if citation not in self._by_id})
        passed = bool(citations) and not missing
        return self._check("citation_integrity", passed, expected="至少一个引用且全部属于当前 Run", observed={"citations": citations, "missing": missing}, explanation="确定性解析稳定 evidence_id 引用")

    def required_section(self, required_sections: list[str]) -> ContractCheckData:
        headings = [match.group(1).strip() for match in re.finditer(r"(?m)^#{1,4}\s+(.+?)\s*$", self.report)]
        missing = [name for name in required_sections if not any(name.lower() in heading.lower() for heading in headings)]
        passed = bool(headings) and not missing
        return self._check("required_section", passed, expected=required_sections or "至少一个非空 Markdown 标题", observed={"headings": headings, "missing": missing}, explanation="报告必需章节存在且非空")

    def claim_support(self) -> ContractCheckData:
        citations = _CITATION.findall(self.report)
        supported = [citation for citation in citations if citation in self._by_id]
        passed = bool(self.report.strip()) and bool(supported)
        return self._check("claim_support", passed, expected="报告包含当前 Run 的支持证据", observed=supported, explanation="MVP 以稳定引用作为关键主张支持的确定性下限")

    def report_consistency(self, consistency_passed: bool | None) -> ContractCheckData:
        passed = consistency_passed is not False and bool(self.report.strip())
        return self._check("report_consistency", passed, expected=True, observed=consistency_passed, explanation="现有 ConsistencyChecker 未报告失败；未配置 LLM 检查时保守依赖确定性检查")

    def source_diversity(self) -> ContractCheckData:
        sources = set()
        for item in self.evidence:
            source_id = str(self._get(item, "source_id", ""))
            metadata = self._get(item, "metadata_json", "{}")
            if self.source_mode is SourceMode.WEB:
                try:
                    payload = metadata if isinstance(metadata, dict) else json.loads(metadata or "{}")
                except (TypeError, ValueError):
                    payload = {}
                sources.add(str(payload.get("domain") or source_id))
            else:
                sources.add(source_id)
        sources.discard("")
        limitation = bool(re.search(r"局限|限制|single[- ]source|limited", self.report, re.I))
        passed = len(sources) >= 2 or (bool(sources) and limitation)
        return self._check("source_diversity", passed, expected="至少两个独立来源，或明确披露单一来源局限", observed={"sources": sorted(sources), "limitation_disclosed": limitation}, explanation="来源不足不得伪造多样性")
