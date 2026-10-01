#!/usr/bin/env node
/**
 * Discover the live review API of any Dianping page.
 *
 * Purpose: Dianping moves its endpoints around and names them opaquely
 * (`outsideshopreviewlist.bin`, `outsidesiftedreviewlist.bin`, ...). Rather than
 * guessing, point this at a page and it logs every XHR/fetch the page makes,
 * with params, status and a shape summary of each JSON response, then ranks the
 * review-related ones and extracts anything that looks like a next-hop URL or a
 * pagination cursor.
 *
 * Usage:
 *   node scripts/probe.mjs --url https://www.dianping.com/shop/l7WauBtSg0GxkYvz
 *   node scripts/probe.mjs --url https://m.dianping.com/review-list/index.html?... --scroll 3
 *   node scripts/probe.mjs --url <shop> --all          # don't filter to review-ish hosts
 *
 * Reads: nothing. Writes: <out>/endpoints.json, <out>/endpoints.md
 */
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { parseArgs } from "node:util";
import puppeteer from "puppeteer-core";

const { values: opt } = parseArgs({
  args: process.argv.slice(2),
  options: {
    url: { type: "string" },
    out: { type: "string", default: "probe-out" },
    profile: { type: "string", default: path.join(os.homedir(), ".dianping-harvest", "profile") },
    executable: { type: "string" },
    cdp: { type: "string" },
    headless: { type: "boolean", default: false },
    scroll: { type: "string", default: "0" },
    "settle-ms": { type: "string", default: "6000" },
    all: { type: "boolean", default: false },
    "login-timeout": { type: "string", default: "300" },
  },
  allowPositionals: true,
});

if (!opt.url) {
  console.error("Usage: node scripts/probe.mjs --url <dianping url> [--scroll N] [--all]");
  process.exit(1);
}

const NOISE = /\.(png|jpe?g|webp|gif|svg|ico|css|js|woff2?|ttf)(\?|$)|\/lx\.js|hm\.baidu|catfront|dpmobile|logan|beacon|report/i;
const INTERESTING = /review|comment|ugc|feed|shop|poi|detail|search|list/i;
const NEXT_HOP = /review|comment|list|detail/i;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = [];

function findChrome() {
  if (opt.executable) return opt.executable;
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  const root = path.join(os.homedir(), ".omp", "puppeteer", "chrome");
  if (fs.existsSync(root)) {
    for (const d of fs.readdirSync(root)) {
      const p = path.join(root, d, "chrome-mac-arm64", "Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing");
      if (fs.existsSync(p)) return p;
    }
  }
  const mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (fs.existsSync(mac)) return mac;
  throw new Error("no Chrome found; pass --executable");
}

/** Compact structural summary: keys, array lengths, nested keys one level down. */
function shape(v, depth = 0) {
  if (v === null) return "null";
  if (Array.isArray(v)) {
    if (!v.length) return "[]";
    return `[${v.length}x ${shape(v[0], depth + 1)}]`;
  }
  if (typeof v === "object") {
    if (depth >= 2) return "{...}";
    const keys = Object.keys(v).slice(0, 14);
    return `{ ${keys.map((k) => `${k}:${shape(v[k], depth + 1)}`).join(", ")}${Object.keys(v).length > keys.length ? ", …" : ""} }`;
  }
  if (typeof v === "string") return v.length > 40 ? `"${v.slice(0, 40)}…"` : JSON.stringify(v);
  return String(v);
}

