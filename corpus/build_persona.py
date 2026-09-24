# -*- coding: utf-8 -*-
"""build_persona.py —— 组装最终人设包（确定性规则 + 真实例句 + 联系人档案）

设计原则（2026-09-24 教训）：
  · **风格规则由统计数字机械生成** —— 小模型会跟数字打架，绝不能让它写规则
  · 本地模型只写它不会写错的部分（关系/话题/语气）
  · 例句一律**原样引用**真实聊天，不改写

产出（persona/ 目录）：
  style_rules.md        硬性风格规则（确定性，带真实数字）
  examples.jsonl        精选真实例句（多样采样，供检索/少样本）
  contacts/<会话>.md    联系人档案 = 程序写的硬事实头 + 本地模型写的定性部分
  PROMPT.md             给云端模型的最终提示词模板（人可读）
"""
from __future__ import annotations

import glob
import json
import os
import random
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "persona")
PROFILES = os.path.join(HERE, "profiles")


def load_stats():
    with open(os.path.join(HERE, "style_stats.json"), encoding="utf-8") as f:
        return json.load(f)


def load_pairs():
    ps = []
    with open(os.path.join(HERE, "pairs.jsonl"), encoding="utf-8") as f:
        for line in f:
            if line.strip():
                ps.append(json.loads(line))
    return ps


def pct(a, b):
    return 100.0 * a / max(b, 1)


