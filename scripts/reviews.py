#!/usr/bin/env python3
"""Common data processing for harvested Dianping reviews.

Subcommands
-----------
stats     counts, rating distribution, date range, score/spend aggregates, top terms
search    regex/keyword search across review text with context (JSON-able)
entities  mine frequently-praised/criticised named entities (staff, dishes, ...)
merge     union several harvest files and de-duplicate
export    reviews -> csv / jsonl / markdown
chart     rating distribution, timeline, top entities, per-entity sentiment

Every subcommand accepts the shared filters so you can slice first, analyse second.

Examples
--------
  python3 scripts/reviews.py stats --reviews data/reviews.json
  python3 scripts/reviews.py search --reviews data/reviews.json --pattern "刘艳|周艳"
  python3 scripts/reviews.py entities --reviews data/reviews.json --role-words 阿姨,护士,店长
  python3 scripts/reviews.py entities --reviews data/reviews.json --roster assets/roster.json --chart
  python3 scripts/reviews.py export --reviews data/reviews.json --format csv --out out.csv
"""
import argparse
import collections
import csv
import hashlib
import json
import os
import re
import statistics
import sys

# ---------------------------------------------------------------- loading

RATING_BUCKETS = {"超预期": "好评", "很棒": "好评(偏中)", "不错": "好评(偏中)"}


def polarity(label):
    return RATING_BUCKETS.get(label, "中评/差评")


def parse_date(s, default_year=None):
    """'发布于2025年3月9日丨编辑于…' -> '2025-03-09' (year may be None)."""
    if not s:
        return None
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{1,2})月(\d{1,2})日", s)
    if m:
        y = f"{default_year:04d}" if default_year else "0000"
        return f"{y}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def load(path, default_year=None):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    meta = data.get("meta", {}) if isinstance(data, dict) else {}
    rows = data["reviews"] if isinstance(data, dict) else data
    out = []
    for r in rows:
        rec = {
            "author": r.get("author", ""),
            "date_raw": r.get("date", ""),
            "date": parse_date(r.get("date", ""), default_year),
            "rating": r.get("rating") or r.get("label") or "",
            "scores": r.get("scores") or r.get("sub") or {},
            "spend": r.get("spend"),
            "text": r.get("text", ""),
            "imageCount": r.get("imageCount", 0),
            "source": meta.get("shop") or os.path.basename(path),
        }
        out.append(rec)
    return out, meta


def dedupe_key(r):
    basis = (r["author"], r["date_raw"], r["text"][:120])
    return hashlib.sha1("|".join(basis).encode("utf-8")).hexdigest()


def apply_filters(rows, args):
    out = rows
    if getattr(args, "rating", None):
        want = set(args.rating)
        out = [r for r in out if r["rating"] in want]
    if getattr(args, "polarity", None):
        want = set(args.polarity)
        out = [r for r in out if polarity(r["rating"]) in want]
    if getattr(args, "grep", None):
        rx = re.compile(args.grep)
        out = [r for r in out if rx.search(r["text"])]
    if getattr(args, "since", None):
        out = [r for r in out if r["date"] and r["date"] >= args.since and not r["date"].startswith("0000")]
    if getattr(args, "until", None):
        out = [r for r in out if r["date"] and r["date"] <= args.until and not r["date"].startswith("0000")]
    return out


# ---------------------------------------------------------------- text

STOPWORDS = set("""
的 了 是 在 和 都 也 很 我 你 他 她 我们 你们 他们 这 那 这个 那个 一个 一些 就是 还是
不 没 有 会 要 就 但 而 而且 因为 所以 如果 可以 自己 大家 非常 特别 真的 感觉 觉得 时候
之后 还有 的话 什么的 一次 有点 一点 一直 应该 可能 知道 出来 过来 下来 起来 上来
""".split())


