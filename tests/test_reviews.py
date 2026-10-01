#!/usr/bin/env python3
"""Behaviour tests for the review processing toolkit.

Run: python3 -m unittest discover -s tests -v
"""
import argparse
import csv
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import reviews as R  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "reviews.sample.json")
ROSTER = {
    "roles": {"月嫂/阿姨": ["张一云", "李文静", "陈可"], "护士/护理": ["孙恬"]},
    "aliases": {"恬恬": "孙恬"},
}


def ns(**kw):
    base = dict(reviews=FIXTURE, year=None, rating=None, polarity=None, grep=None, since=None, until=None)
    base.update(kw)
    return argparse.Namespace(**base)


class TestDates(unittest.TestCase):
    def test_absolute_date(self):
        self.assertEqual(R.parse_date("发布于2025年3月9日"), "2025-03-09")
        self.assertEqual(R.parse_date("发布于2025年3月9日丨编辑于2025年3月10日"), "2025-03-09")

    def test_relative_date_uses_assumed_year(self):
        self.assertEqual(R.parse_date("发布于9月24日", 2025), "2025-09-24")
        self.assertEqual(R.parse_date("发布于9月24日"), "0000-09-24")

    def test_unparseable(self):
        self.assertIsNone(R.parse_date(""))
        self.assertIsNone(R.parse_date("unknown"))


class TestPolarity(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(R.polarity("超预期"), "好评")
        self.assertEqual(R.polarity("很棒"), "好评(偏中)")
        self.assertEqual(R.polarity("不错"), "好评(偏中)")
        self.assertEqual(R.polarity("一般"), "中评/差评")
        self.assertEqual(R.polarity("差评"), "中评/差评")


class TestFilters(unittest.TestCase):
    def setUp(self):
        self.rows, _ = R.load(FIXTURE)

    def test_rating_filter(self):
        got = R.apply_filters(self.rows, ns(rating=["一般"]))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["author"], "dave")

    def test_polarity_filter(self):
        got = R.apply_filters(self.rows, ns(polarity=["中评/差评"]))
        self.assertEqual([r["author"] for r in got], ["dave"])

    def test_grep_filter(self):
        got = R.apply_filters(self.rows, ns(grep="陈可"))
        self.assertEqual(len(got), 1)

    def test_since_filter_ignores_undated(self):
        got = R.apply_filters(self.rows, ns(since="2025-01-01"))
        self.assertTrue(all(r["date"] and r["date"] >= "2025-01-01" for r in got))
        self.assertNotIn("erin", [r["author"] for r in got])


class TestMerge(unittest.TestCase):
    def test_duplicates_are_dropped_and_unique_kept(self):
        with tempfile.TemporaryDirectory() as d:
            a = os.path.join(d, "a.json")
            b = os.path.join(d, "b.json")
            out = os.path.join(d, "merged.json")
            payload = json.load(open(FIXTURE, encoding="utf-8"))
            json.dump(payload, open(a, "w", encoding="utf-8"), ensure_ascii=False)
            partial = {"reviews": payload["reviews"][:2] + [dict(payload["reviews"][2], author="new")]}
            json.dump(partial, open(b, "w", encoding="utf-8"), ensure_ascii=False)
            R.cmd_merge(argparse.Namespace(reviews=[a, b], out=out, year=None))
            merged = json.load(open(out, encoding="utf-8"))["reviews"]
            self.assertEqual(len(merged), 6, "5 unique + 1 genuinely new")
            originals, _ = R.load(a)
            self.assertEqual(R.dedupe_key(merged[0]), R.dedupe_key(originals[0]))


class TestSearch(unittest.TestCase):
    def test_finds_every_occurrence_with_context_and_polarity(self):
        rows, _ = R.load(FIXTURE)
        hits = []
        import re as _re
        rx = _re.compile("张一云")
        for r in rows:
            for m in rx.finditer(r["text"]):
                hits.append((r["author"], R.polarity(r["rating"]), m.start()))
        self.assertEqual(len(hits), 3, "one hit in review 1, two in review 0")
        self.assertTrue(all(p == "好评" for _, p, _ in hits))