def build_rules(g):
    """完全由数字生成的硬规则。"""
    total = g["messages"]
    text_msgs = total - g["sticker"] - g["image"]
    avg = g["chars"] / max(text_msgs, 1)
    bl = {int(k): v for k, v in g["burst_len"].items()}
    tot_burst = sum(bl.values())
    multi = sum(v for k, v in bl.items() if k > 1)
    p = g["punct"]
    L = []
    L.append("# 说话风格硬性规则（由真实统计生成，非模型编写）\n")
    L.append("> 依据主人 %d 条真实微信发言（%d 字）。**这些是硬事实，模仿时必须遵守。**\n"
             % (total, g["chars"]))

    L.append("## 1. 长度：单条短，但**一轮不是两个字**")
    L.append("- 单条消息平均 **%.1f 个字**。" % avg)
    L.append("- **%.1f%%** 的消息只有 1-2 个字（如 `嗯`/`行`/`哦`/`？`/`6`）。"
             % pct(g["short"], total))
    tc_avg = g.get("turn_chars_avg")
    tc_med = g.get("turn_chars_median")
    tc_p90 = g.get("turn_chars_p90")
    if tc_avg:
        L.append("- ⚠️ **一轮回复的合计字数**：平均 **%.0f 字**，中位数 %s 字，90%% 的情况不超过 %s 字。"
                 % (tc_avg, tc_med, tc_p90))
        L.append("- **所以：不要只回两三个字就完事**。对方说得越多，你要回的也越多。"
                 "合计落在 %d~%d 字最常见。" % (max(int(tc_med or 6), 4), int(tc_p90 or 30)))
    L.append("- 超过 40 字的一轮回复很少见 —— 不要写成一段长文。")

    L.append("\n## 2. 节奏：多数时候连发多条（最关键）")
    L.append("- **%.1f%%** 的回复是**连发多条短消息**，不是一条长消息。" % pct(multi, tot_burst))
    L.append("- 每轮条数分布：%s"
             % "、".join("%d条×%d次" % (k, v) for k, v in sorted(bl.items())))
    dist = "、".join("%d条占%.0f%%" % (k, pct(v, tot_burst)) for k, v in sorted(bl.items()))
    L.append("- **真实分布**：%s" % dist)
    L.append("- **怎么决定发几条**：看对方说了多少 ——")
    L.append("  · 对方只发一句短的 → 回 **1 条**（最常见）")
    L.append("  · 对方连发几条 / 一次问了几件事 → 回 **2-4 条**，一件事一条地答")
    L.append("  · 对方发了一长段 → 回 2-3 条，抓重点说，不要逐句复述")
    L.append("- ⚠️ 不要为了凑条数硬拆；也不要该答三件事却只回两个字。")
    L.append("- ⚠️ **不要编造**上下文里没有的具体信息（人名、时间、金额、约定）。"
             "不确定就含糊过去或反问，绝不虚构。")

    L.append("\n## 3. 标点：几乎不用（最容易露馅的地方）")
    L.append("- 句号 `。`：只有 %.1f%% 的消息有 ｜ 感叹号 `！`：%.1f%% ｜ 波浪号 `～`：%.1f%% ｜ 省略号 `…`：%.1f%%"
             % (pct(p.get("句号。", 0), total), pct(p.get("感叹号！", 0), total),
                pct(p.get("波浪号～", 0), total), pct(p.get("省略号…", 0), total)))
    L.append("- 逗号 `，`：%.1f%% ｜ 问号 `？`：%.1f%%"
             % (pct(p.get("逗号，", 0), total), pct(p.get("问号？", 0), total)))
    L.append("- **硬性：句尾不加句号，不用感叹号，不用波浪号。** 一句说完就直接结束。")
    L.append("- 问号可以用（他常用单独一个 `？` 表示疑惑/反问）。")

    L.append("\n## 4. 表情与图片")
    L.append("- 表情包 %d 次、图片 %d 次（合计占 %.1f%%）—— 会用，但不是每条都用。"
             % (g["sticker"], g["image"], pct(g["sticker"] + g["image"], total)))

    L.append("\n## 5. 口头禅（原句，出现频次）")
    L.append("- " + "、".join("`%s`×%d" % (t, c) for t, c in g["catchphrases"][:20]))

    L.append("\n## 6. 高频用词")
    L.append("- " + "、".join("%s(%d)" % (t, c) for t, c in g["ngrams"][:30]))

    L.append("\n## 7. 绝对不要做的事")
    L.append("- ❌ 不要写解释性、总结性、客套的长句（「我觉得这件事…」「希望对你有帮助」）")
    L.append("- ❌ 不要用书面语连接词（「因此」「此外」「总的来说」）")
    L.append("- ❌ 不要在句尾加句号，不要用感叹号表达情绪")
    L.append("- ❌ 不要一次发一大段；不要用列表/编号/emoji 装饰")
    L.append("- ❌ 不要重复对方的话，也不要问「还有什么可以帮你的」")
    L.append("- ❌ **不要替主人做约定**：不许出现时间、地点、「一起」「几点」「没问题」"
             "「我帮你」「约」这类把事定死的话；对方提出约定就含糊带过或岔开话题。")

    L.append("\n## 8. 口头禅要自然，别每条都塞")
    L.append("- 口头禅（%s 等）在真人语料里只出现在约 **%.0f%%** 的回复中。"
             % ("`？`/`行`/`OK`/`66`/`神了`/`啥`",
                pct(sum(c for _, c in g["catchphrases"]), total)))
    L.append("- **硬性**：大约**每两条回复里才有一条**带口头禅（真人就是 50% 左右），"
             "一条回复里最多 1 个。连着几次都用同一个（每次都 `？` 或每次都 `行`）会立刻露馅。")
    L.append("- ⚠️ **更重要的：不要用「行」「看情况」「再说吧」这类空词敷衍**。"
             "真人的回复里有**具体内容**（在干嘛、谁怎么样、什么事）。"
             "没内容可说的宁可回短但有实义的话，也不要纯语气词堆砌。")

    # ---- 数据驱动：对方说几条 → 你回几条 / 多少字 ----
    try:
        with open(os.path.join(HERE, "pairs_stats.json"), encoding="utf-8") as f:
            rm = (json.load(f) or {}).get("response_map") or {}
        if rm:
            L.append("\n## 9. 对方说几条 → 你回几条（真实统计，最该照着做）")
            L.append("| 对方发了 | 你最常回 | **平均回** | 平均合计字数 | 样本 |")
            L.append("|---|---|---|---|---|")
            for k in sorted(rm, key=lambda x: int(x)):
                v = rm[k]
                L.append("| %s 条 | %d 条 | **%.1f 条** | %.1f 字 | %d 次 |"
                         % (k if k != "4" else "4+", v["lines_mode"],
                            v.get("lines_avg", v["lines_mode"]),
                            v["chars_avg"], v["samples"]))
            L.append("\n**怎么用**：一轮**平均 2.4 条**（1 条占 38%、2 条占 28%、3 条 16%、"
                     "4 条以上 18%）—— 所以**常常要发 2-3 条**，不是只发 1 条；"
                     "但也别硬凑，内容少就 1 条。\n"
                     "**合计字数**：一轮 **15-20 字**最常见（中位 9 字、平均 14 字）——"
                     "别只回两三个字，也别写成一段超过 40 字的。")
    except Exception:
        pass
    return "\n".join(L) + "\n"