def top_terms(texts, limit=30, min_count=3, use_jieba=True):
    """Chinese term frequencies; jieba when installed, maximal n-grams otherwise.

    The fallback keeps only maximal n-grams: a shorter gram is dropped when most
    of its occurrences sit inside a longer accepted one, which is what stops
    "月子中心" from also emitting "月子中" and "子中".
    """
    if use_jieba:
        try:
            import jieba  # type: ignore

            counter = collections.Counter()
            for t in texts:
                for w in jieba.cut(t):
                    w = w.strip()
                    if len(w) >= 2 and w not in STOPWORDS and not w.isdigit():
                        counter[w] += 1
            return [(w, c) for w, c in counter.most_common(limit) if c >= min_count]
        except ImportError:
            pass

    occurrences = collections.defaultdict(list)
    for ti, t in enumerate(texts):
        for n in (2, 3, 4):
            for i in range(len(t) - n + 1):
                gram = t[i:i + n]
                if gram not in STOPWORDS and re.fullmatch(r"[\u4e00-\u9fa5]+", gram):
                    occurrences[gram].append((ti, i, i + n))

    covered = collections.defaultdict(set)
    accepted = []
    for gram in sorted(occurrences, key=lambda g: (-len(g), -len(occurrences[g]), g)):
        spans = occurrences[gram]
        free = [s for s in spans if all(p not in covered[s[0]] for p in range(s[1], s[2]))]
        # 4-char phrases must be clearly frequent, otherwise a phrase fragment such
        # as "月子餐很" outranks the word "月子餐" on tiny corpora
        need = min_count if len(gram) <= 3 else max(min_count, 4)
        if len(free) >= need and len(free) >= 0.4 * len(spans):
            accepted.append((gram, len(free)))
            for ti, a, b in free:
                covered[ti].update(range(a, b))
    accepted.sort(key=lambda x: -x[1])
    return accepted[:limit]


# ---------------------------------------------------------------- entities

ROLE_WORDS = ["阿姨", "月嫂", "护士长", "护士", "护理师", "老师", "销售", "客服", "店长", "经理", "师傅", "医生"]
STOP_CHARS = set("的嫂师姨姐士心个位名谢感非特咱我他她家是和跟与对被给请找靠夸赞荐绍到遇面定了选顾护负责带再又还就都也很们您你此这那有在会能可要把让使从向于及同以为因由过通每好打扫天地")
BAN_CHARS = set("嫂师姨姐士心打扫卫生")
# tokens that pass the shape test but are never names; mined output is for humans to curate anyway
JUNK = set("""
查房 每天 都会 都会来 很好 我们 我的 还有 专业 产科 儿科 医生 护士 团队 服务 态度 时候 情况 感觉 觉得
特别 非常 真的 已经 可以 现在 之后 开始 结束 出来 过来 起来 人员 姐姐 妹妹 宝妈 宝宝 阿姨 姐姐们 小姐姐
费心 帮忙 照顾 陪伴 全程 一直 还会 负责 细心 耐心 用心 贴心 认真 努力 辛苦 上心 都特别 都很好 人都
产康 月子 护理 环境 设施 房间 餐食 月子餐 三餐 点心 会所 中心 医院 医生们 护士们 小姐姐们
金牌 神仙 高级 资深 优秀 明星 首席 王牌 五星 顶级 各位 现场 这次 感觉 体验 人员 团队们
保洁 验丰富 院陪 经验 丰富 医院陪 小姐姐 小仙女 阿姨们 护士们 医生们
""".split())
BAD_SUBSTR = "感谢谢夸赞荐"  # '感谢/特别感谢/夸赞' captured as a name


ROLE_PREFIX = set("嫂师姨姐士心护护理医生店经销售客服长") | STOP_CHARS
# a personal name never ends with a role character; stops fragments like "儿科医"
BAN_TAIL = set("医护士长师嫂姨姐销售客服员工房间")


def clean_entity(token):
    """Reduce a captured token to a 2-3 char candidate name.

    '护士长文文' -> '长文文' -> '文文' ; '金牌月嫂王丽娜' -> '王丽娜'.
    """
    s = token
    while len(s) > 3 and s[0] in STOP_CHARS:
        s = s[1:]
    if len(s) > 3:
        s = s[-3:]
    while len(s) > 2 and s[0] in ROLE_PREFIX:
        s = s[1:]
    while len(s) > 2 and s[0] in "和跟与及还":
        s = s[1:]
    return s


def _ok_entity(tok):
    if not (2 <= len(tok) <= 3):
        return False
    if any(ch in BAN_CHARS for ch in tok):
        return False
    if tok in JUNK or tok[0] in STOP_CHARS:
        return False
    if tok[-1] in "的地得" or tok[-1] in BAN_TAIL:
        return False
    if any(ch in BAD_SUBSTR for ch in tok):
        return False
    return True


