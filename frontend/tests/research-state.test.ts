import assert from "node:assert/strict";
import test from "node:test";
import * as research from "../src/types/research.ts";

test("只有仍在生成或研究中的课题需要自动刷新", () => {
  assert.equal(typeof research.isResearchActive, "function");
  assert.equal(research.isResearchActive({ status: "draft", run_status: "outlining" }), true);
  assert.equal(research.isResearchActive({ status: "draft", run_status: "awaiting_scope_approval" }), false);
  assert.equal(research.isResearchActive({ status: "investigating" }), true);
  assert.equal(research.isResearchActive({ status: "investigating", run_status: "completed" }), false);
  assert.equal(research.isResearchActive({ status: "awaiting_approval" }), false);
  assert.equal(research.isResearchActive({ status: "accepted" }), false);
});

test("本地差异按字段路径统计并忽略未变化值", () => {
  assert.equal(typeof research.diffResearchSpec, "function");
  const before = {
    title: "A",
    questions: ["问题一"],
    hard_constraints: [],
    items: [],
    fields: [{ id: "evidence", label: "证据", description: "", evidence_requirement: "来源", applies_to: [] }],
    scope: { time_range: "", inclusion: [], exclusion: [] },
    source_policy: "public_web",
    queries: [], sections: ["结论"],
    budget: { max_search_calls: 8, max_active_seconds: 300, max_llm_tokens: 20000 },
    stop_conditions: [],
  };
  const after = { ...before, title: "B", budget: { ...before.budget, max_search_calls: 10 } };
  assert.deepEqual(research.diffResearchSpec(before, before), []);
  assert.deepEqual(research.diffResearchSpec(before, after).map((item: { path: string }) => item.path), ["title", "budget.max_search_calls"]);
});

test("冲突错误给出保留用户输入的恢复提示", () => {
  assert.equal(typeof research.researchErrorMessage, "function");
  assert.equal(research.researchErrorMessage({ code: "CONFLICT", message: "研究范围已更新，请刷新后修改" }), "课题已被其他操作更新。你的输入仍保留，请刷新后重新提交。");
  assert.equal(research.researchErrorMessage(new Error("网络中断")), "网络中断");
  assert.equal(research.researchErrorMessage({code: "CONFLICT", message: "正在保存当前进度，请稍后重试修改"}), "正在保存当前进度，请稍后重试修改");
});

test("服务端差异会归一化为可读的前后值", () => {
  assert.equal(typeof research.normalizeResearchDiff, "function");
  assert.deepEqual(research.normalizeResearchDiff({changes: [{path: "/scope/time_range", before: "2020", after: "2024"}]}), [
    {path: "scope.time_range", before: "2020", after: "2024"},
  ]);
  assert.deepEqual(research.normalizeResearchDiff([{op: "add", path: "/questions/1", value: "问题二"}]), [
    {path: "questions.1", before: undefined, after: "问题二"},
  ]);
  assert.deepEqual(research.normalizeResearchDiff({changed_fields: ["questions", "fields"], previous_revision: 2}), [
    {path: "questions", before: "第 2 版", after: "当前版本已修改"},
    {path: "fields", before: "第 2 版", after: "当前版本已修改"},
  ]);
});

test("有完整前后值时优先展示实际差异而非字段变化占位提示", () => {
  assert.deepEqual(research.normalizeResearchDiff({
    changed_fields: ["title"], previous_revision: 2,
    changes: [{path: "title", before: "旧课题", after: "新课题"}],
  }), [{path: "title", before: "旧课题", after: "新课题"}]);
});
