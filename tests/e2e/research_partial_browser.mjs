import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const root = resolve(import.meta.dirname, "../..");
const output = resolve(process.argv[2] ?? `${root}/.local-run/partial-e2e/screenshots`);
await mkdir(output, { recursive: true });
const { chromium } = await import(pathToFileURL(resolve(root, ".local-run/workbench/e2e-playwright/node_modules/playwright-core/index.mjs")).href);
const api = process.env.E2E_API_URL ?? "http://127.0.0.1:8014";
const web = process.env.E2E_WEB_URL ?? "http://127.0.0.1:5174";
async function request(path, body) {
  const response = await fetch(api + '/api/v1' + path, body ? { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) } : undefined);
  assert.ok(response.ok, await response.clone().text());
  return response.json();
}
async function waitRun(id, status) {
  for (let i = 0; i < 100; i++) {
    const run = await request(`/runs/${id}`);
    if (run.status === status) return run;
    assert.ok(!['failed', 'budget_exhausted'].includes(run.status), JSON.stringify(run));
    await new Promise(resolve => setTimeout(resolve, 300));
  }
  throw new Error(`Run did not reach ${status}`);
}
const session = await request('/sessions', {title: '局部失败交付验证'});
const created = await request(`/sessions/${session.session_id}/messages`, {content: '比较三个框架的恢复能力', client_message_id: crypto.randomUUID(), source_mode: 'web', workflow_mode: 'deep_research'});
const draft = await waitRun(created.run_id, 'awaiting_scope_approval');
const study = await request(`/research/${draft.study_id}`);
const approved = await request(`/research/${study.study_id}/approve`, {revision: study.current_revision, fingerprint: study.fingerprint, client_request_id: crypto.randomUUID()});
await waitRun(approved.run_id, 'partial');
const matrix = await request(`/research/${study.study_id}/matrix`);
assert.equal(matrix.counts.current, 2);
assert.equal(matrix.counts.pending_retry, 1);
const browser = await chromium.launch({executablePath: process.env.E2E_BROWSER_PATH ?? 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true});
try {
  const page = await browser.newPage({viewport: {width: 1440, height: 980}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(id => localStorage.setItem('last_session_id', id), session.session_id);
  await page.goto(web, {waitUntil: 'networkidle'});
  const workbench = page.getByRole('region', {name: '研究工作台'});
  await workbench.waitFor();
  assert.match(await workbench.innerText(), /部分完成，仍有缺口/);
  await workbench.getByRole('button', {name: /证据矩阵/}).click();
  await workbench.getByText('待补查 1', {exact: true}).waitFor();
  assert.match(await workbench.innerText(), /证据校验失败 · 已尝试 3 次/);
  assert.equal(await workbench.locator('.rw-cell-content.status-pending_retry').count(), 1);
  await page.screenshot({path: resolve(output, 'partial-matrix.png'), fullPage: true});
  await workbench.getByRole('button', {name: /审阅与定稿/}).click();
  await workbench.getByText('部分报告', {exact: true}).waitFor();
  assert.equal(await workbench.getByRole('button', {name: '确认定稿'}).isDisabled(), true);
  assert.match(await workbench.locator('.rw-report').innerText(), /不能视为全部完成/);
  await page.screenshot({path: resolve(output, 'partial-report.png'), fullPage: true});
  await page.reload({waitUntil: 'networkidle'});
  await workbench.waitFor();
  assert.match(await workbench.innerText(), /部分完成，仍有缺口/);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ok: true, run_id: approved.run_id, counts: matrix.counts, output}));
} finally {
  await browser.close();
}
