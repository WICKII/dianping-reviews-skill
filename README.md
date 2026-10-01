# dianping-reviews-skill

An agent skill for pulling reviews out of 大众点评 (Dianping) — where the real
review feed hides behind a login wall on an internal H5 page — and for the common
data processing that follows.

Two things it is actually good at:

1. **Finding the endpoint.** `probe.mjs` loads any Dianping page in a real
   browser, records every XHR/fetch with its params, status, response shape and
   any URL embedded in the payload, then ranks the review-shaped ones. That is how
   the review feed, its short-lived token and its `start=` cursor were found — and
   how to find them again after Dianping renames something.
2. **Getting all of it, then doing something with it.** `harvest.mjs` walks the
   full sealed pagination to the last review; `reviews.py` gives you stats,
   regex search, entity mining/roster scoring, merge/dedupe, export and charts.

## Why it needs a browser

The review feed request is signed (`mtgsig`) by Dianping's own JS and the list URL
contains a per-session `shopuuidencrypt` token. Cookies alone return 403. The
scripts therefore drive Chromium (via `puppeteer-core`, reusing an existing Chrome
install) against a persistent profile that you log into once by scanning a QR code.

## Install

```bash
git clone <this repo> && cd dianping-reviews-skill
npm install
# optional, better Chinese word segmentation for `stats --top-terms`
python3 -m pip install jieba
python3 -m pip install matplotlib      # only for `chart`
```

As an agent skill, symlink it into your skills directory:

```bash
ln -s "$PWD" ~/.agents/skills/dianping-reviews
```

## Use

```bash
# 1. discover what the shop page actually calls
node scripts/probe.mjs --url "https://www.dianping.com/shop/<shopUuid>"

# 2. drain the review list (first run: scan the QR with the Dianping app)
node scripts/harvest.mjs --shop <shopUuid> --out data

# 3. slice and analyse
python3 scripts/reviews.py stats    --reviews data/reviews.json
python3 scripts/reviews.py search   --reviews data/reviews.json --pattern "退款|差评" --json
python3 scripts/reviews.py entities --reviews data/reviews.json --min-count 3
python3 scripts/reviews.py chart    --reviews data/reviews.json --out .
```

See `SKILL.md` for the full workflow, the endpoint contract and the failure-mode
table; `references/dianping-endpoints.md` for the reverse-engineered details.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Layout

```
SKILL.md                        agent-facing playbook
references/dianping-endpoints.md  observed API/DOM contract + rediscovery recipe
scripts/probe.mjs               endpoint discovery (read-only)
scripts/harvest.mjs             full review harvest -> JSON/CSV
scripts/reviews.py              stats | search | entities | merge | export | chart
assets/roster.example.json      roster format for entity scoring
tests/                          fixtures + behaviour tests
```

## Scope and ethics

Read-only, login-gated, personal-scale. Keep the built-in pacing, don't
republish review text as a dataset, and don't commit harvested data (`.gitignore`
covers `data/`). Reviewers are private individuals: aggregate before publishing,
and treat a named complaint as one customer's claim rather than a fact.
