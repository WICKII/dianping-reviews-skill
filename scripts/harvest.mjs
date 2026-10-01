#!/usr/bin/env node
/**
 * Harvest all reviews for a Dianping (大众点评) shop.
 *
 * Why this exists: Dianping web shows only the first ~5 reviews and pushes you
 * to the app. The full list is served by an internal H5 page reachable only
 * when the browser carries a logged-in Dianping session AND the request comes
 * from the shop page (risk control 403s otherwise). This script reproduces the
 * exact navigation chain a real browser makes and drains the infinite scroll.
 *
 * Flow (all of it reverse-engineered against the live site):
 *   1. open  https://www.dianping.com/shop/<id>          (login required)
 *   2. capture the JSON response of mapi .../outsideshopreviewlist.bin
 *      -> it contains `reviewListSchema`, the full-list H5 URL with a fresh
 *         `shopuuidencrypt` token (tokens are session bound, so we never cache it)
 *   3. open that URL **with the shop page as Referer** (mandatory, else the
 *      list request is 403 and the page renders 暂无评价)
 *   4. scroll `.review-list-scroll` until the card count stops growing
 *      (the H5 loads 14 more reviews per scroll via ?start=N)
 *   5. extract structured records and write JSON + CSV
 *
 * Usage:
 *   node scripts/harvest.mjs --shop l7WauBtSg0GxkYvz
 *   node scripts/harvest.mjs --shop https://www.dianping.com/shop/1173681147 --tab 差评
 *
 * Reads: nothing. Writes: <out>/reviews.json, <out>/reviews.csv, <out>/harvest.log.json
 */
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { parseArgs } from "node:util";
import puppeteer from "puppeteer-core";

const argv = process.argv.slice(2);
const { values: opt } = parseArgs({
  args: argv,
  options: {
    shop: { type: "string" },
    out: { type: "string", default: "data" },
    profile: { type: "string", default: path.join(os.homedir(), ".dianping-harvest", "profile") },
    executable: { type: "string" },
    cdp: { type: "string" },
    headless: { type: "boolean", default: false },
    tab: { type: "string", default: "全部" },
    "max-reviews": { type: "string" },
    "login-timeout": { type: "string", default: "300" },
    "scroll-idle": { type: "string", default: "3" },
    help: { type: "boolean", default: false },
  },
  allowPositionals: true,
});

if (opt.help || !opt.shop) {
  console.log(`Usage: node scripts/harvest.mjs --shop <url|shopUuid|shopId> [options]

Options:
  --shop           shop URL, 24-char shopUuid, or numeric shopId  (required)
  --out            output dir                         (default: data)
  --profile        persistent browser profile         (default: ~/.dianping-harvest/profile)
  --cdp            attach to a running Chromium, e.g. http://127.0.0.1:9222
  --executable     Chrome/Chromium binary to launch
  --headless       run without a window (login still requires one first run)
  --tab            全部|最新|好评|差评|中评|带图                          (default: 全部)
  --max-reviews    stop after N reviews (debugging)
  --login-timeout  seconds to wait for the QR scan        (default: 300)
  --scroll-idle    stop after N scrolls with no new cards (default: 3)
`);
  process.exit(opt.shop ? 0 : 1);
}

const log = [];
const LOGIN_HOSTS = ["account.dianping.com", "mlogin.dianping.com", "passport.dianping.com"];

