// Browser regression checks use a bridge fixture; no live Yunhu API is contacted.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

async function run() {
  const root = path.resolve(__dirname, "../../pages/manager");
  const output = path.resolve(__dirname, "../../.dev/ui");
  fs.mkdirSync(output, { recursive: true });
  const server = http.createServer((req, res) => {
    const pathname = new URL(req.url, "http://localhost").pathname;
    const file = {
      "/": "index.html",
      "/app.js": "app.js",
      "/style.css": "style.css",
      "/assets/logo.png": "assets/logo.png",
    }[pathname];
    if (!file) {
      res.writeHead(404);
      return res.end();
    }
    res.setHeader(
      "content-type",
      {
        ".html": "text/html",
        ".js": "application/javascript",
        ".css": "text/css",
        ".png": "image/png",
      }[path.extname(file)],
    );
    res.end(fs.readFileSync(path.join(root, file)));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({
      headless: true,
      ...(process.env.PLAYWRIGHT_EXECUTABLE_PATH
        ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH }
        : {}),
    });
    const context = await browser.newContext({
      viewport: { width: 1280, height: 940 },
      locale: "zh-CN",
    });
    await context.addInitScript(() => {
      const now = Date.now() / 1000;
      const makeFile = (number, name, pinned = 0) => ({
        file_id: String(number).padStart(32, "0"),
        file_name: name,
        chat_type: number === 3 ? "user" : "group",
        chat_id: number === 3 ? "8297819" : "8297819",
        sender_id: "8297819",
        message_id: `m${number}`,
        size: [4096, 18240, 1920120][number - 1] || 1200,
        created_at: now - number * 3600,
        expires_at: now + 6 * 86400,
        pinned,
      });
      const files = {
        "yunhu-main": [
          makeFile(1, "项目说明.md", 1),
          makeFile(2, "adapter.py"),
          makeFile(3, "项目资料.zip"),
        ],
        "yunhu-second": [makeFile(4, "other.txt")],
      };
      const settings = {
        instance_id: "yunhu-main",
        bot_id: "11201781",
        file_retention_days: 7,
        file_store_max_mb: 512,
        file_store_max_count: 1000,
        media_ttl: 600,
        reply_in_thread: false,
        storage_path:
          "/AstrBot/data/plugin_data/astrbot_plugin_yunhuAdapter/files/main",
        mode: "websocket",
      };
      window.__calls = [];
      window.AstrBotPluginPage = {
        async ready() {
          return { isDark: false, locale: "zh-CN" };
        },
        onContext(handler) {
          window.__theme = handler;
        },
        async apiGet(endpoint, params = {}) {
          window.__calls.push({ method: "GET", endpoint, params });
          if (endpoint === "instances")
            return {
              version: "2.5.0",
              instances: [
                {
                  id: "yunhu-main",
                  bot_id: "11201781",
                  connected: true,
                  mode: "websocket",
                },
                {
                  id: "yunhu-second",
                  bot_id: "222222",
                  connected: false,
                  mode: "webhook",
                },
              ],
            };
          if (endpoint === "settings")
            return { ...settings, instance_id: params.instance_id };
          if (endpoint.endsWith("/preview")) {
            if (endpoint.includes(String(3).padStart(32, "0")))
              throw new Error("压缩包不支持文本预览，请下载后查看");
            return {
              file_name: "项目说明.md",
              text: "# 项目说明\n\n<script>window.__unsafe = true</script>\n\n这是用户上传的文件，作为纯文本显示。",
              size: 4096,
              truncated: false,
            };
          }
          if (endpoint === "files") {
            let selected = files[params.instance_id];
            if (params.search)
              selected = selected.filter((f) =>
                f.file_name.includes(params.search),
              );
            if (params.status !== "all")
              selected = selected.filter(
                (f) => Boolean(f.pinned) === (params.status === "pinned"),
              );
            if (params.chat_type)
              selected = selected.filter(
                (f) =>
                  f.chat_type === params.chat_type &&
                  f.chat_id === params.chat_id,
              );
            const all = files[params.instance_id];
            const conversations = [
              ...new Set(all.map((f) => f.chat_type + ":" + f.chat_id)),
            ].map((key) => {
              const [chat_type, chat_id] = key.split(":");
              return {
                chat_type,
                chat_id,
                file_count: all.filter((f) => f.chat_type === chat_type).length,
              };
            });
            return {
              items: selected.slice(
                params.offset,
                params.offset + params.limit,
              ),
              total: selected.length,
              conversations,
              usage: {
                used_bytes: all.reduce((sum, f) => sum + f.size, 0),
                file_count: all.length,
                max_bytes: settings.file_store_max_mb * 1024 * 1024,
                max_files: settings.file_store_max_count,
                pinned_count: all.filter((f) => f.pinned).length,
                pinned_bytes: all
                  .filter((f) => f.pinned)
                  .reduce((sum, f) => sum + f.size, 0),
                retention_days: settings.file_retention_days,
              },
            };
          }
          throw new Error("Unexpected endpoint: " + endpoint);
        },
        async apiPost(endpoint, body) {
          window.__calls.push({ method: "POST", endpoint, body });
          if (endpoint === "settings/save") {
            Object.assign(settings, body.settings);
            return { saved: true, settings };
          }
          if (endpoint === "cleanup") return { message: "已清理" };
          if (endpoint === "operations/run")
            return {
              ok: true,
              code: 1,
              message: "操作成功",
              data: { action: body.operation },
            };
          if (endpoint.endsWith("/manage")) {
            const id = endpoint.split("/")[1];
            if (body.action === "delete")
              files[body.instance_id] = files[body.instance_id].filter(
                (f) => f.file_id !== id,
              );
            else
              files[body.instance_id].find((f) => f.file_id === id).pinned =
                body.action === "pin" ? 1 : 0;
            return { ok: true };
          }
          throw new Error("Unexpected endpoint: " + endpoint);
        },
        async download(endpoint, params, filename) {
          window.__calls.push({
            method: "DOWNLOAD",
            endpoint,
            params,
            filename,
          });
          return { filename };
        },
      };
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.locator("#files-body tr").first().waitFor();
    assert.equal(await page.locator("#files-body tr").count(), 3);
    const logo = page.getByRole("img", { name: "云湖适配器 logo" });
    assert.equal(
      await logo.evaluate((image) => image.complete && image.naturalWidth > 0),
      true,
    );
    assert.deepEqual(
      fs.readFileSync(path.join(root, "assets/logo.png")),
      fs.readFileSync(path.resolve(root, "../../logo.png")),
    );
    await page.screenshot({
      path: path.join(output, "files-light.png"),
      fullPage: true,
    });

    await page
      .getByRole("button", { name: "预览", exact: true })
      .first()
      .click();
    await page
      .locator("#preview-content")
      .filter({ hasText: "<script>" })
      .waitFor();
    assert.equal(await page.evaluate(() => window.__unsafe), undefined);
    await page.getByRole("button", { name: "下载原文件", exact: true }).click();
    assert.equal(
      (
        await page.evaluate(() =>
          window.__calls.filter((c) => c.method === "DOWNLOAD"),
        )
      ).length,
      1,
    );
    await page.getByLabel("关闭文件预览").click();

    await page.getByLabel("搜索文件").fill("adapter.py");
    await page.waitForFunction(
      () => document.querySelectorAll("#files-body tr").length === 1,
    );
    await page.getByRole("button", { name: "保留", exact: true }).click();
    await page.locator("#confirm-submit").click();
    await page.waitForFunction(
      () =>
        document.querySelector("#files-body .badge").textContent === "长期保留",
    );
    await page.getByLabel("搜索文件").fill("");
    await page.waitForFunction(
      () => document.querySelectorAll("#files-body tr").length === 3,
    );

    await page
      .getByRole("tab", { name: "保存与消息设置", exact: true })
      .click();
    await page.locator('[name="file_retention_days"]').fill("30");
    await page.getByRole("button", { name: "保存并应用", exact: true }).click();
    await page
      .locator("#settings-state")
      .filter({ hasText: "已保存并应用" })
      .waitFor();
    await page.screenshot({
      path: path.join(output, "settings-light.png"),
      fullPage: true,
    });

    await page.getByRole("tab", { name: "功能操作", exact: true }).click();
    await page.locator("#operation-name").selectOption("mute");
    await page.locator("#operation-chat-id").fill("8297819");
    await page.locator('[name="user_id"]').fill("3141766");
    await page.getByRole("button", { name: "执行操作", exact: true }).click();
    await page
      .locator("#confirm-detail")
      .filter({ hasText: "3141766" })
      .waitFor();
    await page.locator("#confirm-submit").click();
    await page
      .locator("#result-status")
      .filter({ hasText: "操作成功" })
      .waitFor();
    let calls = await page.evaluate(() =>
      window.__calls.filter((c) => c.endpoint === "operations/run"),
    );
    assert.equal(calls[0].body.chat_id, "8297819");
    assert.equal(calls[0].body.arguments.duration, 600);
    await page.locator("#operation-name").selectOption("global_board");
    assert.equal(await page.locator("#operation-chat-id").isVisible(), false);
    await page.locator('[name="text"]').fill("测试公告");
    await page.getByRole("button", { name: "执行操作", exact: true }).click();
    await page.locator("#confirm-submit").click();
    await page.waitForFunction(
      () =>
        window.__calls.filter((c) => c.endpoint === "operations/run").length ===
        2,
    );
    await page.screenshot({
      path: path.join(output, "operations-light.png"),
      fullPage: true,
    });

    await page.getByRole("tab", { name: "文件库", exact: true }).click();
    await page.getByLabel("删除 项目资料.zip", { exact: true }).click();
    await page.locator("#confirm-cancel").click();
    assert.equal(await page.locator("#files-body tr").count(), 3);
    await page.getByLabel("删除 项目资料.zip", { exact: true }).click();
    await page.locator("#confirm-submit").click();
    await page.waitForFunction(
      () => document.querySelectorAll("#files-body tr").length === 2,
    );

    await page.evaluate(() => window.__theme({ isDark: true }));
    await page.screenshot({
      path: path.join(output, "files-dark.png"),
      fullPage: true,
    });
    await page.evaluate(() => window.__theme({ isDark: false }));
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
      true,
    );
    await page.screenshot({
      path: path.join(output, "files-mobile.png"),
      fullPage: true,
    });
    await page
      .getByLabel("平台实例", { exact: true })
      .selectOption("yunhu-second");
    await page
      .locator(".file-title")
      .filter({ hasText: "other.txt" })
      .waitFor();
    assert.equal(await page.locator("#files-body tr").count(), 1);
    assert.deepEqual(errors, []);
    console.log(
      "PASS: real-browser rendering, safe previews, downloads, filtering, pin/delete confirmation, live settings, group/global operations, themes, mobile layout and instance switching.",
    );
    console.log("Screenshots:", output);
    await context.close();
  } finally {
    if (browser) await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
}
run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
