# -*- coding: utf-8 -*-
"""style_stats.py —— P0 统计：把「主人的风格」量化成可读的事实

不调用模型，纯统计。产出：
  corpus/style_stats.json   机器可读
  corpus/style_report.md    人可读（也是给本地模型蒸馏的输入）
"""
from __future__ import annotations

import glob
import json
import os
import re
import statistics
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TURNS = os.path.join(HERE, "turns")

PUNCT = {
    "句号。": "。", "感叹号！": "！", "问号？": "？", "省略号…": "…",
    "波浪号～": "～", "逗号，": "，", "顿号、": "、", "空格": " ",
}


def default_stats():
    return {
        "messages": 0, "chars": 0, "len_hist": Counter(),
        "punct": Counter(), "starters": Counter(), "enders": Counter(),
        "ngrams": Counter(), "burst_len": Counter(), "exact": Counter(),
        "turn_chars": Counter(),
        "sticker": 0, "image": 0, "short": 0,
    }


PLACEHOLDER_RE = re.compile(r"^\[.+\]$")


def add_msg(st, text, mtype):
    st["messages"] += 1
    if mtype == 47:
        st["sticker"] += 1
        return
    if mtype == 3:
        st["image"] += 1
        return
    s = (text or "").strip()
    if not s or PLACEHOLDER_RE.match(s):
        return                      # [语音]/[图片]/[链接] 之类的占位符不参与文字统计
    st["chars"] += len(s)
    st["len_hist"][min(len(s), 60)] += 1
    if len(s) <= 2:
        st["short"] += 1
    st["exact"][s] += 1
    for label, ch in PUNCT.items():
        if ch in s:
            st["punct"][label] += 1
    st["starters"][s[:2]] += 1
    st["enders"][s[-2:]] += 1
    # 高频片段：只统计**纯中文**的 2-4 字组合，避免拼音/英文碎片污染
    clean = re.sub(r"[^\u4e00-\u9fff]", "", s)
    if clean:
        for n in (2, 3, 4):
            for i in range(len(clean) - n + 1):
                st["ngrams"][clean[i:i + n]] += 1


def finalize(st):
    st["len_hist"] = dict(sorted(st["len_hist"].items()))
    st["punct"] = dict(st["punct"])
    st["starters"] = st["starters"].most_common(20)
    st["enders"] = st["enders"].most_common(20)
    # 只保留出现 >=5 次的片段，按次数排序
    st["ngrams"] = [(g, c) for g, c in st["ngrams"].most_common(60) if c >= 5]
    # 口头禅：完全重复的原句（>=5 次）
    st["catchphrases"] = [(t, c) for t, c in st["exact"].most_common(40) if c >= 5]
    del st["exact"]
    # 一轮回复的合计字数（均值 + 分位）
    tc = {int(k): v for k, v in st["turn_chars"].items()}
    tot = sum(tc.values())
    if tot:
        acc = 0
        for k in sorted(tc):
            acc += tc[k]
            if acc >= tot * 0.5 and "turn_chars_median" not in st:
                st["turn_chars_median"] = k
            if acc >= tot * 0.9 and "turn_chars_p90" not in st:
                st["turn_chars_p90"] = k
        st["turn_chars_avg"] = round(sum(k * v for k, v in tc.items()) / tot, 1)
        st["turn_chars_n"] = tot
    st["turn_chars"] = dict(sorted(tc.items()))
    st["burst_len"] = dict(sorted(st["burst_len"].items()))
    return st


