# -*- coding: utf-8 -*-
"""memory_tidy.py —— 记忆库大扫除（第 ⑩ 项）

长期跑下去记忆库只增不减会越来越吵，这个脚本做三件事：

  1. **去重合并**：说得几乎一样的事实（bigram ≥0.82）合并成一条，
     保留更完整/更可信的那句，来源会话合并。
  2. **过期清理**：临时事实（「明天要交作业」这类）超过 N 天就删。
  3. **报告**：前后对比，让人家知道库干净了多少。

用法：
  python corpus/memory_tidy.py --dry-run    # 只看会动什么
  python corpus/memory_tidy.py              # 真清理
  python corpus/memory_tidy.py --expire-days 7
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memory  # noqa: E402

DUP_THRESHOLD = 0.82


def sim(a: str, b: str) -> float:
    A, B = memory._bigrams(a or ""), memory._bigrams(b or "")
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--expire-days", type=int, default=7,
                    help="临时事实超过这么多天就删（默认 7）")
    ap.add_argument("--dup-threshold", type=float, default=DUP_THRESHOLD)
    args = ap.parse_args()

    con = memory.init()
    rows = list(con.execute(
        "SELECT id,statement,predicate,object,chat,ts,confidence FROM fact ORDER BY id"))
    n0 = len(rows)
    now = time.time()

    # ---- 1. 去重合并 ----
    kept, dups = [], []
    for r in rows:
        fid, st, pred, obj, chat, ts, conf = r
        hit = None
        for k in kept:
            if sim(st, k[1]) >= args.dup_threshold:
                hit = k
                break
        if hit is None:
            kept.append(list(r))
            continue
        dups.append((st, hit[1]))
        # 合并：保留更长的说法 + 更高的置信度 + 来源会话取并集
        if len(st or "") > len(hit[1] or ""):
            hit[1] = st
        hit[6] = max(hit[6] or 0.7, conf or 0.7)
        if chat and chat not in (hit[4] or ""):
            hit[4] = ((hit[4] + "," + chat).strip(",") if hit[4] else chat)

    # ---- 2. 过期清理（临时事实）----
    expired, final = [], []
    for k in kept:
        age_days = (now - (k[5] or now)) / 86400.0
        if memory.is_transient(k[1]) and age_days > args.expire_days:
            expired.append((k[1], int(age_days)))
        else:
            final.append(k)

    print("事实总数：%d" % n0)
    print("  ① 重复合并：%d 条 → 合并掉 %d 条" % (len(kept), len(dups)))
    for a, b in dups[:5]:
        print("       · 「%s」 ⇒ 并入「%s」" % (a[:26], b[:26]))
    if len(dups) > 5:
        print("       … 另有 %d 条" % (len(dups) - 5))
    print("  ② 过期清理：%d 条（临时事实且超过 %d 天）" % (len(expired), args.expire_days))
    for st, age in expired[:5]:
        print("       · %s（%d 天前）" % (st[:30], age))
    if len(expired) > 5:
        print("       … 另有 %d 条" % (len(expired) - 5))
    print("  清理后：%d 条（少了 %d 条，%.0f%%）"
          % (len(final), n0 - len(final), 100.0 * (n0 - len(final)) / max(n0, 1)))

    if args.dry_run:
        print("\n（--dry-run：什么都没改）")
        return 0

    # ---- 3. 落库 ----
    drop_ids = set()
    keep_ids = set()
    for k in final:
        keep_ids.add(k[0])
    for r in rows:
        if r[0] not in keep_ids:
            drop_ids.add(r[0])
    try:
        for fid in drop_ids:
            con.execute("DELETE FROM fact_fts WHERE rowid=?", (fid,))
            con.execute("DELETE FROM fact WHERE id=?", (fid,))
        # 更新被合并那条的内容
        for k in final:
            con.execute("UPDATE fact SET statement=?, chat=?, confidence=? WHERE id=?",
                        (k[1], k[4], k[6], k[0]))
        con.commit()
        print("\n✅ 已清理：删除 %d 条，更新 %d 条" % (len(drop_ids), len(final)))
    except Exception as e:
        print("\n⚠️ 清理失败：%s: %s" % (type(e).__name__, e))
        return 2

    st = memory.stats(con)
    print("库内现状：片段 %d ｜ 事实 %d" % (st["episode"], st["fact"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