class TestEntities(unittest.TestCase):
    def test_mines_name_next_to_role_word(self):
        rows, _ = R.load(FIXTURE)
        rows[0]["text"] = "我的金牌月嫂何桃阿姨太棒了，月嫂何桃阿姨很专业。" + rows[0]["text"]
        mined = dict((n, c) for n, c, _ in R.mine_entities(rows, ["月嫂", "阿姨"]))
        self.assertEqual(mined.get("何桃"), 2)

    def test_rejects_role_fragments(self):
        rows, _ = R.load(FIXTURE)
        rows[0]["text"] = "每天都有打扫卫生的阿姨来，送餐的阿姨也很好。"
        mined = dict((n, c) for n, c, _ in R.mine_entities(rows, ["阿姨"]))
        self.assertNotIn("卫生的", mined)

    def test_min_count_applies(self):
        rows, _ = R.load(FIXTURE)
        self.assertEqual(R.mine_entities(rows, ["阿姨", "护士"], min_count=5), [])


class TestRosterScoring(unittest.TestCase):
    def setUp(self):
        rows, _ = R.load(FIXTURE)
        self.by_name = dict(R.score_roster(rows, ROSTER))

    def test_alias_folds_into_canonical(self):
        self.assertIn("孙恬", self.by_name)
        self.assertEqual(self.by_name["孙恬"]["reviews"], 1)

    def test_review_counted_once_for_repeated_mentions(self):
        s = self.by_name["张一云"]
        self.assertEqual(s["reviews"], 2)
        self.assertEqual(s["mentions"], 3)
        self.assertEqual(s["labels"]["好评"], 2)

    def test_negation_guard(self):
        self.assertEqual(self.by_name["李文静"]["neg_ctx"], 0)

    def test_hostile_context_flagged(self):
        self.assertEqual(self.by_name["陈可"]["neg_ctx"], 1)

    def test_unnamed_complaint_not_attributed(self):
        for name, s in self.by_name.items():
            self.assertEqual(s["neg_ctx"], 1 if name == "陈可" else 0, name)


class TestExport(unittest.TestCase):
    def test_csv_roundtrip_with_filter(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out.csv")
            R.cmd_export(ns(out=out, format="csv", polarity=["好评"]))
            with open(out, encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 3, "reviews 0, 1 and 4 are 超预期")
            self.assertIn("text", rows[0])
            self.assertEqual(rows[0]["polarity"], "好评")

    def test_jsonl_keeps_every_record(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out.jsonl")
            R.cmd_export(ns(out=out, format="jsonl"))
            lines = [l for l in open(out, encoding="utf-8") if l.strip()]
            self.assertEqual(len(lines), 5)


class TestTerms(unittest.TestCase):
    def test_word_is_extracted(self):
        texts = ["月子餐很好吃，三餐三点很丰富", "月子餐很香，营养均衡", "月子餐搭配科学，不会发胖", "我觉得月子餐很不错"]
        terms = dict(R.top_terms(texts, limit=10, min_count=3, use_jieba=False))
        self.assertIn("月子餐", terms)

    def test_min_count_is_respected(self):
        terms = R.top_terms(["月子餐好吃", "月子餐不错"], limit=10, min_count=3, use_jieba=False)
        self.assertEqual(terms, [])

    def test_no_fully_contained_fragments(self):
        texts = ["月子中心环境安静，月子中心房间干净，月子中心的三餐很好"] * 2
        terms = [t for t, _ in R.top_terms(texts, limit=10, min_count=2, use_jieba=False)]
        for a in terms:
            for b in terms:
                if a != b and a in b:
                    self.fail(f"{a!r} is a fragment of {b!r}")


if __name__ == "__main__":
    unittest.main()