def mine_entities(rows, role_words=ROLE_WORDS, min_count=2):
    """Frequent 2-3 char tokens sitting next to a role word, e.g. '月嫂王丽娜阿姨'.

    Both the "<role><name>" and "<name><role>" patterns can cover the same text
    span, so mentions are collected as unique (start, end) offsets per token —
    otherwise one mention is counted twice.
    """
    hits = collections.defaultdict(set)
    example = {}
    for r in rows:
        text = r["text"]
        for rw in role_words:
            for m in re.finditer(re.escape(rw) + r"([\u4e00-\u9fa5]{2,4})", text):
                tok = clean_entity(m.group(1))
                if _ok_entity(tok):
                    hits[tok].add((m.start(1), m.end(1)))
                    example.setdefault(tok, _ctx(text, m.start()))
            for m in re.finditer(r"([\u4e00-\u9fa5]{1,6})" + re.escape(rw), text):
                tok = clean_entity(m.group(1))
                if _ok_entity(tok):
                    # the name is the tail of the captured token, so anchor the span on it
                    start = m.end(1) - len(tok)
                    hits[tok].add((start, start + len(tok)))
                    example.setdefault(tok, _ctx(text, m.start()))
    ranked = sorted(hits.items(), key=lambda kv: -len(kv[1]))
    return [(n, len(spans), example.get(n, "")) for n, spans in ranked if len(spans) >= min_count]


def _ctx(text, pos, radius=25):
    return text[max(0, pos - radius):pos + radius].replace("\n", " ")


# ---------------------------------------------------------------- sentiment around an entity

NEG = re.compile(r"很差|差评|不专业|不负责|不道德|没人性|(?<!不会)(?<!不)(?<!别)(?<!没)失望|敷衍|偷懒|"
                 r"不耐心|不耐烦|遭罪|不满意|太离谱|不细心|不认真|无语|崩溃|态度差|极差|差劲|不合格|不靠谱|翻车")
POS = re.compile(r"专业|细心|耐心|推荐|感谢|夸|棒|好|赞|贴心|负责|温柔|满意|优秀|靠谱|能干")


def score_roster(rows, roster, radius=12):
    roles = roster.get("roles", roster)
    aliases = roster.get("aliases", {})
    name2role = {n: role for role, names in roles.items() for n in names}
    stats = collections.defaultdict(lambda: {
        "reviews": 0, "mentions": 0, "pos_ctx": 0, "neg_ctx": 0, "labels": collections.Counter(), "roles": set(),
    })
    for r in rows:
        text = r["text"]
        label = polarity(r["rating"])
        for name in name2role:
            spans = [m.start() for m in re.finditer(re.escape(name), text)]
            for alias, target in aliases.items():
                if target == name:
                    spans += [m.start() for m in re.finditer(re.escape(alias), text)]
            if not spans:
                continue
            s = stats[name]
            s["reviews"] += 1
            s["mentions"] += len(set(spans))
            s["labels"][label] += 1
            s["roles"].add(name2role[name])
            for p in sorted(set(spans)):
                w = text[max(0, p - radius):p + len(name) + radius]
                if NEG.search(w):
                    s["neg_ctx"] += 1
                elif POS.search(w):
                    s["pos_ctx"] += 1
    out = []
    for name, s in stats.items():
        out.append((name, {
            "role": "/".join(sorted(s["roles"])), "reviews": s["reviews"], "mentions": s["mentions"],
            "pos_ctx": s["pos_ctx"], "neg_ctx": s["neg_ctx"], "labels": dict(s["labels"]),
        }))
    return sorted(out, key=lambda kv: (-kv[1]["reviews"], -kv[1]["mentions"]))


# ---------------------------------------------------------------- subcommands

