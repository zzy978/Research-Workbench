import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const root = resolve(import.meta.dirname, "../..");
const output = resolve(process.argv[2] ?? `${root}/.local-run/history-e2e/browser`);
await mkdir(output, {recursive: true});
const { chromium } = await import(pathToFileURL(resolve(root, ".local-run/workbench/e2e-playwright/node_modules/playwright-core/index.mjs")).href);
const browser = await chromium.launch({
  executablePath: process.env.E2E_BROWSER_PATH ?? "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  headless: true,
});
const context = await browser.newContext({viewport: {width: 1440, height: 980}});
const page = await context.newPage();
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
const webUrl = process.env.E2E_WEB_URL ?? "http://127.0.0.1:5175";
const apiUrl = process.env.E2E_API_URL ?? "http://127.0.0.1:8015";
try {
  // Start from a fresh selected session even when the fixture DB has older runs.
  const sessionResponse = await context.request.post(`${apiUrl}/api/v1/sessions`, {data: {title: "历史版本回归"}});
  assert.equal(sessionResponse.status(), 201);
  const session = await sessionResponse.json();
  await page.addInitScript((sessionId) => {
    localStorage.setItem("last_session_id", sessionId);
    localStorage.setItem("chat.composerCollapsed", "0");
  }, session.session_id);
  await page.goto(webUrl, {waitUntil: "networkidle"});
  await page.getByText("联网搜索", {exact: true}).click();
  await page.getByPlaceholder("提出一个需要证据支持的问题…").fill("验证历史版本查看与回退");
  await page.getByRole("button", {name: "发送研究 →"}).click();
  const workbench = page.getByRole("region", {name: "研究工作台"});
  await workbench.getByRole("button", {name: "确认并开始调查"}).waitFor({timeout: 30000});
  const originalTitle = await workbench.getByLabel("课题名称").inputValue();
  // Save a visibly different revision through the production UI.
  await workbench.getByLabel("课题名称").fill("历史版本测试：新版标题");
  const savedResponse = page.waitForResponse((response) => response.url().endsWith("/revisions") && response.request().method() === "POST");
  await workbench.getByRole("button", {name: "保存为新版本"}).click();
  const saved = await (await savedResponse).json();
  const oldRevision = saved.current_revision - 1;
  await workbench.getByText("研究范围已更新，请核对变化后确认。", {exact: true}).waitFor();
  await workbench.getByRole("button", {name: /历史版本/}).click();
  await workbench.getByLabel("选择版本").selectOption(String(oldRevision));
  const snapshot = workbench.getByRole("region", {name: `第 ${oldRevision} 版完整内容`});
  await snapshot.waitFor();
  assert.match(await snapshot.innerText(), new RegExp(originalTitle));
  assert.equal(await snapshot.locator("input, textarea, select, button").count(), 0, "历史内容必须只读");
  assert.equal(await snapshot.locator("dt").count(), Object.keys(saved.spec).length, "全部范围字段可见");
  assert.equal(await snapshot.locator("dt").first().innerText(), "课题名称");
  assert.doesNotMatch(await snapshot.innerText(), /max_active_seconds|time_range|applies_to/);
  assert.match(await workbench.locator(".rw-history-diff").innerText(), /历史版本测试：新版标题/);
  await page.screenshot({path: resolve(output, "history-desktop.png"), fullPage: true});

  // Browsing history must preserve unsaved edits and block restoring over them.
  await workbench.getByRole("button", {name: /研究范围/}).click();
  await workbench.getByLabel("课题名称").fill("尚未保存的输入");
  await workbench.getByRole("button", {name: /历史版本/}).click();
  await workbench.getByLabel("选择版本").selectOption(String(oldRevision));
  assert.equal(await workbench.getByRole("button", {name: "回退到此版本"}).isDisabled(), true);
  await workbench.getByRole("button", {name: /研究范围/}).click();
  assert.equal(await workbench.getByLabel("课题名称").inputValue(), "尚未保存的输入");
  await workbench.getByRole("button", {name: "撤销本地修改"}).click();
  await workbench.getByRole("button", {name: /历史版本/}).click();
  await workbench.getByLabel("选择版本").selectOption(String(oldRevision));
  await workbench.getByRole("button", {name: "回退到此版本"}).click();
  await workbench.getByRole("button", {name: "取消", exact: true}).click();
  assert.equal(await workbench.getByRole("button", {name: "确认回退并创建新版本"}).count(), 0);

  await page.setViewportSize({width: 375, height: 844});
  await workbench.locator(".rw-history-panel").scrollIntoViewIfNeeded();
  await page.screenshot({path: resolve(output, "history-mobile.png"), fullPage: true});
  assert.equal(await workbench.evaluate((element) => element.scrollWidth <= element.clientWidth + 1), true, "历史页不应横向溢出");
  await workbench.getByRole("button", {name: "回退到此版本"}).click();
  const restoredResponse = page.waitForResponse((response) => response.url().endsWith("/restore"));
  await workbench.getByRole("button", {name: "确认回退并创建新版本"}).click();
  const restored = await (await restoredResponse).json();
  assert.equal(restored.current_revision, saved.current_revision + 1);
  assert.equal(restored.spec.title, originalTitle);
  assert.equal(restored.approved_revision, null);
  await workbench.getByLabel("课题名称").waitFor();
  await page.reload({waitUntil: "networkidle"});
  await workbench.getByLabel("课题名称").waitFor();
  assert.equal(await workbench.getByLabel("课题名称").inputValue(), originalTitle);
  await workbench.getByRole("button", {name: /历史版本/}).click();
  await workbench.getByLabel("选择版本").selectOption(String(saved.current_revision));
  await workbench.getByRole("region", {name: `第 ${saved.current_revision} 版完整内容`}).waitFor();
  assert.match(await workbench.locator(".rw-history-snapshot").innerText(), /历史版本测试：新版标题/);
  assert.deepEqual(errors, []);
  await writeFile(resolve(output, "result.json"), JSON.stringify({ok: true, oldRevision, restoredRevision: restored.current_revision, readOnly: true, unsavedPreserved: true, refreshPersisted: true, mobileOverflow: false, errors}, null, 2));
  console.log(JSON.stringify({ok: true, output}));
} catch (error) {
  await page.screenshot({path: resolve(output, "failure.png"), fullPage: true}).catch(() => {});
  throw error;
} finally {
  await browser.close();
}
