// Build first. Runs against a disposable local backend; never accepts a production URL.
// PYTHON=/path/to/venv/bin/python npm run test:cookiecloud
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, rm, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium, webkit } from "playwright-core";

const root = fileURLToPath(new URL("../../", import.meta.url));
const home = await mkdtemp(resolve(tmpdir(), "assetizer-cookiecloud-"));
const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith("ASSETIZER_")));
Object.assign(env, { PYTHONPATH: resolve(root, "src"), ASSETIZER_HOME: home });
const server = spawn(process.env.PYTHON || "python3", ["-m", "uvicorn", "assetizer.web.app:app", "--host", "127.0.0.1", "--port", "0"],
  { cwd: root, env, stdio: ["ignore", "pipe", "pipe"] });
const token = "ccat_Q7mK2pV9xL4bN8zR1tY6hW3dJ5fG0cUaE2sT4vB8nM"; // synthetic
let browser;
try {
  const url = await new Promise((resolveUrl, reject) => {
    const timer = setTimeout(() => reject(new Error("Local test server did not start")), 10000);
    server.on("error", reject);
    server.once("exit", (code) => reject(new Error(`Local test server exited: ${code}`)));
    server.stderr.on("data", (data) => {
      const match = data.toString().match(/Uvicorn running on (http:\/\/127\.0\.0\.1:\d+)/);
      if (match) { clearTimeout(timer); resolveUrl(match[1]); }
    });
  });
  browser = process.env.BROWSER === "webkit" ? await webkit.launch() : await chromium.launch({
    executablePath: process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });
  for (const width of [1440, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 844 } });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const reset = await context.request.post(`${url}/api/cookiecloud/credentials`, {
      data: { clear: ["origin", "access_client_id", "access_client_secret", "app_token"] },
    });
    assert.equal(reset.status(), 200);
    await page.goto(`${url}/engine`);
    await page.getByRole("region", { name: "系统" }).getByRole("link", { name: /^YouTube/ }).click();
    await page.waitForURL("**/settings#youtube");
    await page.locator("#youtube").getByRole("button", { name: "配置 CookieCloud", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("App Token", { exact: true }).fill(token);
    // A successful mutation already returns fresh metadata. A failed refresh must not hide it or block testing.
    await page.route("**/api/settings", (route) => route.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"refresh unavailable"}' }));
    await dialog.getByRole("button", { name: "保存第 2、3 步", exact: true }).click();
    await dialog.getByText("已保存。接着做第 4 步：先「测试连接」，再「视频测试」。", { exact: true }).waitFor();
    assert.equal(await dialog.getByLabel("App Token", { exact: true }).inputValue(), "");
    assert.match(await dialog.locator(".cc-field").last().innerText(), /已配置 · 文件/);
    assert.equal(await dialog.getByRole("button", { name: "测试连接", exact: true }).isEnabled(), true);

    const leak = await page.evaluate((value) => [document.body.innerText, location.href,
      JSON.stringify(localStorage), JSON.stringify(sessionStorage)].some((text) => text.includes(value)), token);
    assert.equal(leak, false);
    await page.route("**/api/cookiecloud/test", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({
      ok: true, count: 2, hosts: [".youtube.com"], token: { name: "assetizer", allowed_domains: ["youtube.com"] },
    }) }));
    await dialog.getByRole("button", { name: "测试连接", exact: true }).click();
    await dialog.getByText("令牌 assetizer · 域名白名单 youtube.com", { exact: true }).waitFor();
    const originField = dialog.locator(".cc-field").first();
    await originField.getByRole("button", { name: "清除", exact: true }).click();
    assert.equal((await context.request.get(`${url}/api/settings`)).ok(), true);
    await originField.getByRole("button", { name: "确认清除", exact: true }).click();
    await dialog.getByText("已清除。", { exact: true }).waitFor();
    assert.equal(await dialog.getByLabel("Origin", { exact: true }).inputValue(), "");
    assert.equal(await dialog.getByRole("button", { name: "保存第 2、3 步", exact: true }).isEnabled(), false);
    const saved = await (await context.request.get(`${url}/api/settings`)).json();
    assert.equal(saved.youtube.cookiecloud.origin, null);
    assert.equal(saved.youtube.cookiecloud.fields.app_token.configured, true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    if (process.env.SMOKE_ARTIFACTS) {
      await mkdir(process.env.SMOKE_ARTIFACTS, { recursive: true });
      await page.screenshot({ path: resolve(process.env.SMOKE_ARTIFACTS, `cookiecloud-${process.env.BROWSER || "chromium"}-${width}.png`) });
    }
    assert.deepEqual(errors, []);
    await context.close();
    console.log(`CookieCloud ${process.env.BROWSER || "chromium"} ${width}px: save, refresh failure, write-only fields, test, confirmed clear passed`);
  }
} finally {
  await browser?.close();
  if (server.exitCode === null) { const stopped = once(server, "exit"); server.kill("SIGTERM"); await stopped; }
  await rm(home, { recursive: true, force: true });
}
