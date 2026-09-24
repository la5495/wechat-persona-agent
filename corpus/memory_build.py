# -*- coding: utf-8 -*-
"""memory_build.py —— 后台跑 L1/L2：会话摘要 + 长期事实（全本地，零 API 费）

流程：读 turns/*.jsonl → 切片段(episode) → 本地 qwen2.5:7b 总结+抽事实 → 写入 memory.db
特点：
  · **断点续跑**：每段处理完就落库 + 记水位，随时 Ctrl+C / 关机都不丢进度
  · **幂等**：同一段不会重复总结（按 chat+start_ts 判重）
  · **可试跑**：--limit N 先跑几段看质量

用法：
  python corpus/memory_build.py --limit 5          # 试跑 5 段
  python corpus/memory_build.py --chat 好友C       # 只跑某个会话
  python corpus/memory_build.py                    # 全量（后台挂着跑）
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import memory  # noqa: E402

TURNS = os.path.join(HERE, "turns")
OLLAMA = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen2.5:7b"
GAP_SEC = 45 * 60          # 超过 45 分钟算新片段
MAX_MSGS = 60              # 一段最多多少条消息
MAX_CHARS = 1400           # 一段最多多少字（控制 prompt 大小）

SYS = (
    "你是一个聊天记录整理助手。你会看到一段真实的微信聊天记录。"
    "请**只依据记录内容**输出严格 JSON（不要任何解释、不要 markdown 代码块），格式：\n"
    '{"topic":"这段在聊什么（6字以内，如 约游戏/工作安排/闲聊）",'
    '"summary":"2-3 句中文摘要，写清谁说了什么、有没有约定或结论",'
    '"importance":1到5的整数（5=重要约定或重要信息，1=纯闲聊）,'
    '"facts":[{"predicate":"属性名，如 工作/喜好/关系/约定","object":"具体内容",'
    '"statement":"一句完整的第三人称事实，主语用「主人」"}]}\n'
    "要求：\n"
    "1. facts 只写**明确、稳定、可复用**的信息（如主人做保险、主人在学 AI、"
    "主人答应周五前给答复、某人是同事）。**不要**写一次性的琐事和猜测。\n"
    "2. facts 没有就给空数组 []。宁少勿滥。\n"
    "3. summary 里不要出现「这段记录」这类元话术，直接说内容。"
)


def ollama(prompt: str, temperature=0.2, num_ctx=6144) -> str:
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYS},
                     {"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    req = urllib.request.Request(
        OLLAMA, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=600) as resp:
        return (json.loads(resp.read().decode("utf-8")).get("message") or {}).get("content", "")


def parse_json(txt: str) -> dict:
    """模型偶尔会加壳，宽容地抠出第一个 JSON 对象。"""
    txt = txt.strip()
    txt = re.sub(r"^```(json)?|```$", "", txt, flags=re.M).strip()
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        return json.loads(txt[i:j + 1])
    except Exception:
        return {}


def episodes_of(msgs):
    """把消息切成片段。"""
    eps, cur = [], []
    for m in msgs:
        if cur:
            gap = m["ts"] - cur[-1]["ts"]
            chars = sum(len(x.get("text") or "") for x in cur)
            if gap > GAP_SEC or len(cur) >= MAX_MSGS or chars >= MAX_CHARS:
                eps.append(cur)
                cur = []
        cur.append(m)
    if cur:
        eps.append(cur)
    return eps


def transcript(ep):
    out = []
    for m in ep:
        t = time.strftime("%H:%M", time.localtime(m["ts"]))
        who = "我" if m["who"] == "me" else (m.get("sender") or "对方")
        out.append("%s %s：%s" % (t, who, (m.get("text") or "").strip()))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理这么多段（试跑）")
    ap.add_argument("--chat", default=None, help="只处理这个会话")
    ap.add_argument("--min-msgs", type=int, default=6, help="少于这么多条的段跳过")
    args = ap.parse_args()

    con = memory.init()
    n_contact = memory.sync_contacts(con, os.path.join(HERE, "persona", "contacts"))
    print("联系人同步 %d 条 ｜ 库：%s" % (n_contact, memory.DB))
    print("模型：%s（本地，零 API 费）\n" % MODEL)

    files = sorted(glob.glob(os.path.join(TURNS, "*.jsonl")))
    if args.chat:
        files = [f for f in files if os.path.basename(f)[:-6] == args.chat]
    if not files:
        print("没有找到语料文件")
        return 2

    done_total = ep_total = 0
    t_start = time.time()
    for fp in files:
        chat = os.path.basename(fp)[:-6]
        msgs = [json.loads(l) for l in open(fp, encoding="utf-8") if l.strip()]
        eps = [e for e in episodes_of(msgs) if len(e) >= args.min_msgs]
        print("【%s】%d 条消息 → %d 段" % (chat, len(msgs), len(eps)))
        for idx, ep in enumerate(eps, 1):
            if args.limit and done_total >= args.limit:
                break
            start_ts = ep[0]["ts"]
            if memory.already_done_range(con, chat, start_ts):
                continue
            body = transcript(ep)
            t0 = time.time()
            try:
                raw = ollama("【会话】%s\n【记录】\n%s\n" % (chat, body))
                data = parse_json(raw)
            except Exception as e:
                print("   ⚠️ 第 %d 段失败（%s），跳过" % (idx, type(e).__name__))
                continue
            if not data.get("summary"):
                print("   ⚠️ 第 %d 段没解析出摘要，跳过（原文前 60 字：%s）"
                      % (idx, body[:60].replace("\n", " ")))
                continue
            try:
                n_mine = sum(1 for m in ep if m["who"] == "me")
                try:
                    imp = int(data.get("importance", 2) or 2)
                except (TypeError, ValueError):
                    imp = 2
                eid = memory.add_episode(con, chat, start_ts, ep[-1]["ts"],
                                         str(data.get("topic") or ""),
                                         str(data.get("summary") or ""),
                                         imp, len(ep), n_mine, MODEL)
                n_fact = 0
                for f in (data.get("facts") or []):
                    # 模型偶尔把 fact 写成数组/字符串，一律宽容处理
                    if isinstance(f, dict):
                        st = str(f.get("statement") or "").strip()
                        pred = str(f.get("predicate") or "").strip()
                        obj = str(f.get("object") or "").strip()
                    elif isinstance(f, (list, tuple)) and len(f) >= 3:
                        pred, obj, st = (str(f[0]).strip(), str(f[1]).strip(),
                                         str(f[2]).strip())
                    elif isinstance(f, str):
                        st, pred, obj = f.strip(), "", ""
                    else:
                        continue
                    if len(st) < 6:
                        continue
                    st = memory.normalize_statement(st)
                    if memory.similar_fact_exists(con, st):
                        continue          # 同一条事实不重复入库
                    memory.add_fact(con, st, pred, obj, chat, start_ts, 0.7, eid)
                    n_fact += 1
                memory.set_ingest(con, chat, ep[-1]["ts"],
                                  ep[-1].get("local_id") or 0, idx)
            except Exception as e:
                print("   ⚠️ 第 %d 段落库失败（%s: %s），跳过"
                      % (idx, type(e).__name__, str(e)[:60]))
                continue
            done_total += 1
            ep_total += 1
            print("   [%d/%d] %s ｜ %s ｜ 重要度 %s ｜ 事实 %d ｜ %.0fs"
                  % (idx, len(eps), data.get("topic", "?"),
                     (data.get("summary") or "")[:40], data.get("importance"),
                     n_fact, time.time() - t0))
        if args.limit and done_total >= args.limit:
            break

    st = memory.stats(con)
    print("\n完成：本次处理 %d 段，用时 %.1f 分钟" % (done_total, (time.time() - t_start) / 60))
    print("库内累计：片段 %d ｜ 事实 %d ｜ 联系人 %d" % (st["episode"], st["fact"], st["contact"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