def main():
    files = sorted(glob.glob(os.path.join(TURNS, "*.jsonl")))
    glob_st = default_stats()
    per_chat = {}

    for fp in files:
        chat = os.path.basename(fp)[:-6]
        st = default_stats()
        msgs = [json.loads(l) for l in open(fp, encoding="utf-8") if l.strip()]
        # 每条我发的消息
        for m in msgs:
            if m["who"] != "me":
                continue
            add_msg(st, m.get("text"), m.get("type"))
            add_msg(glob_st, m.get("text"), m.get("type"))
        # 连发长度（我连续发几条算一波）+ 每轮合计字数
        burst = 0
        burst_chars = 0
        for m in msgs:
            if m["who"] == "me":
                burst += 1
                txt = (m.get("text") or "").strip()
                if m.get("type") == 1 and txt and not PLACEHOLDER_RE.match(txt):
                    burst_chars += len(txt)
            else:
                if burst:
                    st["burst_len"][min(burst, 8)] += 1
                    st["turn_chars"][min(burst_chars, 120)] += 1
                    glob_st["burst_len"][min(burst, 8)] += 1
                    glob_st["turn_chars"][min(burst_chars, 120)] += 1
                    burst = 0
                    burst_chars = 0
        per_chat[chat] = finalize(st)

    glob_st = finalize(glob_st)
    with open(os.path.join(HERE, "style_stats.json"), "w", encoding="utf-8") as f:
        json.dump({"global": glob_st, "per_chat": per_chat}, f,
                  ensure_ascii=False, indent=2)

    # 人可读报告
    L = []
    L.append("# 主人的微信说话风格（统计事实，非模型生成）\n")
    L.append("样本：我发的消息 %d 条，共 %d 字\n" % (glob_st["messages"], glob_st["chars"]))
    avg = glob_st["chars"] / max(glob_st["messages"] - glob_st["sticker"] - glob_st["image"], 1)
    L.append("## 长度")
    L.append("- 平均每条 **%.1f 字**（不含表情/图片）" % avg)
    L.append("- 单字/双字回复：**%d 条（%.1f%%）**"
             % (glob_st["short"], 100.0 * glob_st["short"] / max(glob_st["messages"], 1)))
    L.append("- 表情包：%d 条 ｜ 图片：%d 条" % (glob_st["sticker"], glob_st["image"]))
    bl = glob_st["burst_len"]
    tot_b = sum(bl.values())
    multi = sum(c for k, c in bl.items() if int(k) > 1)
    L.append("\n## 连发习惯（最关键）")
    L.append("- 一轮回复里发 **1 条**：%d 次" % bl.get("1", 0) if "1" in bl else "- 一轮 1 条：0 次")
    L.append("- 一轮发 **多条**：%d 次（%.1f%%）" % (multi, 100.0 * multi / max(tot_b, 1)))
    L.append("- 每轮条数分布：%s" % json.dumps(bl, ensure_ascii=False))
    L.append("\n## 标点习惯（出现该标点的消息条数）")
    for k, v in sorted(glob_st["punct"].items(), key=lambda x: -x[1]):
        L.append("- %s：%d 条（%.1f%%）"
                 % (k, v, 100.0 * v / max(glob_st["messages"], 1)))
    L.append("\n## 常见开头两字")
    L.append("- " + "、".join("%s(%d)" % (s, c) for s, c in glob_st["starters"][:15]))
    L.append("\n## 常见结尾两字")
    L.append("- " + "、".join("%s(%d)" % (s, c) for s, c in glob_st["enders"][:15]))
    L.append("\n## 高频短语（纯中文 2-4 字，出现≥5 次）")
    L.append("- " + "、".join("%s(%d)" % (g, c) for g, c in glob_st["ngrams"][:40]))
    L.append("\n## 口头禅（完全重复的原句，≥5 次）")
    for t, c in glob_st["catchphrases"][:25]:
        L.append("- `%s` × %d" % (t, c))
    L.append("\n## 各会话差异（语域）")
    for chat, st in per_chat.items():
        n = max(st["messages"], 1)
        L.append("- **%s**：%d 条 ｜ 单字 %.0f%% ｜ 表情 %d ｜ 句号 %d ｜ 感叹 %d"
                 % (chat, st["messages"], 100.0 * st["short"] / n,
                    st["sticker"], st["punct"].get("句号。", 0),
                    st["punct"].get("感叹号！", 0)))
    with open(os.path.join(HERE, "style_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")

    print("已写出 corpus/style_stats.json 与 corpus/style_report.md")
    print()
    print("\n".join(L[:34]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
