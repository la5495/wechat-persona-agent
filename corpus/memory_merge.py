# -*- coding: utf-8 -*-
"""memory_merge.py —— 优化：把小而相邻的片段合并成一段（重新总结）

问题：主人聊天很碎，57% 的片段只有 6-20 条消息，"约游戏"这种小事被切成好几段，
检索时容易只命中半截。

规则（保守）：
  · 同一个会话
  · 相邻片段间隔 < 2 小时
  · 合并后总条数 ≤ 40
  · 组内至少有 1 段 ≤ 10 条
效果：把碎片并成 15-40 条的整段，重新用本地模型总结一次。

用法：
  python corpus/memory_merge.py --dry-run     # 只看会并多少
  python corpus/memory_merge.py               # 真跑（本地模型，零 API 费）
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import memory  # noqa: E402
from memory_build import MODEL, ollama, parse_json, transcript  # noqa: E402

TURNS = os.path.join(HERE, "turns")
MAX_GROUP_MSGS = 40
MAX_GAP_SEC = 2 * 3600
SMALL = 10


def build_groups(eps):
    """eps 行 = (id, chat, start_ts, end_ts, msg_count, n_mine, topic, summary, importance)
    已按 start_ts 排序 → 返回分组列表"""
    groups, cur = [], []
    for e in eps:
        if not cur:
            cur = [e]
            continue
        gap = e[2] - cur[-1][3]                     # 本段开始 - 上段结束
        total = sum(x[4] for x in cur) + e[4]
        if gap <= MAX_GAP_SEC and total <= MAX_GROUP_MSGS and \
                (min(x[4] for x in cur + [e]) <= SMALL):
            cur.append(e)
        else:
            groups.append(cur)
            cur = [e]
    if cur:
        groups.append(cur)
    return groups


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = memory.init()
    # 载入语料（按会话）
    corpus = {}
    for fp in sorted(glob.glob(os.path.join(TURNS, "*.jsonl"))):
        chat = os.path.basename(fp)[:-6]
        corpus[chat] = [json.loads(l) for l in open(fp, encoding="utf-8") if l.strip()]

    rows = list(con.execute(
        "SELECT id,chat,start_ts,end_ts,msg_count,n_mine,topic,summary,importance "
        "FROM episode ORDER BY chat, start_ts"))
    by_chat = {}
    for r in rows:
        by_chat.setdefault(r[1], []).append(r)

    plan = []
    for chat, eps in by_chat.items():
        for g in build_groups(eps):
            if len(g) > 1:
                plan.append((chat, g))

    n_ep_before = len(rows)
    n_ep_after = n_ep_before - sum(len(g) for _, g in plan) + len(plan)
    print("片段 %d → %d（合并 %d 组，减少 %d 段）"
          % (n_ep_before, n_ep_after, len(plan), n_ep_before - n_ep_after))
    print()
    for chat, g in plan[:5]:
        print("  例：[%s] %s → %s ｜ %d 段 %d 条 → 1 段 %d 条"
              % (chat, time.strftime("%m-%d %H:%M", time.localtime(g[0][2])),
                 time.strftime("%m-%d %H:%M", time.localtime(g[-1][3])),
                 len(g), sum(x[4] for x in g), sum(x[4] for x in g)))
    if args.dry_run:
        print("\n（--dry-run：什么都没改）")
        return 0

    print("\n开始合并（本地 %s，零 API 费）…\n" % MODEL)
    t0 = time.time()
    done = 0
    for chat, g in plan:
        msgs = corpus.get(chat) or []
        s_ts, e_ts = g[0][2], g[-1][3]
        seg = [m for m in msgs if s_ts <= m["ts"] <= e_ts]
        if len(seg) < 6:
            continue
        body = transcript(seg)
        try:
            data = parse_json(ollama("【会话】%s\n【记录】\n%s\n" % (chat, body)))
        except Exception as e:
            print("   ⚠️ %s 合并失败（%s），保留原样" % (chat, type(e).__name__))
            continue
        if not data.get("summary"):
            continue
        ids = [x[0] for x in g]
        con.execute("DELETE FROM episode_fts WHERE rowid IN (%s)"
                    % ",".join("?" * len(ids)), ids)
        con.execute("DELETE FROM episode WHERE id IN (%s)"
                    % ",".join("?" * len(ids)), ids)
        n_mine = sum(1 for m in seg if m["who"] == "me")
        try:
            imp = int(data.get("importance", 2) or 2)
        except (TypeError, ValueError):
            imp = 2
        memory.add_episode(con, chat, s_ts, e_ts, str(data.get("topic") or ""),
                           str(data.get("summary") or ""), imp, len(seg), n_mine, MODEL)
        done += 1
        if done % 20 == 0:
            print("   已合并 %d/%d 组…" % (done, len(plan)))

    st = memory.stats(con)
    print("\n合并完成：%d 组，用时 %.1f 分钟" % (done, (time.time() - t0) / 60))
    print("库内现状：片段 %d ｜ 事实 %d" % (st["episode"], st["fact"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
