# Dianping review endpoints — observed contract

Captured against a live 月子中心/门店 shop page (2026-10), logged in, on both the
`www` and `m` hosts.
Names and tokens rotate; treat the *shape* as durable and the *names* as
rediscoverable with `scripts/probe.mjs`.

## 1. Shop page → full-list URL

`GET https://www.dianping.com/shop/<shopUuid>`

The page fires:

```
GET https://mapi.dianping.com/mapi/review/outsideshopreviewlist.bin
      ?mtsiReferrer=https%3A%2F%2Fwww.dianping.com%2Fshop%2F<shopUuid>...
      &isNeedNewReview=1&reqsource=2&shopuuid=<shopUuid>
      &device_system=MACINTOSH&yodaReady=h5&csecplatform=4&csecversion=4.3.0
headers: mtgsig: {"a1":"1.2","a2":<ts>,"a3":"…","a5":"…","a6":"…","a8":"…","a9":"4.3.0,9,109", …}
         appname: dianping-wxapp, channel: H5, minaversion: 11.0.0, appversion: 11.0.0
```

Response (≈70 KB JSON) — keys worth knowing:

```
{
  bottomReviewListSchema: "https://m.dianping.com/review-list/index.html?…&count=549&shopuuidencrypt=<token>",
  reviewListSchema:        "…same…",
  reviewNlpTagList:        [ … ],          // the 月嫂细心耐心(379) style tag cloud
  reviewPreloadData:       { reviewList: [ … 2-3 review objects … ] },
  star, subScoreList, monthReviewCount, reviewAbstractList, …
}
```

- `count` = total review count declared by the shop.
- `shopuuidencrypt` is **session-bound and short-lived** — always refetch.
- `mtgsig` is produced by the page's JS (H5guard). It is not reproducible from a
  plain HTTP client, which is why the tooling drives a browser.

`shopUuid` vs numeric id: modern shops use a 24-char opaque id
(`<shopUuid>`, e.g. a 24-char base62 string); the numeric id (`<numericId>`) still appears in
`data-launch-shop-id` / `dianping://…referid=` attributes and in the legacy
`m.dianping.com/shop/<numericId>` route.

## 2. Full list page → paginated feed

`GET https://m.dianping.com/review-list/index.html?notitlebar=1&refertype=0&referid=<shopUuid>&selecttab=0&merge=1&count=<N>&shopuuidencrypt=<token>`

**Must be opened with `Referer: https://www.dianping.com/shop/<shopUuid>`.**
Without it the feed call 403s and the page renders 「暂无评价」.

DOM contract:

| selector | content |
|---|---|
| `.review-list-scroll` | the inner scroll container — scrolling the window does nothing |
| `.review-card` | one review (14 preloaded, then +14 per scroll) |
| `.user-name` | author |
| `.time-text` | `发布于9月24日丨编辑于…` (year is often omitted) |
| `.emoji-text` | platform rating label: `超预期` / `很棒` / `不错` / `一般` |
| `.score-item` | `环境:5.0` `护理:5.0` `月子餐:5.0` |
| `.review-content-item-text` | body paragraphs (complete — the 全文 button is a CSS clamp, not a data limit) |
| `.review-images-container img` | attached photos |
| `.read-more-text-expand-button` | visual expander; `innerText` already holds the full text |
| `.label` | filter tabs: 全部 / 最新 / 差评 / 中评 / 带图 · 视频 |
| `.review-list-scroll` sentinel | end reached when card count stops growing |

Filter tabs are inert on desktop web (they launch the native app via
`dianping://`), but on the `review-list` H5 they switch the feed.

Pagination request (fired by the page, `start` advances by 14):

```
GET https://mapi.dianping.com/mapi/review/outsidesiftedreviewlist.bin
      ?optimus_code=10&optimus_partner=76&optimus_risk_level=71&reqsource=4
      &filterid=800&merge=1&needfilter=1&queryid=<epoch>_<rand>
      &referid=<shopUuid>&refertype=0&start=14&multifilterid
```

Notes:

- `start=0,14,28,…` — a cursor, not `pageNo`; unknown params are ignored.
- `filterid=800` corresponds to the 全部 tab; the tab click regenerates it.
- A `tab` of 差评 that is empty renders 「暂无评价」 — that is data, not an error.

## 3. Rediscovery recipe

Endpoints get renamed. To find the new one:

1. `node scripts/probe.mjs --url <page> [--all] [--scroll 3]`.
2. In `endpoints.md`, look for endpoints whose path contains `review|comment|ugc`
   and whose JSON `shape:` contains an array of review-like objects.
3. Follow `next hop:` URLs; the full-list URL is the one with `count=` + an
   `encrypt`/`token` param.
4. Identify the cursor param from `cursor params:` (`start`, `pageNo`, `offset`…),
   then vary it and confirm the payload changes and does not repeat items.
5. If a call 403s: check Referer first, then session, then whether it needs a
   browser-generated signature (look for an `mtgsig`/`dfpId` style header).

## 4. Adjacent endpoints seen (not needed for reviews, but useful context)

- `/wxmapi/shop/shopinfo|shopservice|shopmenu|shopquestion|friendslike` — shop modules
- `/an/gear/dpmapp/api/poi/breadcrumb`, `/readLionConfig/config` — page scaffolding
- `/ugc/review/shop/shopreview` — 403 without a mini-program signed context
- `m.dianping.com/ugcdetail/<id>` — a single review/note page (also a next hop in payloads)