function extractUrls(obj, bag = new Set(), depth = 0) {
  if (depth > 4 || obj === null) return bag;
  if (typeof obj === "string") {
    if (/^https?:\/\//.test(obj) && NEXT_HOP.test(obj)) bag.add(obj);
    return bag;
  }
  if (typeof obj === "object") {
    for (const v of Object.values(obj)) extractUrls(v, bag, depth + 1);
  }
  return bag;
}

/** Params whose values look like cursors / pagination controls. */
function cursorParams(url) {
  const out = {};
  try {
    const u = new URL(url);
    for (const [k, v] of u.searchParams) {
      if (/^(start|offset|page|pageNo|pageNum|pagesize|limit|count|cursor|scroll|index|filter|select|tab|type|sort|referid|refertype)/i.test(k)) out[k] = v;
    }
  } catch { /* relative */ }
  return out;
}

async function main() {
  fs.mkdirSync(opt.out, { recursive: true });
  const browser = opt.cdp
    ? await puppeteer.connect({ browserURL: opt.cdp, defaultViewport: null })
    : await puppeteer.launch({
        executablePath: findChrome(),
        headless: opt.headless,
        userDataDir: opt.profile,
        defaultViewport: { width: 1440, height: 900 },
        args: ["--no-first-run", "--no-default-browser-check", "--disable-blink-features=AutomationControlled"],
      });
  const page = await browser.newPage();
  const records = [];

  page.on("response", async (res) => {
    const req = res.request();
    const url = res.url();
    if (NOISE.test(url)) return;
    const type = req.resourceType();
    if (!["xhr", "fetch", "document"].includes(type)) return;
    if (!opt.all && !INTERESTING.test(url)) return;

    const rec = {
      method: req.method(),
      url,
      resourceType: type,
      status: res.status(),
      mime: res.headers()["content-type"] || "",
      requestHeaders: req.headers(),
      postData: req.postData() || null,
      cursorParams: cursorParams(url),
    };
    try {
      const body = await res.text();
      rec.bodyBytes = body.length;
      if (body.startsWith("{") || body.startsWith("[")) {
        const json = JSON.parse(body);
        rec.shape = shape(json);
        rec.nextHops = [...extractUrls(json)];
        if (Array.isArray(json)) rec.items = json.length;
        else if (typeof json === "object") {
          const arrs = Object.entries(json).filter(([, v]) => Array.isArray(v)).map(([k, v]) => `${k}[${v.length}]`);
          if (arrs.length) rec.arrays = arrs;
        }
      }
    } catch { /* streaming or empty body */ }
    records.push(rec);
  });

  await page.goto(opt.url, { waitUntil: "networkidle2", timeout: 90000 }).catch((e) => log.push({ goto: String(e.message) }));
  await sleep(Number(opt["settle-ms"]));

  for (let i = 0; i < Number(opt.scroll); i++) {
    await page.evaluate(() => {
      const el = document.querySelector(".review-list-scroll") || document.scrollingElement;
      if (el) el.scrollTop = el.scrollHeight;
      else window.scrollTo(0, document.body.scrollHeight);
    });
    await sleep(2000);
  }

  const loginHost = /account\.dianping\.com|mlogin\.dianping\.com/.test(page.url());
  if (loginHost) await page.screenshot({ path: path.join(opt.out, "login-page.png") }).catch(() => {});

  const ranked = records
    .map((r) => ({ ...r, score: (/review/i.test(r.url) ? 3 : 0) + (/sifted|shopreview|comment/i.test(r.url) ? 2 : 0) + (r.items ? 1 : 0) + (r.nextHops?.length ? 1 : 0) }))
    .sort((a, b) => b.score - a.score || b.bodyBytes - a.bodyBytes);

  const report = {
    probedUrl: opt.url,
    landedUrl: page.url(),
    loggedIn: !loginHost,
    capturedAt: new Date().toISOString(),
    total: ranked.length,
    endpoints: ranked,
    notes: log,
  };
  fs.writeFileSync(path.join(opt.out, "endpoints.json"), JSON.stringify(report, null, 1));

  const md = [];
  md.push(`# Endpoint probe — ${opt.url}`);
  md.push("");
  md.push(`landed: \`${page.url()}\``);
  md.push(loginHost ? "**login required** — scan the QR in the open window, then re-run." : "session: logged in");
  md.push("");
  md.push("| score | method | endpoint | status | bytes | items | cursor params |");
  md.push("|---|---|---|---|---|---|---|");
  for (const r of ranked.slice(0, 40)) {
    const ep = r.url.replace(/^https?:\/\/[^/]+/, "").slice(0, 110);
    md.push(`| ${r.score} | ${r.method} | \`${ep}\` | ${r.status} | ${r.bodyBytes ?? ""} | ${r.items ?? ""} | \`${JSON.stringify(r.cursorParams)}\` |`);
  }
  for (const r of ranked.filter((r) => r.nextHops?.length || r.shape).slice(0, 10)) {
    md.push("");
    md.push(`### \`${r.url.replace(/^https?:\/\/[^/]+/, "").slice(0, 120)}\``);
    md.push(`- status ${r.status} · ${r.bodyBytes ?? "?"} bytes · method ${r.method}`);
    if (r.postData) md.push(`- post: \`${r.postData.slice(0, 400)}\``);
    if (r.shape) md.push(`- shape: ${r.shape.slice(0, 700)}`);
    for (const h of r.nextHops || []) md.push(`- next hop: \`${h}\``);
  }
  fs.writeFileSync(path.join(opt.out, "endpoints.md"), md.join("\n") + "\n");

  console.log(`captured ${records.length} request(s); top candidates:`);
  for (const r of ranked.slice(0, 8)) {
    console.log(`  [${r.score}] ${r.method} ${r.url.replace(/^https?:\/\/[^/]+/, "").slice(0, 100)}  ${r.status} ${r.bodyBytes ?? ""}b ${r.items ? `items=${r.items}` : ""}`);
  }
  console.log(`\nwrote ${path.join(opt.out, "endpoints.md")} and endpoints.json`);

  await page.close().catch(() => {});
  if (!opt.cdp) await browser.close().catch(() => {});
}

main().catch((e) => {
  console.error(`[probe failed] ${e.message}`);
  process.exit(1);
});
