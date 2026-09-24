# -*- coding: utf-8 -*-
"""blindtest.py —— P3 盲测：模型写的 vs 主人真写的，能不能分辨出来？

流程（严格防作弊）：
  1. 从每个会话**最近的**配对里留出一部分（holdout），写入 blindtest/holdout.jsonl
  2. 重建人设包时把这些排除掉（build_persona --exclude）→ 模型没见过这些
  3. 对每个测试用例，用「真实回复之前的上下文」让模型生成回复
  4. 客观指标：长度/连发条数/标点/口头禅 与真实回复对比
  5. 盲评：本地模型当裁判，只看到 A/B 两条，判断哪条是真人写的
     · 裁判正确率 ≈50% → **分不出来**（说明像）
     · 越接近 100% → 越容易被识破
  6. 输出 A/B 试卷（打乱顺序）+ 答案，供主人亲自盲评

用法：
  python corpus/blindtest.py --holdout 3 --cases 18
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import memory  # noqa: E402
from memory_build import ollama  # noqa: E402

TURNS = os.path.join(HERE, "turns")
OUTDIR = os.path.join(HERE, "blindtest")
HOLDOUT = os.path.join(OUTDIR, "holdout.jsonl")
PUNCT = "。！？～…，、"

JUDGE_SYS = (
    "你是微信聊天记录的辨别专家。下面给你一段聊天上下文，以及**两条候选回复**"
    "（A 和 B）。其中一条是这个人**真实发出的**，另一条是 AI 模仿他写的。"
    "请判断哪一条更像真人写的。\n"
    "只输出一个字符：A 或 B 或 =（两条一样像）。不要解释。"
)


def load_turns():
    out = {}
    for fp in sorted(os.listdir(TURNS)):
        if fp.endswith(".jsonl"):
            chat = fp[:-6]
            out[chat] = [json.loads(l) for l in
                         open(os.path.join(TURNS, fp), encoding="utf-8") if l.strip()]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", type=int, default=3, help="每个会话留出多少条（取最近的）")
    ap.add_argument("--cases", type=int, default=18, help="实际测试多少条")
    ap.add_argument("--judge", default="local", choices=("local", "none"))
    args = ap.parse_args()

    os.makedirs(OUTDIR, exist_ok=True)
    pairs = [json.loads(l) for l in open(os.path.join(HERE, "pairs.jsonl"),
                                         encoding="utf-8") if l.strip()]
    by_chat = {}
    for p in pairs:
        by_chat.setdefault(p["chat"], []).append(p)

    # ---- 1. 留出集：每会话随机抽 N 条（8/1 之后），避免只抽到"最近的长回复" ----
    holdout = []
    for chat, ps in by_chat.items():
        pool = [p for p in ps if (p.get("reply") or "").strip()
                and p["time"] >= "2026-08-01"]
        random.seed(hash(chat) & 0xFFFF)
        random.shuffle(pool)
        holdout.extend(pool[:args.holdout])
    with open(HOLDOUT, "w", encoding="utf-8") as f:
        for p in holdout:
            f.write(json.dumps({"chat": p["chat"], "ts": p["ts"]}, ensure_ascii=False) + "\n")
    print("留出集：%d 条（%d 个会话）→ %s" % (len(holdout), len(by_chat), HOLDOUT))

    # ---- 2. 重建人设包（排除留出集）----
    import subprocess
    py = sys.executable
    r = subprocess.run([py, os.path.join(HERE, "build_persona.py"), "--exclude", HOLDOUT],
                       capture_output=True, text=True, encoding="utf-8")
    print("重建人设包：", (r.stdout or "").strip().splitlines()[0] if r.stdout else r.stderr[:80])

    # ---- 3. 生成 ----
    import reply as reply_mod
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    turns = load_turns()
    random.seed(2026)
    random.shuffle(holdout)
    cases = []
    for p in holdout[:args.cases]:
        chat, ts = p["chat"], p["ts"]
        msgs = turns.get(chat, [])
        hist = []
        for m in msgs:
            if m["ts"] >= ts:
                break
            hist.append({"who": "me" if m["who"] == "me" else "other",
                         "text": m.get("text") or ""})
        if len(hist) < 3:
            continue
        t0 = time.time()
        lines, info = reply_mod.generate_burst(cfg, chat, hist[-10:])
        cases.append({
            "chat": chat, "ts": ts, "time": p["time"],
            "incoming": [m.get("text") for m in p["incoming"]],
            "real": p["reply"], "real_lines": len((p["reply"] or "").split("\n")),
            "model": "\n".join(lines), "model_lines": len(lines),
            "tokens": (info or {}).get("tokens") if isinstance(info, dict) else None,
            "gen_sec": round(time.time() - t0, 1),
        })
        print("  [%d] %s %s → 生成 %d 条" % (len(cases), chat[:8], p["time"][5:16], len(lines)))

    # ---- 4. 客观指标 ----
    def stats_of(texts):
        n = len(texts)
        chars = [len(t) for t in texts]
        punct = sum(1 for t in texts for c in t if c in PUNCT)
        return {"n": n, "avg_len": round(sum(chars) / max(n, 1), 1),
                "punct_per_msg": round(punct / max(n, 1), 2)}

    real_st = stats_of([c["real"] for c in cases])
    model_st = stats_of([c["model"] for c in cases])
    burst_real = stats_of([str(c["real_lines"]) for c in cases])
    same_burst = sum(1 for c in cases if c["model_lines"] == c["real_lines"])

    # 口头禅命中率
    catch = ["？", "行", "OK", "66", "666", "来", "神了", "啥", "等下", "哦", "没", "知道"]
    def catch_rate(texts):
        hit = sum(1 for t in texts if any(x in t for x in catch))
        return round(100.0 * hit / max(len(texts), 1), 1)

    # ---- 5. 盲评交给 blindjudge.py（本脚本只负责生成与客观指标）----
    verdicts = []
    acc = None

    # ---- 6. 输出 ----
    with open(os.path.join(OUTDIR, "cases.jsonl"), "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    # 给主人的盲评试卷（打乱 A/B，答案另存）
    random.seed(7)
    sheet, key = [], []
    for i, c in enumerate(cases, 1):
        flip = random.random() < 0.5
        a, b = (c["real"], c["model"]) if flip else (c["model"], c["real"])
        sheet.append("### %d. 【%s】对方说：%s\n\n- **A**：%s\n- **B**：%s\n"
                     % (i, c["chat"], " / ".join(x or "" for x in c["incoming"])[:60],
                        a.replace("\n", "  ⏎  "), b.replace("\n", "  ⏎  ")))
        key.append("%d. 真人写的是 **%s**" % (i, "A" if flip else "B"))
    with open(os.path.join(OUTDIR, "ab_sheet.md"), "w", encoding="utf-8") as f:
        f.write("# 盲评试卷（主人亲自判）\n\n> 每题两条回复，一条是主人**真实发出的**，"
                "一条是 AI 模仿的。猜猜哪条是真人？答案在同目录 `ab_key.md`。\n\n"
                + "\n".join(sheet))
    with open(os.path.join(OUTDIR, "ab_key.md"), "w", encoding="utf-8") as f:
        f.write("# 答案\n\n" + "\n".join(key) + "\n")

    report = []
    report.append("# P3 盲测报告\n")
    report.append("测试用例：%d 条 ｜ 留出集 %d 条（已从人设例句中排除）\n" % (len(cases), len(holdout)))
    report.append("## 客观指标（模型 vs 真人）\n")
    report.append("| 指标 | 真人 | 模型 | 说明 |")
    report.append("|---|---|---|---|")
    report.append("| 平均每条字数 | %.1f | %.1f | 越接近越好 |"
                  % (real_st["avg_len"], model_st["avg_len"]))
    report.append("| 标点密度（个/条） | %.2f | %.2f | 主人几乎不用标点 |"
                  % (real_st["punct_per_msg"], model_st["punct_per_msg"]))
    report.append("| 连发条数与真人一致 | — | %d/%d (%.0f%%) | 越高越好 |"
                  % (same_burst, len(cases), 100.0 * same_burst / max(len(cases), 1)))
    report.append("| 含口头禅的比例 | %.1f%% | %.1f%% | 越接近越好 |"
                  % (catch_rate([c["real"] for c in cases]),
                     catch_rate([c["model"] for c in cases])))
    if acc is not None:
        report.append("\n## 盲评结果（本地 qwen2.5:7b 当裁判）\n")
        report.append("裁判正确识别出「真人写的」的比例：**%.0f%%**（%d/%d）\n"
                      % (acc, sum(verdicts), len(verdicts)))
        report.append("判读：**50%% 左右 = 分不出来（成功）**；越高越容易被识破。\n")
    report.append("\n## 逐条明细\n")
    for i, c in enumerate(cases, 1):
        report.append("**%d. [%s] %s**" % (i, c["chat"], c["time"]))
        report.append("- 对方：%s" % " / ".join(x or "" for x in c["incoming"])[:60])
        report.append("- 真人：`%s`" % c["real"].replace("\n", "` ⏎ `"))
        report.append("- 模型：`%s`" % c["model"].replace("\n", "` ⏎ `"))
        if c.get("judge_pick"):
            report.append("- 裁判：选 %s（真人实为 %s）%s"
                          % (c["judge_pick"], c["_real_is"],
                             "✅" if c["judge_right"] else "❌"))
        report.append("")
    with open(os.path.join(OUTDIR, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(report))

    print("\n" + "\n".join(report[:16]))
    print("\n输出目录：corpus/blindtest/")
    print("  report.md    完整报告")
    print("  ab_sheet.md  盲评试卷（主人亲自判）")
    print("  ab_key.md    答案")
    return 0


if __name__ == "__main__":
    sys.exit(main())
