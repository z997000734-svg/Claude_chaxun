import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path

import script_checker as sc

A = "大家好，今天给大家介绍一款超级好用的保温杯。它采用三百零四不锈钢内胆，保温时间长达十二个小时。现在下单还送杯套！"
B = "大家好，今天给大家介绍一款超级好用的保温杯！它采用304不锈钢内胆。颜色有五种可以选择，总有一款适合你。"
C = "周末去哪里玩？推荐你去城郊的湿地公园，那里空气清新，适合带孩子散步拍照。"


class CompareTest(unittest.TestCase):
    def run_cmp(self, a, b):
        return sc.compare(a, b, threshold=0.8, min_len=6, other_name="x", reason="r")

    def test_identical(self):
        r = self.run_cmp(A, A)
        self.assertEqual(r.duplicate_rate, 1.0)
        self.assertEqual(r.overall_similarity, 1.0)

    def test_partial(self):
        r = self.run_cmp(A, B)
        self.assertTrue(0 < r.duplicate_rate < 1)
        self.assertTrue(any("保温杯" in h.sentence for h in r.hits))

    def test_unrelated(self):
        r = self.run_cmp(A, C)
        self.assertEqual(r.duplicate_rate, 0.0)
        self.assertEqual(r.hits, [])

    def test_punctuation_and_space_ignored(self):
        r = self.run_cmp("今天 天气 真的很好啊。", "今天天气，真的很好啊！")
        self.assertEqual(r.duplicate_rate, 1.0)


class RecentScriptsTest(unittest.TestCase):
    def test_date_from_name_and_folder(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "2026-11-01").mkdir()
            files = {
                "2026-11-01/a.txt": A,       # 1 天前 -> 选中
                "20261031_b.txt": B,         # 2 天前 -> 选中
                "2026-10-30-c.txt": C,       # 3 天前 -> 不选
                "2026-11-02-today.txt": A,   # 今天 -> 不选
            }
            for name, text in files.items():
                (root / name).write_text(text, encoding="utf-8")
            cur = root / "2026-11-02-today.txt"
            found = sc.recent_scripts(root, 2, dt.date(2026, 11, 2), cur, sc.TEXT_EXTS)
            names = sorted(p.name for p, _ in found)
            self.assertEqual(names, ["20261031_b.txt", "a.txt"])

    def test_mtime_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "noname.txt"
            p.write_text(A, encoding="utf-8")
            ts = dt.datetime(2026, 11, 1, 12).timestamp()
            os.utime(p, (ts, ts))
            self.assertEqual(sc.script_date(p, Path(d)), dt.date(2026, 11, 1))

    def test_gbk_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "g.txt"
            p.write_bytes(A.encode("gbk"))
            self.assertEqual(sc.read_text(p), A)


class CliTest(unittest.TestCase):
    def test_fail_above(self):
        with tempfile.TemporaryDirectory() as d:
            cur, tgt = Path(d) / "cur.txt", Path(d) / "tgt.txt"
            cur.write_text(A, encoding="utf-8")
            tgt.write_text(A, encoding="utf-8")
            rc = sc.main([str(cur), "-t", str(tgt), "--days", "0", "--fail-above", "0.3", "--json"])
            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
