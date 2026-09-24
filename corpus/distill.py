# -*- coding: utf-8 -*-
"""distill.py —— P1 本地模型蒸馏：把语料变成「人设 + 语域档案」

用本机 Ollama（qwen2.5:7b）**离线**处理，聊天内容不出门。

输入：corpus/pairs.jsonl（真实配对）、corpus/style_stats.json（统计）、corpus/turns/
输出：
  corpus/profiles/<会话>.md   每个联系人的语域档案（L4）
  corpus/style_profile.md     主人总体说话风格（L3 规范，统计+模型润色）

用法：
  python corpus/distill.py --chat 好友C
  python corpus/distill.py --all
  python corpus/distill.py --global-only
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OLLAMA = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen2.5:7b"
PROFILES = os.path.join(HERE, "profiles")


def ollama_chat(system: str, user: str, temperature=0.3, num_ctx=8192) -> str:
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    req = urllib.request.Request(
        OLLAMA, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=900) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("message") or {}).get("content", "").strip()


def load_pairs():
    out = {}
    with open(os.path.join(HERE, "pairs.jsonl"), encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            p = json.loads(line)
            out.setdefault(p["chat"], []).append(p)
    return out


def sample_for_chat(pairs, n=70):
    """挑有代表性的样本：优先内容较多、又不全是很长的"""
    ps = [p for p in pairs if (p.get("reply") or "").strip()]
    random.seed(11)
    good = [p for p in ps if 2 <= len(p["reply"]) <= 60]
    random.shuffle(good)
    return good[:n]


def fmt_pairs(ps):
    lines = []
    for p in ps:
        inc = " ⏎ ".join((m.get("text") or "")[:60] for m in p["incoming"][-3:])
        rep = (p.get("reply") or "").replace("\n", " ⏎ ")[:90]
        lines.append("对方：%s\n主人：%s" % (inc, rep))
    return "\n---\n".join(lines)


def fmt_stats(st):
    return (
        "主人共发了 %d 条消息；单字/双字回复 %d 条；表情包 %d 条；图片 %d 条。\n"
        "每轮连发条数分布：%s\n"
        "标点使用（条数）：%s\n"
        "高频短语：%s\n"
        "口头禅（原句重复）：%s\n"
    ) % (
        st["messages"], st["short"], st["sticker"], st["image"],
        json.dumps(st["burst_len"], ensure_ascii=False),
        json.dumps({k: v for k, v in list(st["punct"].items())}, ensure_ascii=False),
        "、".join("%s(%d)" % (g, c) for g, c in st["ngrams"][:30]),
        "、".join("`%s`×%d" % (t, c) for t, c in st["catchphrases"][:20]),
    )


SYS_PROFILE = (
    "你是一个语料分析助手，材料是某人在微信上与一位联系人的真实对话样本"
    "（`对方：…` / `主人：…`）以及统计数字。**只依据材料**，不要编造。\n"
    "【硬性要求】\n"
    "1. 统计数字是**硬事实**：你的每条描述都必须与之一致；凡与数字矛盾的（例如明明几乎不用"
    "句号却写「常用标点」）一律不许写。\n"
    "2. 必须**原样引用至少 5 条主人的真实句子**（用反引号包起来），不要改写。\n"
    "3. 不要写「友好」「亲密」这类空泛评价，只写**可执行的具体做法**。\n"
    "4. 不要提「%s」以外的联系人，也不要推测材料外的信息。\n"
    "输出简体中文 Markdown，500 字以内，结构固定为：\n"
    "## 关系与场景\n## 主人对他/她的语气（具体做法）\n## 常见话题\n"
    "## 说话习惯（必须引用统计数字：平均字数/连发比例/标点/表情）\n"
    "## 专属说法（原句引用）\n## 硬性规则（模仿时必须遵守的 5 条）"
)

SYS_GLOBAL = (
    "你是一个风格分析助手，材料是某人在微信上真实说话的**统计数字**与**真实例句**。\n"
    "【硬性要求】\n"
    "1. 统计数字是硬事实，每条描述都要与数字一致，矛盾的不许写。\n"
    "2. 必须原样引用至少 8 条主人的真实句子（反引号包裹）。\n"
    "3. 不写空泛形容词，只写给另一个 AI 能照着执行的规则。\n"
    "输出简体中文 Markdown，700 字以内，结构固定为：\n"
    "## 一句话概括\n## 长度与节奏（带数字）\n## 标点与表情（带数字）\n"
    "## 口头禅与常用词（原句引用）\n## 情绪表达方式\n## 硬性规则（模仿时必须遵守的 8 条）"
)


def do_chat(chat, pairs_map, stats):
    ps = sample_for_chat(pairs_map.get(chat, []))
    if len(ps) < 5:
        print("  样本太少，跳过：%s" % chat)
        return None
    st = stats["per_chat"].get(chat) or stats["global"]
    user = ("【联系人】%s\n\n【统计】\n%s\n\n【真实对话样本（共 %d 组）】\n%s\n"
            % (chat, fmt_stats(st), len(ps), fmt_pairs(ps)))
    out = ollama_chat(SYS_PROFILE % chat, user)
    os.makedirs(PROFILES, exist_ok=True)
    path = os.path.join(PROFILES, chat.replace("/", "_") + ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 语域档案：%s\n\n> 由本地 %s 依据 %d 组真实对话蒸馏，仅供参考与人工校订。\n\n%s\n"
                % (chat, MODEL, len(ps), out))
    print("  ✅ %s（%d 组样本，%d 字）" % (chat, len(ps), len(out)))
    return path


def do_global(stats):
    g = stats["global"]
    examples = []
    with open(os.path.join(HERE, "pairs.jsonl"), encoding="utf-8") as f:
        for line in f:
            p = json.loads(line)
            if 2 <= len((p.get("reply") or "")) <= 40:
                examples.append(p)
    random.seed(3)
    random.shuffle(examples)
    user = ("【统计】\n%s\n\n【真实例句（对方 → 主人）】\n%s\n"
            % (fmt_stats(g), fmt_pairs(examples[:60])))
    out = ollama_chat(SYS_GLOBAL, user, temperature=0.25)
    path = os.path.join(HERE, "style_profile.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 主人的说话风格规范（本地模型蒸馏）\n\n"
                "> 由本地 %s 依据 %d 条主人真实发言蒸馏。统计底稿见 style_report.md。\n\n%s\n"
                % (MODEL, g["messages"], out))
    print("  ✅ 总风格规范：%d 字" % len(out))
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--global-only", action="store_true")
    args = ap.parse_args()

    with open(os.path.join(HERE, "style_stats.json"), encoding="utf-8") as f:
        stats = json.load(f)
    pairs_map = load_pairs()
    print("样本会话 %d 个 ｜ 配对总数 %d" % (len(pairs_map), sum(len(v) for v in pairs_map.values())))

    if args.global_only:
        do_global(stats)
        return 0
    if args.chat:
        do_chat(args.chat, pairs_map, stats)
        return 0
    if args.all:
        do_global(stats)
        for chat in pairs_map:
            try:
                do_chat(chat, pairs_map, stats)
            except Exception as e:
                print("  ❌ %s 失败：%s: %s" % (chat, type(e).__name__, e))
        return 0
    print("用法：--chat 名字 / --all / --global-only")
    return 1


if __name__ == "__main__":
    sys.exit(main())
