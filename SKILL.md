---
name: dianping-reviews
description: Locate 大众点评 (Dianping) review APIs behind the login wall and harvest/analyse every review of a shop. Use when asked to scrape, export, count, chart or search 大众点评/点评 reviews (评价), when a shop page shows only a handful of reviews and pushes you to the app, when you need the endpoint/pagination parameters of a Dianping review feed, or for rating distributions, keyword and named-entity (月嫂/店员) analysis of that data.
allowed-tools: Read Write Edit Bash
compatibility: Node 18+ with puppeteer-core (npm install) for the browser steps; Python 3.9+ for the data steps (matplotlib optional, for charts only). A logged-in Dianping session is required — the public web only exposes the first ~5 reviews.
metadata: {"version": "1.0", "skill-author": "WICKII"}
---

# Dianping review harvesting

Dianping does not expose a useable public review feed. What actually works:

1. **The web is a teaser.** `www.dianping.com/shop/<id>` renders ~3 reviews;
   `m.dianping.com/shop/<id>/review_all` renders 5 and ends with
   「去APP查看全部 N 条评价」. `www.../review_all` 301s to the app-download page.
   Everything below the first page is gated behind a logged-in session.
2. **The real feed lives on an internal H5 page.** The shop page fires
   `mapi.dianping.com/mapi/review/outsideshopreviewlist.bin`; its JSON contains
   `reviewListSchema` / `bottomReviewListSchema` — the URL of the full review list
   plus a `shopuuidencrypt` token. **The token is session-bound, so never cache
   it: fetch it fresh on every run.**
3. **Referer is mandatory.** Opening that H5 URL directly, or fetching the API
   with cookies but no browser signature, returns **403** and the page renders
   「暂无评价」. Navigating with the shop page as `Referer` works.
4. **Pagination is a cursor, not a page number.** The list scrolls in pages of 14
   via `mapi.dianping.com/mapi/review/outsidesiftedreviewlist.bin?...&start=0,14,28,…`.
   Requests are signed (`mtgsig` header) by the page's own JS, so drive a real
   browser rather than replaying HTTP.
5. **Reviews are free text.** Staff are named inline ("月嫂王丽娜阿姨", "护士长文文"),
   so anything entity-shaped needs mining + human curation, not a schema.

## Workflow

### 1. Discover the endpoint (do this first, and again whenever something breaks)

```bash
cd <skill dir> && npm install          # once
node scripts/probe.mjs --url "https://www.dianping.com/shop/<shopUuid>"
```

This launches a browser, loads the page, records every XHR/fetch with status,
body size, shape summary, cursor-like params, **and any URL embedded in a
response** (the "next hop"). It writes `probe-out/endpoints.md` + `.json`.

Read the report and find the review endpoint. How to read it:

- the top-ranked line is usually the review feed (score ≥ 5 when "review" is in the path);
- `next hop:` entries point at the full-list H5 URL — that is what you feed the harvester;
- `cursor params:` shows which query keys move the window (`start`, `pageNo`, `offset`, …);
- `shape:` tells you whether the payload already contains review objects or only a schema.

If the endpoint is not obviously review-shaped, add `--all` to stop filtering by
host, or `--url <the H5 you suspect> --scroll 2` to watch the pagination call fire.
The method generalises: intercept → read shape → follow embedded URLs → vary the
cursor param.

### 2. Harvest everything

```bash
node scripts/harvest.mjs --shop <shopUuid|numericId|url> --out data
# first run opens a window: scan the QR code with the Dianping app (login persists
# in --profile, default ~/.dianping-harvest/profile). Screenshot: data/login-page.png
node scripts/harvest.mjs --shop <id> --tab 差评 --out data-bad    # a filter tab
```

Outputs `data/reviews.json` (`{meta, reviews[]}`) and `data/reviews.csv`.
Records carry `author, date, rating, scores{环境,护理,月子餐}, spend, text, imageCount`.
It scrolls the list container until the card count stops growing (`--scroll-idle`),
so a 500-review shop takes ~1–2 minutes.

Useful flags: `--cdp http://127.0.0.1:9222` to attach to an already-running
Chromium (e.g. your own logged-in Chrome) instead of launching one; `--headless`
after the profile is logged in; `--max-reviews N` to sample.

### 3. Process

```bash
python3 scripts/reviews.py stats   --reviews data/reviews.json
python3 scripts/reviews.py search  --reviews data/reviews.json --pattern "刘艳|周艳" --context 40
python3 scripts/reviews.py entities --reviews data/reviews.json --min-count 3
python3 scripts/reviews.py chart   --reviews data/reviews.json --roster assets/roster.example.json --out .
python3 scripts/reviews.py export  --reviews data/reviews.json --format csv --out reviews.csv
python3 scripts/reviews.py merge   --reviews data/*.json --out data/merged.json
```

All subcommands share the same filters — slice first, analyse second:
`--rating 一般 --rating 不错`, `--polarity 中评/差评`, `--grep 退款`, `--since 2025-01-01`.
Dates like `发布于9月24日` carry no year; pass `--year 2025` to place them.

- `stats` — counts, rating/polarity buckets, year histogram, score averages,
  spend min/median/max, text length, top terms (jieba if installed, else maximal
  n-grams).
- `search` — every regex hit with author, rating and surrounding context; `--json`
  for machine consumption. This is the workhorse for "does anyone mention X".
- `entities` — mines frequent 2–3 char tokens adjacent to role words
  (`阿姨/月嫂/护士/护士长/老师/销售/客服/店长/…`, override with `--role-words`),
  and, given a roster JSON, scores each name against review polarity **and**
  local sentiment with a negation guard. Mined output is a candidate list:
  curate it before trusting (2-char nicknames collide with ordinary words).
- `chart` — overview PNG: rating distribution, timeline, per-entity sentiment,
  detail table.

## Gotchas observed in the wild

| Symptom | Cause | Fix |
|---|---|---|
| Page renders 「暂无评价」, API returns 403 | H5 opened without the shop-page Referer, or the token went stale | let `harvest.mjs` navigate for you, or pass `referer` explicitly; never reuse a token |
| Login page instead of the shop | session expired | re-run headed, scan the QR (first run only) |
| Only 3–5 reviews | you are reading the shop page, not the review list | use the `next hop` URL from the probe |
| 差评 tab shows 「暂无评价」 | genuinely zero platform-rated bad reviews | remember criticism is often buried in otherwise-positive reviews — search the prose, e.g. `--pattern "很差\|不专业"` |
| Very old reviews about a different brand | Dianping merges a shop's historical listings | check `date` and brand names before aggregating |
| Fewer cards than the declared count | scroll stalled / tab filter applied | re-run; compare `meta.cardsRendered` with `declaredTotal` |

## Ethics and limits

Read-only, login-gated, personal-scale use. Keep the scroll pacing (the scripts
already wait between pages), do not republish user-generated review text as a
dataset, and do not commit harvested data to public repositories. Reviewers are
private individuals — aggregate before publishing; a named person's negative
review is a claim by one customer, not a fact about them.
