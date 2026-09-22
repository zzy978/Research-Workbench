import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const root = resolve(import.meta.dirname, "../..");
const output = resolve(process.argv[2] ?? `${root}/.local-run/workbench/e2e-browser`);
await mkdir(output, { recursive: true });
const playwrightEntry = resolve(root, ".local-run/workbench/e2e-playwright/node_modules/playwright-core/index.mjs");
const { chromium } = await import(pathToFileURL(playwrightEntry).href);

const webUrl = process.env.E2E_WEB_URL ?? "http://127.0.0.1:5173";
const apiUrl = process.env.E2E_API_URL ?? "http://127.0.0.1:8013";
const resumeFromFirstMatrix = process.env.E2E_RESUME_FROM_FIRST_MATRIX === "1";
const edgePath = process.env.E2E_BROWSER_PATH ?? "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const browser = await chromium.launch({ executablePath: edgePath, headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 980 } });
const page = await context.newPage();
const evidence = [];
const browserErrors = [];
const failedResponses = [];
page.on("pageerror", (error) => browserErrors.push(`pageerror: ${error.message}`));
page.on("console", (message) => { if (message.type() === "error") browserErrors.push(`console: ${message.text()}`); });
page.on("response", (response) => { if (response.status() >= 400) failedResponses.push({ status: response.status(), url: response.url() }); });

async function shot(name, target = page) {
  const path = resolve(output, `${name}.png`);
  await target.screenshot({ path, fullPage: true });
  evidence.push({ name, path });
}

async function waitStudyStatus(text, target = page, timeout = 30_000) {
  await target.getByText(text, { exact: true }).first().waitFor({ state: "visible", timeout });
}