def build_examples(pairs, n=400, exclude_keys=None):
    """多样采样：短句 / 单字 / 表情 / 连发 / 长句 各取一些，保证覆盖面。
    exclude_keys: 盲测留出集的 {(chat, ts)} —— 绝不能进人设，否则测试作弊。
    """
    random.seed(24)
    exclude_keys = exclude_keys or set()
    pairs = [p for p in pairs if (p.get("chat"), p.get("ts")) not in exclude_keys]
    buckets = {"single_char": [], "sticker": [], "burst": [], "short": [], "long": []}
    for p in pairs:
        r = (p.get("reply") or "").strip()
        if not r:
            continue
        if p["reply_type"] == "sticker":
            buckets["sticker"].append(p)
        elif len(r) <= 2:
            buckets["single_char"].append(p)
        elif "\n" in r:
            buckets["burst"].append(p)
        elif len(r) <= 12:
            buckets["short"].append(p)
        else:
            buckets["long"].append(p)
    quota = {"single_char": 60, "sticker": 30, "burst": 120, "short": 130, "long": 60}
    out = []
    for k, q in quota.items():
        lst = buckets[k]
        random.shuffle(lst)
        out.extend(lst[:q])
    random.shuffle(out)
    return out[:n]


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--exclude", default=None,
                    help="留出集文件（blindtest 生成），其中的配对不进人设例句")
    args = ap.parse_args()

    stats = load_stats()
    g = stats["global"]
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(os.path.join(OUT, "contacts"), exist_ok=True)

    rules = build_rules(g)
    with open(os.path.join(OUT, "style_rules.md"), "w", encoding="utf-8") as f:
        f.write(rules)

    pairs = load_pairs()
    excl = set()
    if args.exclude and os.path.exists(args.exclude):
        with open(args.exclude, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    d = json.loads(line)
                    excl.add((d["chat"], d["ts"]))
        print("留出集：%d 条不进入人设例句（防作弊）" % len(excl))
    ex = build_examples(pairs, exclude_keys=excl)
    with open(os.path.join(OUT, "examples.jsonl"), "w", encoding="utf-8") as f:
        for p in ex:
            f.write(json.dumps({
                "chat": p["chat"],
                "incoming": [m.get("text") for m in p["incoming"]],
                "reply": p["reply"],
                "reply_type": p["reply_type"],
            }, ensure_ascii=False) + "\n")

    # 联系人档案：程序写硬事实头 + 本地模型写的定性部分
    n_contact = 0
    for md in sorted(glob.glob(os.path.join(PROFILES, "*.md"))):
        name = os.path.basename(md)[:-3]
        st = stats["per_chat"].get(name)
        with open(md, encoding="utf-8") as f:
            body = f.read()
        # 去掉本地模型文件的标题/说明，保留正文
        body = re.sub(r"^#.*?\n", "", body, count=1)
        body = re.sub(r"^>.*?\n", "", body, count=1, flags=re.M)
        head = ["# 联系人档案：%s\n" % name]
        if st:
            tot = st["messages"]
            head.append("## 硬事实（程序统计，不可违背）")
            head.append("- 主人对这个人共发了 %d 条消息" % tot)
            head.append("- 其中单/双字回复 %d 条（%.0f%%）、表情包 %d 条、图片 %d 条"
                        % (st["short"], pct(st["short"], tot), st["sticker"], st["image"]))
            bl = {int(k): v for k, v in st["burst_len"].items()}
            multi = sum(v for k, v in bl.items() if k > 1)
            head.append("- 连发多条的比例：%.0f%%（每轮条数分布 %s）"
                        % (pct(multi, sum(bl.values())),
                           "、".join("%d条×%d" % (k, v) for k, v in sorted(bl.items()))))
            # 给模型的直接建议：这个会话一轮通常发几条
            if bl:
                mode = max(bl.items(), key=lambda x: x[1])[0]
                head.append("- **建议：对这个人一轮通常发 %d 条**（本会话最常见）" % mode)
            head.append("- 句号 %d 条 ｜ 感叹号 %d 条 ｜ 问号 %d 条 ｜ 逗号 %d 条"
                        % (st["punct"].get("句号。", 0), st["punct"].get("感叹号！", 0),
                           st["punct"].get("问号？", 0), st["punct"].get("逗号，", 0)))
            if st["catchphrases"]:
                head.append("- 对这个人的口头禅：" +
                            "、".join("`%s`×%d" % (t, c) for t, c in st["catchphrases"][:10]))
            head.append("")
        head.append("## 定性分析（本地 %s 依据真实样本蒸馏，仅供参考）\n" % "qwen2.5:7b")
        path = os.path.join(OUT, "contacts", name + ".md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(head) + body.strip() + "\n")
        n_contact += 1

    # 给云端模型的提示词模板
    tpl = (
        "# 给云端模型的提示词模板（回复时拼装）\n\n"
        "```\n"
        "你是「{联系人}」微信聊天的回复生成器。你要**模仿下面这位主人的说话方式**，"
        "替他写回复。\n\n"
        "【硬性风格规则】\n{style_rules}\n\n"
        "【对这位联系人的专属档案】\n{contact_profile}\n\n"
        "【最近对话】\n{recent}\n\n"
        "【与他类似场景下主人的真实回复（供模仿，不要照抄内容）】\n{examples}\n\n"
        "【输出要求】\n"
        "只输出主人会发的内容，每条一行，1-3 行（对应连发 1-3 条）。"
        "不要加引号、不要解释、不要编号。若判断这次不该回，只输出一个空行。\n"
        "```\n"
    )
    with open(os.path.join(OUT, "PROMPT.md"), "w", encoding="utf-8") as f:
        f.write(tpl)

    print("人设包已生成：corpus/persona/")
    print("  style_rules.md      %d 字（确定性规则）" % len(rules))
    print("  examples.jsonl      %d 组精选例句" % len(ex))
    print("  contacts/*.md       %d 份联系人档案" % n_contact)
    print("  PROMPT.md           云端提示词模板")
    print()
    print(rules[:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
