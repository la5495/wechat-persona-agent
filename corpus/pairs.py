# -*- coding: utf-8 -*-
"""pairs.py —— P0 配对：抽「对方说X → 主人回Y」的真实样本

规则：
  · 连续的"我"的消息 = 一次回复（多条形如连发）
  · 紧邻其前的"对方"消息（30 分钟内）= 触发它的 incoming
  · 主人的表情 / 单字回复**照样保留**，单独打标签（那是风格）
输出：corpus/pairs.jsonl + corpus/pairs_stats.json
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TURNS = os.path.join(HERE, "turns")
OUT = os.path.join(HERE, "pairs.jsonl")
STATS = os.path.join(HERE, "pairs_stats.json")

GAP_SEC = 30 * 60          # 超过 30 分钟算新话题
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]")


def kind_of(msg):
    t = msg.get("type")
    if t == 47:
        return "sticker"
    if t == 3:
        return "image"
    if t != 1:
        return "other"
    s = (msg.get("text") or "").strip()
    if len(s) <= 2:
        return "short"          # 单字/两字：嗯、哦、哈哈
    if len(s) <= 6:
        return "brief"
    return "text"


def main():
    files = sorted(glob.glob(os.path.join(TURNS, "*.jsonl")))
    pairs = []
    stat = {"chats": {}, "kinds": Counter(), "reply_len": Counter(),
            "multi": 0, "with_emoji": 0, "total_replies": 0}

    for fp in files:
        chat = os.path.basename(fp)[:-6]
        msgs = [json.loads(l) for l in open(fp, encoding="utf-8") if l.strip()]
        n_pair = 0
        i = 0
        while i < len(msgs):
            if msgs[i]["who"] != "other":
                i += 1
                continue
            # incoming 段
            inc = []
            while i < len(msgs) and msgs[i]["who"] == "other":
                inc.append(msgs[i])
                i += 1
            if i >= len(msgs):
                break
            # 我的回复段
            rep = []
            while i < len(msgs) and msgs[i]["who"] == "me":
                rep.append(msgs[i])
                i += 1
            if not inc or not rep:
                continue
            gap = rep[0]["ts"] - inc[-1]["ts"]
            if gap > GAP_SEC:
                continue
            kinds = [kind_of(m) for m in rep]
            text = "\n".join((m.get("text") or "") for m in rep)
            rec = {
                "chat": chat,
                "ts": rep[0]["ts"],
                "time": rep[0]["time"],
                "incoming": [{"sender": m.get("sender"),
                              "text": m.get("text"),
                              "type": m.get("type")} for m in inc[-4:]],
                "reply": text,
                "reply_parts": [m.get("text") for m in rep],
                "reply_kinds": kinds,
                "reply_type": "sticker" if kinds == ["sticker"] else
                              ("short" if len(rep) == 1 and kinds[0] == "short" else
                               ("multi" if len(rep) > 1 else kinds[0])),
                "gap_sec": gap,
                "has_emoji": bool(EMOJI_RE.search(text or "")),
            }
            pairs.append(rec)
            n_pair += 1
            stat["kinds"][rec["reply_type"]] += 1
            stat["reply_len"][min(len(text or ""), 100)] += 1
            if len(rep) > 1:
                stat["multi"] += 1
            if rec["has_emoji"]:
                stat["with_emoji"] += 1
            stat["total_replies"] += 1
        stat["chats"][chat] = n_pair

    with open(OUT, "w", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    stat["kinds"] = dict(stat["kinds"])
    # ---- 数据驱动：对方发几条 → 主人回几条 / 回多少字 ----
    resp = {}
    for p in pairs:
        n_in = min(len(p.get("incoming") or []), 4)
        key = str(n_in)
        d = resp.setdefault(key, {"n": 0, "lines": Counter(), "chars": []})
        d["n"] += 1
        d["lines"][len((p.get("reply") or "").split("\n"))] += 1
        d["chars"].append(len(p.get("reply") or ""))
    response_map = {}
    for key, d in resp.items():
        if d["n"] < 5:
            continue
        mode = max(d["lines"].items(), key=lambda x: x[1])[0]
        tot_l = sum(k * v for k, v in d["lines"].items())
        cnt = sum(d["lines"].values())
        response_map[key] = {
            "samples": d["n"],
            "lines_mode": mode,
            "lines_avg": round(tot_l / max(cnt, 1), 2),
            "lines_dist": dict(sorted(d["lines"].items())),
            "chars_avg": round(sum(d["chars"]) / max(len(d["chars"]), 1), 1),
        }
    stat["response_map"] = response_map
    with open(STATS, "w", encoding="utf-8") as f:
        json.dump(stat, f, ensure_ascii=False, indent=2)

    print("配对数：%d" % len(pairs))
    print("回复形态分布：", stat["kinds"])
    print("连发多条：%d（%.1f%%） ｜ 带 emoji：%d（%.1f%%）"
          % (stat["multi"], 100.0 * stat["multi"] / max(len(pairs), 1),
             stat["with_emoji"], 100.0 * stat["with_emoji"] / max(len(pairs), 1)))
    print()
    print("== 抽样（每条：对方说 → 主人回） ==")
    import random
    random.seed(7)
    for p in random.sample(pairs, min(8, len(pairs))):
        inc = " / ".join((m.get("text") or "")[:26] for m in p["incoming"])
        print("  [%s] %s" % (p["chat"][:6], inc[:60]))
        print("        → %s   (%s)" % ((p["reply"] or "").replace("\n", " ⏎ ")[:60],
                                       p["reply_type"]))
    print()
    print("输出：%s" % os.path.relpath(OUT, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
