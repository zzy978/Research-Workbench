"""Research guidance and bounded search progress."""

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar



RESEARCH_GUIDANCE = """
按用户问题选择研究深度：事实解释保持简洁，不强行拆成复杂任务。
比较或决策问题须分别覆盖选择依据、反对证据/失败条件、适用边界和替代选择。
个人决策缺少兴趣、目标、约束时先澄清；若继续回答，明确未知条件并给出条件式建议。
每个检索步骤针对尚未回答的具体问题；优先原始研究、官方资料和可核验数据，
核对作者/机构、日期、方法与适用范围，区分转载和独立来源。不能凭搜索排名认定可信。
主动检索反例、风险和不利结果。未找到反证不等于没有反证，应说明检索局限。
已有证据可以回答的问题不重复检索；连续无新增信息时停止，并保留未解决问题。
最终回应用户每项要求，区分事实、推断与建议，结论力度不得超过证据和已知个人条件。
未完成部分要明确说明，不得把证据数量、引用存在或工具成功当作完整交付。
以上为研究规则，不要将规则原文或内部制作说明复制进报告。
""".strip()


class SearchProgress:
    def __init__(self):
        self.seen = set()
        self.stale_rounds = 0

    def exhausted(self, results):
        fingerprints = {
            (item.metadata.source_id, item.metadata.content_hash or
             hashlib.sha256(str(item.evidence).encode()).hexdigest()) for item in results
        }
        self.stale_rounds = 0 if fingerprints - self.seen else self.stale_rounds + 1
        self.seen.update(fingerprints)
        return self.stale_rounds >= 2


_search_budget = ContextVar('research_search_budget', default=None)


def stage_reserves(total_tokens):
    from deepresearch_agent.config.settings import REPORT_RESERVED_TOKENS, VERIFICATION_RESERVED_TOKENS
    return (min(REPORT_RESERVED_TOKENS, int(total_tokens * .30)),
            min(VERIFICATION_RESERVED_TOKENS, int(total_tokens * .075)))


def search_ceiling(total_tokens):
    report, verification = stage_reserves(total_tokens)
    return max(0, total_tokens - report - verification - min(20000, total_tokens // 10))


@contextmanager
def search_budget(run_id, ceiling, baseline=0):
    from deepresearch_agent.models.prefix_cache import tracker
    totals = (tracker.run_snapshot(run_id) or {}).get('totals', {})
    initial = totals.get('input_tokens', 0) + totals.get('output_tokens', 0)
    token = _search_budget.set((run_id, ceiling, baseline, initial))
    try:
        yield
    finally:
        _search_budget.reset(token)


def search_budget_exhausted():
    budget = _search_budget.get()
    if budget is None:
        return False
    from deepresearch_agent.models.prefix_cache import tracker
    run_id, ceiling, baseline, initial = budget
    snapshot = tracker.run_snapshot(run_id) or {}
    totals = snapshot.get('totals', {})
    current = totals.get('input_tokens', 0) + totals.get('output_tokens', 0)
    return baseline + max(0, current - initial) >= ceiling