def cmd_stats(args):
    rows, meta = load(args.reviews, args.year)
    rows = apply_filters(rows, args)
    ratings = collections.Counter(r["rating"] for r in rows)
    buckets = collections.Counter(polarity(r["rating"]) for r in rows)
    dated = [r for r in rows if r["date"] and not r["date"].startswith("0000")]
    undated = len(rows) - len(dated)
    by_year = collections.Counter(r["date"][:4] for r in dated)
    spends = sorted(r["spend"] for r in rows if r["spend"])
    score_keys = sorted({k for r in rows for k in r["scores"]})
    score_avg = {}
    for k in score_keys:
        vals = [r["scores"][k] for r in rows if isinstance(r["scores"].get(k), (int, float))]
        if vals:
            score_avg[k] = round(statistics.fmean(vals), 2)
    lengths = [len(r["text"]) for r in rows]

    report = {
        "reviews": len(rows),
        "from_file": args.reviews,
        "shop": meta.get("shop"),
        "ratings": dict(ratings),
        "polarity": dict(buckets),
        "dated": len(dated),
        "undated": undated,
        "by_year": dict(sorted(by_year.items())),
        "date_range": [min((r["date"] for r in dated), default=None), max((r["date"] for r in dated), default=None)],
        "score_averages": score_avg,
        "spend": {
            "n": len(spends),
            "min": spends[0] if spends else None,
            "median": int(statistics.median(spends)) if spends else None,
            "max": spends[-1] if spends else None,
        },
        "text_length": {
            "median": int(statistics.median(lengths)) if lengths else 0,
            "max": max(lengths) if lengths else 0,
            "with_images": sum(1 for r in rows if r["imageCount"]),
        },
        "top_terms": top_terms([r["text"] for r in rows], limit=args.top_terms),
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(f"reviews           : {report['reviews']}")
        print(f"rating labels     : {report['ratings']}")
        print(f"polarity buckets  : {report['polarity']}")
        print(f"date range        : {report['date_range'][0]} … {report['date_range'][1]}  ({report['undated']} undated)")
        print(f"by year           : {report['by_year']}")
        print(f"score averages    : {report['score_averages']}")
        print(f"spend             : {report['spend']}")
        print(f"text length median: {report['text_length']['median']}  with images: {report['text_length']['with_images']}")
        print(f"top terms         : " + ", ".join(f"{w}×{c}" for w, c in report["top_terms"][:20]))
    return 0


def cmd_search(args):
    rows, _ = load(args.reviews, args.year)
    rows = apply_filters(rows, args)
    rx = re.compile(args.pattern)
    hits = []
    for r in rows:
        for m in rx.finditer(r["text"]):
            hits.append({
                "match": m.group(0),
                "author": r["author"],
                "date": r["date"] or r["date_raw"],
                "rating": r["rating"],
                "polarity": polarity(r["rating"]),
                "context": _ctx(r["text"], m.start(), args.context),
            })
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=1))
        return 0
    print(f"{len(hits)} match(es) in {len({h['author'] + h['date'] for h in hits})} review(s), file={args.reviews}")
    for h in hits:
        print(f"\n[{h['rating']}] {h['author']} · {h['date']}\n  …{h['context']}…")
    return 0