function shopUrlOf(shop) {
  if (/^https?:\/\//i.test(shop)) return shop;
  if (/^\d+$/.test(shop)) return `https://www.dianping.com/shop/${shop}`;
  return `https://www.dianping.com/shop/${shop}`;
}

function findChrome() {
  if (opt.executable) return opt.executable;
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  const roots = [
    path.join(os.homedir(), ".omp", "puppeteer", "chrome"),
    path.join(os.homedir(), "Library", "Caches", "ms-playwright"),
  ];
  for (const root of roots) {
    if (!fs.existsSync(root)) continue;
    const hits = fs
      .readdirSync(root)
      .flatMap((d) => {
        const base = path.join(root, d);
        const cands = [
          path.join(base, "chrome-mac-arm64", "Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing"),
          path.join(base, "chrome-mac-x64", "Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing"),
          path.join(base, "chrome-linux64", "chrome"),
        ];
        const inner = fs.existsSync(base) && fs.statSync(base).isDirectory()
          ? fs.readdirSync(base).filter((x) => /^chromium-\d+|^chrome-\d+/.test(x)).map((x) => {
              const p = path.join(base, x);
              return [
                path.join(p, "chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium"),
                path.join(p, "chrome-linux", "chrome"),
                path.join(p, "chrome-mac", "chrome-mac-arm64", "Chromium.app", "Contents", "MacOS", "Chromium"),
              ];
            }).flat()
          : [];
        return [...cands, ...inner];
      })
      .filter((p) => fs.existsSync(p));
    if (hits.length) return hits.sort().pop();
  }
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (fs.existsSync(mac)) return mac;
  throw new Error("No Chrome found. Pass --executable <path> or set CHROME_PATH.");
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function connect() {
  if (opt.cdp) {
    const browser = await puppeteer.connect({ browserURL: opt.cdp, defaultViewport: null });
    log.push({ step: "connect", mode: "cdp", url: opt.cdp });
    return { browser, own: false };
  }
  const executablePath = findChrome();
  fs.mkdirSync(opt.profile, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath,
    headless: opt.headless,
    userDataDir: opt.profile,
    defaultViewport: { width: 1440, height: 900 },
    args: ["--no-first-run", "--no-default-browser-check", "--disable-blink-features=AutomationControlled"],
  });
  log.push({ step: "connect", mode: "launch", executablePath, profile: opt.profile, headless: opt.headless });
  return { browser, own: true };
}

async function ensureLoggedIn(page, shopUrl) {
  const isLogin = () => LOGIN_HOSTS.some((h) => page.url().includes(h));
  await page.goto(shopUrl, { waitUntil: "domcontentloaded", timeout: 60000 }).catch(() => {});
  await sleep(2500);
  if (!isLogin()) {
    log.push({ step: "login", state: "already-logged-in" });
    return;
  }
  const shot = path.join(opt.out, "login-page.png");
  fs.mkdirSync(opt.out, { recursive: true });
  await page.screenshot({ path: shot }).catch(() => {});
  console.error(`\n[login required] Scan the QR code with the Dianping app.`);
  console.error(`  - a browser window is open on the login page`);
  console.error(`  - screenshot saved to: ${shot}\n`);
  const deadline = Date.now() + Number(opt["login-timeout"]) * 1000;
  while (Date.now() < deadline) {
    await sleep(3000);
    if (!isLogin()) {
      log.push({ step: "login", state: "ok" });
      await sleep(2000);
      return;
    }
  }
  throw new Error(`login timed out after ${opt["login-timeout"]}s`);
}

/** The shop page fires the review API; its response carries the full-list URL. */
async function grabReviewListUrl(page, shopUrl) {
  let captured = null;
  const onResponse = async (res) => {
    if (!/outsideshopreviewlist\.bin/.test(res.url())) return;
    if (res.request().method() !== "GET") return;
    try {
      const body = await res.text();
      if (!body.startsWith("{")) return;
      const data = JSON.parse(body);
      const url = data.bottomReviewListSchema || data.reviewListSchema;
      if (url) {
        const fromUrl = Number((url.match(/[?&]count=(\d+)/) || [])[1]) || null;
        captured = { url, total: fromUrl ?? data.reviewCount ?? null, raw: data };
      }
    } catch { /* ignore */ }
  };
  page.on("response", onResponse);
  try {
    for (let attempt = 1; attempt <= 2 && !captured; attempt++) {
      await page.goto(shopUrl, { waitUntil: "networkidle2", timeout: 60000 }).catch(() => {});
      for (let i = 0; i < 20 && !captured; i++) await sleep(500);
      if (!captured) await page.reload({ waitUntil: "networkidle2" }).catch(() => {});
      for (let i = 0; i < 20 && !captured; i++) await sleep(500);
    }
  } finally {
    page.off("response", onResponse);
  }
  if (!captured) throw new Error("could not capture reviewListSchema from the shop page (review API never responded)");
  log.push({ step: "reviewListSchema", url: captured.url, total: captured.total });
  return captured;
}

async function applyTab(page, tab) {
  if (!tab || tab === "全部") return;
  const ok = await page.evaluate((t) => {
    const el = [...document.querySelectorAll(".label, .tag, [class*=tab]")]
      .find((e) => (e.innerText || "").trim().replace(/\s*\d+$/, "") === t);
    if (!el) return false;
    el.click();
    return true;
  }, tab);
  log.push({ step: "tab", tab, clicked: ok });
  if (!ok) console.error(`[warn] tab "${tab}" not found; keeping 全部`);
  await sleep(4000);
}

async function drain(page, expected) {
  const count = () => page.evaluate(() => document.querySelectorAll(".review-card").length);
  const idleLimit = Number(opt["scroll-idle"]);
  const max = opt["max-reviews"] ? Number(opt["max-reviews"]) : Infinity;
  let prev = await count();
  let idle = 0;
  let rounds = 0;
  while (idle < idleLimit && rounds++ < 200) {
    await page.evaluate(() => {
      const el = document.querySelector(".review-list-scroll") || document.scrollingElement;
      if (el) el.scrollTop = el.scrollHeight;
    });
    await sleep(1800);
    const n = await count();
    if (n >= max || (expected && n >= expected)) break;
    if (n === prev) idle++;
    else idle = 0;
    prev = n;
  }
  log.push({ step: "drain", rounds, cards: prev, expected });
  return prev;
}

async function extract(page) {
  return page.evaluate(() => {
    const txt = (root, sel) => {
      const el = root.querySelector(sel);
      return el ? el.innerText.trim() : "";
    };
    return [...document.querySelectorAll(".review-card")].map((card, index) => {
      const scores = {};
      card.querySelectorAll(".score-item").forEach((s) => {
        const m = s.innerText.trim().match(/^(.+?)[:：]\s*([\d.]+)$/);
        if (m) scores[m[1]] = Number(m[2]);
      });
      const spendMatch = card.innerText.match(/¥\s*(\d+)/);
      const body = [...card.querySelectorAll(".review-content-item-text")]
        .map((e) => e.innerText.trim())
        .filter(Boolean)
        .join("\n");
      return {
        index,
        author: txt(card, ".user-name"),
        date: txt(card, ".time-text"),
        rating: txt(card, ".emoji-text"),
        scores,
        spend: spendMatch ? Number(spendMatch[1]) : null,
        text: body,
        imageCount: card.querySelectorAll(".review-images-container img").length,
      };
    });
  });
}

function toCSV(rows) {
  const esc = (v) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const header = ["index", "author", "date", "rating", "环境", "护理", "月子餐", "spend", "imageCount", "text"];
  const lines = [header.join(",")];
  for (const r of rows) {
    lines.push([r.index, r.author, r.date, r.rating, r.scores["环境"] ?? "", r.scores["护理"] ?? "",
      r.scores["月子餐"] ?? "", r.spend ?? "", r.imageCount, r.text.replace(/\n/g, " ")].map(esc).join(","));
  }
  return lines.join("\n") + "\n";
}

async function main() {
  const shopUrl = shopUrlOf(opt.shop);
  fs.mkdirSync(opt.out, { recursive: true });
  const { browser, own } = await connect();
  const page = await browser.newPage();
  try {
    await ensureLoggedIn(page, shopUrl);
    const schema = await grabReviewListUrl(page, shopUrl);

    await page.goto(schema.url, { referer: shopUrl, waitUntil: "networkidle2", timeout: 60000 });
    await sleep(5000);

    let cards = await page.evaluate(() => document.querySelectorAll(".review-card").length);
    if (cards === 0) {
      const text = await page.evaluate(() => document.body.innerText.slice(0, 200));
      log.push({ step: "retry", reason: "zero cards", text });
      await page.goto(shopUrl, { waitUntil: "networkidle2", timeout: 60000 }).catch(() => {});
      await sleep(3000);
      const again = await grabReviewListUrl(page, shopUrl);
      await page.goto(again.url, { referer: shopUrl, waitUntil: "networkidle2", timeout: 60000 });
      await sleep(5000);
      cards = await page.evaluate(() => document.querySelectorAll(".review-card").length);
      if (cards === 0) {
        throw new Error(
          "review list rendered 0 cards. Most likely causes: the Referer was not the shop page, " +
          "the shopuuidencrypt token went stale, or the session lost login. Re-run with --headless off."
        );
      }
    }

    await applyTab(page, opt.tab);
    const total = await drain(page, schema.total);
    const rows = await extract(page);

    const jsonPath = path.join(opt.out, "reviews.json");
    const csvPath = path.join(opt.out, "reviews.csv");
    const meta = {
      shop: opt.shop, shopUrl, source: "dianping",
      harvestedAt: new Date().toISOString(),
      tab: opt.tab, reviewListUrl: schema.url, declaredTotal: schema.total,
      cardsRendered: total, records: rows.length,
    };
    fs.writeFileSync(jsonPath, JSON.stringify({ meta, reviews: rows }, null, 1));
    fs.writeFileSync(csvPath, toCSV(rows));
    fs.writeFileSync(path.join(opt.out, "harvest.log.json"), JSON.stringify(log, null, 1));

    const dist = rows.reduce((a, r) => ((a[r.rating] = (a[r.rating] || 0) + 1), a), {});
    console.log(`harvested ${rows.length} reviews (shop declares ${schema.total ?? "?"})`);
    console.log("rating distribution:", dist);
    console.log(`wrote ${jsonPath}`);
    console.log(`wrote ${csvPath}`);
  } finally {
    await page.close().catch(() => {});
    if (own) await browser.close().catch(() => {});
  }
}

main().catch((err) => {
  console.error(`\n[harvest failed] ${err.message}\n`);
  for (const l of log) console.error("  " + JSON.stringify(l));
  process.exit(1);
});
