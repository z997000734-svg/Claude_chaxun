#!/usr/bin/env python3
"""脚本查重工具。

两种对比方式（可以同时使用）：
  1. 和最近 N 天（默认 2 天）的历史脚本查重
  2. 和指定的一个或多个脚本对比

只依赖 Python 标准库，支持中文 / 英文文本，也支持代码文件。

用法示例：
  # 和 scripts/ 目录里最近两天的脚本查重
  python script_checker.py 今天的脚本.txt

  # 和指定脚本对比
  python script_checker.py 今天的脚本.txt -t 参考脚本.txt

  # 两种一起做，历史库换成别的目录，查最近 3 天
  python script_checker.py 今天的脚本.txt -d 我的脚本库 --days 3 -t 参考脚本.txt
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

TEXT_EXTS = {
    ".txt", ".md", ".py", ".sh", ".js", ".ts", ".lua", ".bat", ".ps1",
    ".json", ".yaml", ".yml", ".ini", ".cfg", ".srt", ".csv", ".html",
}

# 句子切分：中英文标点和换行
_SENT_SPLIT = re.compile(r"[。！？!?；;\n\r]+")
# 规范化时去掉的字符：空白和常见标点
_NOISE = re.compile(r"[\s，,。．.！!？?；;：:、“”\"'‘’（）()【】\[\]《》<>…—\-~·`*#]+")
# 文件名或目录名里的日期：2026-11-02 / 2026_11_02 / 20261102
_DATE_IN_NAME = re.compile(r"(20\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])")


# ---------------------------------------------------------------- 文本处理

def read_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def normalize(text: str) -> str:
    return _NOISE.sub("", text).lower()


def split_sentences(text: str, min_len: int) -> list[str]:
    """切成句子，太短的句子（如“好的”）不参与查重。"""
    out = []
    for raw in _SENT_SPLIT.split(text):
        raw = raw.strip()
        if len(normalize(raw)) >= min_len:
            out.append(raw)
    return out


def ngrams(s: str, n: int = 3) -> set[str]:
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


# ---------------------------------------------------------------- 对比逻辑

@dataclass
class SentenceHit:
    sentence: str        # 当前脚本里的句子
    matched: str         # 对比脚本里最相似的句子
    similarity: float    # 0~1


@dataclass
class CompareResult:
    other: str
    reason: str                      # “最近N天” / “指定对比”
    duplicate_rate: float            # 重复句子字数 / 总字数
    overall_similarity: float        # 全文相似度（difflib）
    ngram_overlap: float             # 当前脚本的 3-gram 有多少出现在对比脚本里
    hits: list[SentenceHit] = field(default_factory=list)


def compare(current: str, other: str, *, threshold: float, min_len: int,
            other_name: str, reason: str) -> CompareResult:
    cur_norm, oth_norm = normalize(current), normalize(other)

    overall = difflib.SequenceMatcher(None, cur_norm, oth_norm, autojunk=False).ratio() \
        if cur_norm and oth_norm else 0.0

    cur_grams, oth_grams = ngrams(cur_norm), ngrams(oth_norm)
    overlap = len(cur_grams & oth_grams) / len(cur_grams) if cur_grams else 0.0

    other_sents = [(s, normalize(s)) for s in split_sentences(other, min_len)]
    other_gram_index = [(s, n, ngrams(n)) for s, n in other_sents]

    hits: list[SentenceHit] = []
    dup_chars = total_chars = 0
    for sent in split_sentences(current, min_len):
        sn = normalize(sent)
        total_chars += len(sn)
        sg = ngrams(sn)
        best_sim, best_match = 0.0, ""
        for os_raw, on, og in other_gram_index:
            # 先用 n-gram 粗筛，避免对每一对句子都跑 difflib
            if sg and og and len(sg & og) / max(len(sg), 1) < threshold * 0.5:
                continue
            sim = difflib.SequenceMatcher(None, sn, on, autojunk=False).ratio()
            if sim > best_sim:
                best_sim, best_match = sim, os_raw
                if sim == 1.0:
                    break
        if best_sim >= threshold:
            dup_chars += len(sn)
            hits.append(SentenceHit(sent, best_match, round(best_sim, 4)))

    return CompareResult(
        other=other_name,
        reason=reason,
        duplicate_rate=round(dup_chars / total_chars, 4) if total_chars else 0.0,
        overall_similarity=round(overall, 4),
        ngram_overlap=round(overlap, 4),
        hits=hits,
    )


# ---------------------------------------------------------------- 找历史脚本

def script_date(path: Path, root: Path) -> dt.date:
    """优先用路径里的日期（文件名或日期文件夹），没有就用文件修改时间。"""
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        rel_parts = path.parts
    for part in reversed(rel_parts):
        m = _DATE_IN_NAME.search(part)
        if m:
            try:
                return dt.date(int(m[1]), int(m[2]), int(m[3]))
            except ValueError:
                pass
    return dt.date.fromtimestamp(path.stat().st_mtime)


def recent_scripts(root: Path, days: int, today: dt.date, exclude: Path,
                   exts: set[str]) -> list[tuple[Path, dt.date]]:
    """最近 days 天的脚本：不含今天，即 today-days ~ today-1。"""
    if not root.is_dir():
        return []
    start = today - dt.timedelta(days=days)
    found = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        if p.resolve() == exclude.resolve():
            continue
        d = script_date(p, root)
        if start <= d < today:
            found.append((p, d))
    return found


# ---------------------------------------------------------------- 输出

def level(rate: float) -> str:
    if rate >= 0.5:
        return "高度重复"
    if rate >= 0.2:
        return "部分重复"
    return "基本原创"


def print_report(current: Path, results: list[CompareResult], show: int) -> None:
    print(f"查重脚本：{current}")
    print("=" * 60)
    if not results:
        print("没有找到可对比的脚本。")
        return
    for r in sorted(results, key=lambda x: x.duplicate_rate, reverse=True):
        print(f"\n[{r.reason}] {r.other}")
        print(f"  重复率：{r.duplicate_rate:.1%}（{level(r.duplicate_rate)}）  "
              f"全文相似度：{r.overall_similarity:.1%}  片段重合：{r.ngram_overlap:.1%}")
        if r.hits:
            print(f"  重复句子 {len(r.hits)} 句：")
            for h in r.hits[:show]:
                print(f"    - ({h.similarity:.0%}) {h.sentence}")
                if normalize(h.sentence) != normalize(h.matched):
                    print(f"        ≈ {h.matched}")
            if len(r.hits) > show:
                print(f"    ……还有 {len(r.hits) - show} 句，用 --show 调大显示数量")
    worst = max(results, key=lambda x: x.duplicate_rate)
    print("\n" + "=" * 60)
    print(f"最高重复率：{worst.duplicate_rate:.1%}，来自 {worst.other}")


# ---------------------------------------------------------------- 入口

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="脚本查重：和最近几天的脚本、或指定脚本对比")
    ap.add_argument("script", help="要检查的脚本文件")
    ap.add_argument("-t", "--target", action="append", default=[],
                    help="指定对比的脚本，可以写多次")
    ap.add_argument("-d", "--dir", default="scripts",
                    help="历史脚本目录（默认 scripts/）")
    ap.add_argument("--days", type=int, default=2,
                    help="和最近几天的脚本比（默认 2，不含今天；0 表示不和历史比）")
    ap.add_argument("--today", help="把哪天当作今天，格式 YYYY-MM-DD（默认真实日期）")
    ap.add_argument("--threshold", type=float, default=0.8,
                    help="句子相似度达到多少算重复（0~1，默认 0.8）")
    ap.add_argument("--min-len", type=int, default=6,
                    help="少于多少个字的句子不参与查重（默认 6）")
    ap.add_argument("--show", type=int, default=10, help="每个对比最多显示几句重复句")
    ap.add_argument("--fail-above", type=float,
                    help="任一重复率超过该值时返回码为 1，方便自动化（如 0.3）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = ap.parse_args(argv)

    current = Path(args.script)
    if not current.is_file():
        ap.error(f"找不到文件：{current}")
    text = read_text(current)
    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()

    jobs: list[tuple[Path, str]] = []
    for t in args.target:
        tp = Path(t)
        if not tp.is_file():
            ap.error(f"找不到对比文件：{tp}")
        jobs.append((tp, "指定对比"))
    if args.days > 0:
        for p, d in recent_scripts(Path(args.dir), args.days, today, current, TEXT_EXTS):
            if all(p.resolve() != j.resolve() for j, _ in jobs):
                jobs.append((p, f"最近{args.days}天 {d.isoformat()}"))

    results = [
        compare(text, read_text(p), threshold=args.threshold, min_len=args.min_len,
                other_name=str(p), reason=reason)
        for p, reason in jobs
    ]

    if args.json:
        print(json.dumps({"script": str(current), "results": [asdict(r) for r in results]},
                         ensure_ascii=False, indent=2))
    else:
        print_report(current, results, args.show)

    if args.fail_above is not None and any(r.duplicate_rate > args.fail_above for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