def cmd_entities(args):
    rows, _ = load(args.reviews, args.year)
    rows = apply_filters(rows, args)
    result = {"mined": [], "roster": []}
    if not args.roster_only:
        roles = [w for w in (args.role_words or "").split(",") if w] or ROLE_WORDS
        result["mined"] = [{"name": n, "count": c, "example": ex}
                           for n, c, ex in mine_entities(rows, roles, args.min_count)]
    if args.roster:
        with open(args.roster, encoding="utf-8") as f:
            roster = json.load(f)
        result["roster"] = [{"name": n, **s} for n, s in score_roster(rows, roster)]
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        if result["mined"]:
            print(f"# mined entities (n={len(result['mined'])}), curate before trusting:\n")
            print(f"{'name':<10}{'count':>6}  example")
            for e in result["mined"][:args.top]:
                print(f"{e['name']:<10}{e['count']:>6}  {e['example'][:60]}")
        if result["roster"]:
            print(f"\n# roster scoring (n={len(result['roster'])})\n")
            print(f"{'name':<10}{'role':<12}{'rev':>5}{'mentions':>9}{'pos':>5}{'neg':>5}  labels")
            for e in result["roster"][:args.top]:
                print(f"{e['name']:<10}{e['role']:<12}{e['reviews']:>5}{e['mentions']:>9}"
                      f"{e['pos_ctx']:>5}{e['neg_ctx']:>5}  {e['labels']}")
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "entities.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        if result["roster"]:
            with open(os.path.join(args.out, "entities.csv"), "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["name", "role", "reviews", "mentions", "pos_ctx", "neg_ctx", "labels"])
                for e in result["roster"]:
                    w.writerow([e["name"], e["role"], e["reviews"], e["mentions"], e["pos_ctx"], e["neg_ctx"], e["labels"]])
        print(f"\nwrote {os.path.join(args.out, 'entities.json')}")
    if args.chart:
        chart_entities(rows, result, args)
    return 0


def cmd_merge(args):
    merged, seen, stats = [], set(), []
    for p in args.reviews:
        rows, meta = load(p, args.year)
        added = 0
        for r in rows:
            k = dedupe_key(r)
            if k in seen:
                continue
            seen.add(k)
            merged.append(r)
            added += 1
        stats.append({"file": p, "loaded": len(rows), "added": added, "duplicates": len(rows) - added})
    out = {"meta": {"mergedFrom": stats, "records": len(merged), "mergedAt": None}, "reviews": merged}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    for s in stats:
        print(f"{s['file']}: loaded {s['loaded']}, added {s['added']}, dropped {s['duplicates']} duplicates")
    print(f"merged -> {args.out} ({len(merged)} records)")
    return 0


def cmd_export(args):
    rows, _ = load(args.reviews, args.year)
    rows = apply_filters(rows, args)
    if args.format == "csv":
        with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["author", "date", "rating", "polarity", "环境", "护理", "月子餐", "spend", "imageCount", "text"])
            for r in rows:
                w.writerow([r["author"], r["date"] or r["date_raw"], r["rating"], polarity(r["rating"]),
                            r["scores"].get("环境", ""), r["scores"].get("护理", ""), r["scores"].get("月子餐", ""),
                            r["spend"] if r["spend"] is not None else "", r["imageCount"], r["text"].replace("\n", " ")])
    elif args.format == "jsonl":
        with open(args.out, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(f"# {len(rows)} reviews from {args.reviews}\n\n")
            for r in rows:
                f.write(f"## [{r['rating']}] {r['author']} · {r['date'] or r['date_raw']}\n\n{r['text']}\n\n")
    print(f"{len(rows)} review(s) -> {args.out} ({args.format})")
    return 0


def cmd_chart(args):
    rows, meta = load(args.reviews, args.year)
    rows = apply_filters(rows, args)
    result = {"mined": [], "roster": []}
    if args.roster:
        with open(args.roster, encoding="utf-8") as f:
            roster = json.load(f)
        result["roster"] = [{"name": n, **s} for n, s in score_roster(rows, roster)]
    else:
        roles = [w for w in (args.role_words or "").split(",") if w] or ROLE_WORDS
        result["mined"] = [{"name": n, "count": c, "example": ex} for n, c, ex in mine_entities(rows, roles, args.min_count)][:15]
    chart_entities(rows, result, args, meta=meta)
    return 0


def chart_entities(rows, result, args, meta=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    available = {f.name for f in font_manager.fontManager.ttflist}
    for fam in ("Hiragino Sans GB", "Arial Unicode MS", "Noto Sans CJK SC", "Microsoft YaHei", "PingFang SC", "SimHei"):
        if fam in available:
            plt.rcParams["font.family"] = fam
            break
    plt.rcParams["axes.unicode_minus"] = False

    out_dir = args.out or "."
    os.makedirs(out_dir, exist_ok=True)
    title = args.title or (meta or {}).get("shop") or args.reviews

    fig, axes = plt.subplots(2, 2, figsize=(15, 10), dpi=110)
    ax1, ax2, ax3, ax4 = axes.flat

    ratings = collections.Counter(r["rating"] for r in rows)
    labels = sorted(ratings, key=lambda k: -ratings[k])
    ax1.bar(labels, [ratings[k] for k in labels], color="#1565c0")
    for i, k in enumerate(labels):
        ax1.text(i, ratings[k], str(ratings[k]), ha="center", va="bottom", fontsize=9)
    ax1.set_title(f"评级分布 (n={len(rows)})")
    ax1.grid(axis="y", alpha=.25)

    years = collections.Counter((r["date"] or "unknown")[:4] for r in rows)
    ys = sorted(years, key=lambda y: (y == "unknown", y))
    ax2.bar(ys, [years[y] for y in ys], color="#00897b")
    for i, y in enumerate(ys):
        ax2.text(i, years[y], str(years[y]), ha="center", va="bottom", fontsize=9)
    ax2.set_title("发布时间分布（0000=点评只给了月日）")
    ax2.grid(axis="y", alpha=.25)

    if result.get("roster"):
        names = [e["name"] for e in result["roster"][:16]][::-1]
        pos = [e["pos_ctx"] for e in result["roster"][:16]][::-1]
        neg = [e["neg_ctx"] for e in result["roster"][:16]][::-1]
        ax3.barh(names, pos, color="#2e7d32", label="positive context")
        ax3.barh(names, neg, left=pos, color="#c62828", label="negative context")
        ax3.legend(fontsize=8)
        ax3.set_title("实体口碑（roster 打分）")
        ax4.axis("off")
        top = result["roster"][:14]
        table = [[e["name"], e["role"], e["reviews"], e["pos_ctx"], e["neg_ctx"]] for e in top]
        t = ax4.table(cellText=table, colLabels=["name", "role", "rev", "+ctx", "-ctx"], loc="center")
        t.auto_set_font_size(False)
        t.set_fontsize(8)
        t.scale(1, 1.3)
        ax4.set_title("明细", fontsize=11)
    else:
        mined = result.get("mined", [])[:16]
        names = [m["name"] for m in mined][::-1]
        cnts = [m["count"] for m in mined][::-1]
        ax3.barh(names, cnts, color="#ef6c00")
        for i, c in enumerate(cnts):
            ax3.text(c, i, f" {c}", va="center", fontsize=8)
        ax3.set_title("高频实体（未校准，需人工核对）")
        ax4.axis("off")
        terms = top_terms([r["text"] for r in rows], limit=15)
        ax4.axis("off")
        text = "\n".join(f"{w} × {c}" for w, c in terms)
        ax4.text(0.02, 0.98, "高频词\n\n" + text, va="top", fontsize=10, family=plt.rcParams["font.family"])

    fig.suptitle(f"{title} · {len(rows)} 条评价", fontsize=14)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    p = os.path.join(out_dir, "reviews-overview.png")
    fig.savefig(p, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {p}")


# ---------------------------------------------------------------- cli

def add_common(p):
    p.add_argument("--reviews", required=True, help="harvested reviews.json")
    p.add_argument("--year", type=int, default=None, help="year to assume when the site omits it")
    p.add_argument("--rating", action="append", help="keep only these platform labels (repeatable)")
    p.add_argument("--polarity", action="append", choices=["好评", "好评(偏中)", "中评/差评"])
    p.add_argument("--grep", help="regex the review text must match")
    p.add_argument("--since", help="YYYY-MM-DD lower bound")
    p.add_argument("--until", help="YYYY-MM-DD upper bound")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("stats", help="aggregate overview")
    add_common(p)
    p.add_argument("--top-terms", type=int, default=25)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("search", help="regex search with context")
    add_common(p)
    p.add_argument("--pattern", required=True)
    p.add_argument("--context", type=int, default=40)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("entities", help="mine / score named entities")
    add_common(p)
    p.add_argument("--role-words", help="comma separated, e.g. 阿姨,护士,店长")
    p.add_argument("--roster", help="JSON with {roles:{role:[names]}, aliases:{}}")
    p.add_argument("--roster-only", action="store_true")
    p.add_argument("--min-count", type=int, default=2)
    p.add_argument("--top", type=int, default=40)
    p.add_argument("--json", action="store_true")
    p.add_argument("--out")
    p.add_argument("--chart", action="store_true")
    p.add_argument("--title")
    p.set_defaults(func=cmd_entities)

    p = sub.add_parser("merge", help="union + de-duplicate several harvests")
    p.add_argument("--reviews", action="append", required=True)
    p.add_argument("--year", type=int, default=None)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("export", help="reviews -> csv/jsonl/markdown")
    add_common(p)
    p.add_argument("--format", choices=["csv", "jsonl", "md"], default="csv")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("chart", help="render the overview figure")
    add_common(p)
    p.add_argument("--roster")
    p.add_argument("--role-words")
    p.add_argument("--min-count", type=int, default=2)
    p.add_argument("--out", default=".")
    p.add_argument("--title")
    p.set_defaults(func=cmd_chart)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
