# -*- coding: utf-8 -*-
"""blindjudge.py —— 给盲测结果当裁判（本地 + 云端两种）

读 corpus/blindtest/cases.jsonl（已生成的 A/B），让裁判判断哪条是真人写的。
  · 正确率 ≈50% → **分不出来 = 像**
  · 越接近 100% → 越容易被识破

用法：
  python corpus/blindjudge.py --engine local
  python corpus/blindjudge.py --engine cloud      # 用 DeepSeek 当裁判（更强）
  python corpus/blindjudge.py --engine both
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

JUDGE_SYS = (
    "你是微信聊天辨别专家。给你一段聊天上下文和两条候选回复（A、B）："
    "其中一条是这个人**真实发出的**，另一条是 AI 模仿他写的。"
    "请判断哪一条更像真人写的。\n"
    "只输出一个字符：A 或 B（若实在一样像，输出 =）。不要输出任何其他内容。"
)


def _pick(text: str) -> str:
    for ch in (text or "").strip().upper():
        if ch in "AB=":
            return ch
    return "?"


def judge_local(ctx, a, b) -> str:
    q = ("【上下文】\n%s\n\n【A】%s\n\n【B】%s\n\n哪条是真人写的？只答 A 或 B。"
         % (ctx, a, b))
    payload = {
        "model": "qwen2.5:7b",
        "messages": [{"role": "system", "content": JUDGE_SYS},
                     {"role": "user", "content": q}],
        "stream": False,
        "options": {"temperature": 0.0, "num_ctx": 4096},
    }
    req = urllib.request.Request("http://127.0.0.1:11434/api/chat",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=300) as resp:
        txt = (json.loads(resp.read().decode("utf-8")).get("message") or {}).get("content", "")
    return _pick(txt)


def judge_cloud(cfg, ctx, a, b) -> str:
    import reply as reply_mod
    key = reply_mod._api_key(cfg, ROOT)
    if not key:
        return "?"
    r = cfg.get("reply") or {}
    payload = {
        "model": r.get("model", "deepseek-chat"),
        "messages": [{"role": "system", "content": JUDGE_SYS},
                     {"role": "user", "content":
                      "【上下文】\n%s\n\n【A】%s\n\n【B】%s\n\n哪条是真人写的？只答 A 或 B。"
                      % (ctx, a, b)}],
        "temperature": 0.0, "max_tokens": 8, "stream": False,
    }
    req = urllib.request.Request(
        r.get("endpoint", "https://api.deepseek.com/v1/chat/completions"),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _pick((data["choices"][0]["message"]["content"] or ""))
    except Exception:
        return "?"


def run(engine, cases, cfg, tag=""):
    random.seed(99)
    rows, right = [], 0
    print("=== 裁判：%s ===" % engine)
    for i, c in enumerate(cases, 1):
        flip = random.random() < 0.5
        a, b = (c["real"], c["model"]) if flip else (c["model"], c["real"])
        real_is = "A" if flip else "B"
        ctx = "\n".join("对方：%s" % (x or "") for x in c["incoming"])
        pick = judge_local(ctx, a, b) if engine == "local" else judge_cloud(cfg, ctx, a, b)
        ok = (pick == real_is)
        right += ok
        rows.append({"i": i, "pick": pick, "real_is": real_is, "ok": ok, "chat": c["chat"]})
        print("   [%2d] 裁判→%s ｜ 真人=%s ｜ %s" % (i, pick, real_is, "✅" if ok else "❌"))
    acc = 100.0 * right / max(len(cases), 1)
    print("   正确率：%.0f%%（%d/%d）" % (acc, right, len(cases)))
    print("   %s" % ("≈50% → 分不出来（成功）" if 35 <= acc <= 65
                     else "偏高 → 容易被识破" if acc > 65 else "偏低 → 裁判在乱猜/反向"))
    return {"engine": engine, "acc": acc, "rows": rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="both", choices=("local", "cloud", "both"))
    ap.add_argument("--cases-file", default=os.path.join(HERE, "blindtest", "cases.jsonl"))
    args = ap.parse_args()

    cases = [json.loads(l) for l in open(args.cases_file, encoding="utf-8") if l.strip()]
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    out = []
    if args.engine in ("local", "both"):
        out.append(run("local", cases, cfg))
    if args.engine in ("cloud", "both"):
        out.append(run("cloud", cases, cfg))
    with open(os.path.join(HERE, "blindtest", "judge.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