try {
  await page.goto(webUrl, { waitUntil: "networkidle" });
  const workbench = page.getByRole("region", { name: "研究工作台" });
  if (!resumeFromFirstMatrix) {
    await page.getByRole("button", { name: "＋ 新建会话" }).click();
    await page.getByText("联网搜索", { exact: true }).click();
    await page.getByPlaceholder("提出一个需要证据支持的问题…").fill("比较 Alpha、Beta 与 Gamma 的恢复能力");
    await page.getByRole("button", { name: "发送研究 →" }).click();
    await workbench.waitFor({ state: "visible", timeout: 30_000 });
    await page.getByRole("button", { name: "确认并开始调查" }).waitFor({ state: "visible", timeout: 30_000 });
    assert.match(await workbench.innerText(), /三种研究框架恢复能力比较/);
    await shot("01-outline-ready");

    // Dispatch two approval clicks in one render turn. The backend and UI must
    // converge on one research execution.
    await page.getByRole("button", { name: "确认并开始调查" }).evaluate((button) => {
      button.click();
      button.click();
    });
    await page.getByRole("button", { name: /证据矩阵/ }).click();
    await page.getByText("已完成 3", { exact: true }).waitFor({ state: "visible", timeout: 30_000 });
    assert.equal(await page.locator(".rw-cell-head span", { hasText: "有证据" }).count(), 3);
    await shot("02-first-matrix-complete");

    // Persistence after a full browser refresh.
    await page.reload({ waitUntil: "networkidle" });
  }
  await workbench.waitFor({ state: "visible", timeout: 20_000 });
  await waitStudyStatus("当前范围已确认");
  assert.match(await workbench.innerText(), /第 2 版/);
  const firstExecutions = await fetch(`${apiUrl}/__e2e/state`).then((response) => response.json());
  const executionOffset = firstExecutions.executions.length;
  if (!resumeFromFirstMatrix) assert.equal(executionOffset, 3, "重复确认不得创建重复调查");

  // Load the same revision in a second tab before the first tab edits it.
  const stalePage = await context.newPage();
  await stalePage.goto(webUrl, { waitUntil: "networkidle" });
  await stalePage.getByRole("region", { name: "研究工作台" }).waitFor({ state: "visible", timeout: 20_000 });
  assert.match(await stalePage.getByRole("region", { name: "研究工作台" }).innerText(), /第 2 版/);

  await page.getByText("对象与证据字段", { exact: true }).click();
  const fields = page.locator("fieldset.rw-object-editor").filter({ hasText: "证据字段" });
  await fields.getByRole("button", { name: "添加证据字段" }).click();
  const added = fields.locator("article").last();
  await added.getByLabel("ID", { exact: true }).fill("license");
  await added.getByLabel("显示名称", { exact: true }).fill("许可");
  await added.getByLabel("需要回答什么", { exact: true }).fill("许可条款");
  await added.getByLabel("证据要求", { exact: true }).fill("官方正文");
  await added.locator("select").selectOption("full_text");
  await page.getByText("检索与交付约束", { exact: true }).click();
  assert.equal(await page.getByLabel("允许的来源域名（留空不限制）").inputValue(), "example.org");
  await page.getByRole("button", { name: "保存为新版本" }).click();
  await page.getByText(/第 3 版/).waitFor({ state: "visible", timeout: 20_000 });
  await shot("03-added-structured-field");

  // The stale tab submits revision 2 after revision 3 exists.
  await stalePage.getByLabel("课题名称").fill("过期页面不应覆盖当前版本");
  await stalePage.getByRole("button", { name: "保存为新版本" }).click();
  await stalePage.getByText("课题已被其他操作更新。你的输入仍保留，请刷新后重新提交。", { exact: true }).waitFor({ state: "visible", timeout: 20_000 });
  assert.equal(await stalePage.getByLabel("课题名称").inputValue(), "过期页面不应覆盖当前版本");
  await shot("04-stale-submit-preserved", stalePage);
  await stalePage.close();

  await page.getByRole("button", { name: "确认并开始调查" }).evaluate((button) => {
    button.click();
    button.click();
  });

  // Do not cut the approval request itself. Wait until the new execution is
  // loaded and its event stream is live, then interrupt that live stream.
  await page.getByText("已确认当前研究范围，调查已开始。", { exact: true }).waitFor({ state: "visible", timeout: 20_000 });
  await page.getByText("实时", { exact: true }).waitFor({ state: "visible", timeout: 20_000 });

  // Simulate an actual browser network outage while the research is active.
  await context.setOffline(true);
  await page.getByText("重连中", { exact: true }).waitFor({ state: "visible", timeout: 10_000 });
  await shot("05-offline-reconnecting");
  await context.setOffline(false);
  const retry = page.getByRole("button", { name: "重新载入" });
  if (await retry.isVisible().catch(() => false)) await retry.click();

  await page.getByRole("button", { name: /证据矩阵/ }).click();
  await page.getByText("已完成 6", { exact: true }).waitFor({ state: "visible", timeout: 40_000 });
  assert.equal(await page.locator(".rw-cell-head span", { hasText: "有证据" }).count(), 6);
  const secondExecutions = await fetch(`${apiUrl}/__e2e/state`).then((response) => response.json());
  const incremental = secondExecutions.executions.slice(executionOffset);
  assert.equal(incremental.length, 3, "新增字段只应执行三个对象的三个新单元");
  assert.ok(incremental.every((query) => query.includes("license") && !query.includes("recovery")), JSON.stringify(incremental));
  await shot("06-incremental-matrix-complete");

  // A reused old cell belongs to its original execution. Opening it verifies
  // that the UI asks the evidence API for that origin instead of the latest one.
  await page.locator(".rw-matrix tbody tr").first().locator(".rw-citations button").first().click();
  await page.locator(".evidence-drawer").waitFor({ state: "visible", timeout: 20_000 });
  assert.match(await page.locator(".evidence-drawer").innerText(), /Evidence ID/);
  await shot("07-reused-origin-evidence");
  await page.getByRole("button", { name: "关闭" }).click();

  await page.getByRole("button", { name: /审阅与定稿/ }).click();
  await page.getByRole("button", { name: "确认定稿" }).waitFor({ state: "visible", timeout: 20_000 });
  await page.getByRole("button", { name: "确认定稿" }).click();
  await page.getByRole("button", { name: "已定稿", exact: true }).waitFor({ state: "visible", timeout: 20_000 });
  await shot("08-report-accepted");

  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("region", { name: "研究工作台" }).waitFor({ state: "visible", timeout: 20_000 });
  await page.getByRole("button", { name: /审阅与定稿/ }).click();
  await page.getByRole("button", { name: "已定稿", exact: true }).waitFor({ state: "visible", timeout: 20_000 });

  const result = {
    ok: true,
    assertions: {
      objects: 3,
      first_matrix_cells: 3,
      final_matrix_cells: 6,
      incremental_queries: incremental,
      duplicate_approve_executions: resumeFromFirstMatrix ? "已在前半程验证" : firstExecutions.executions.length,
      stale_input_preserved: true,
      offline_reconnected: true,
      refresh_persisted: true,
      accepted_after_refresh: true,
    },
    screenshots: evidence,
    browser_errors: browserErrors,
    failed_responses: failedResponses,
  };
  await writeFile(resolve(output, "result.json"), JSON.stringify(result, null, 2), "utf8");
  console.log(JSON.stringify(result, null, 2));
} catch (error) {
  await shot("failure").catch(() => undefined);
  await writeFile(resolve(output, "failure.txt"), String(error?.stack ?? error), "utf8");
  throw error;
} finally {
  await browser.close();
}
