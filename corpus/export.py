# -*- coding: utf-8 -*-
"""export.py —— P0 导出：把范围内会话的对话导成 jsonl（只读）

范围由 corpus/scope.json 控制。
**主人自己发的所有消息全部保留**（含表情包、单字）—— 那是风格信号。
对方的噪音（表情/链接/图片）保留但标记类型，供后续过滤。

输出：
  corpus/turns/<会话名>.jsonl   每行一条 {ts, time, who, sender, type, text, local_id}
  corpus/turns/_summary.json    导出概览
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import wxsnap  # noqa: E402

SELF = "wxid_你的wxid"
OUT = os.path.join(HERE, "turns")

TYPE_LABEL = {
    1: None,            # 文本，用原文
    3: "[图片]",
    34: "[语音]",
    42: "[名片]",
    43: "[视频]",
    47: "[动画表情]",
    48: "[位置]",
    49: "[链接/文件]",
    50: "[通话]",
    10000: "[系统]",
}


def safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name)[:60]


def load_scope():
    with open(os.path.join(HERE, "scope.json"), encoding="utf-8") as f:
        return json.load(f)


def main():
    scope = load_scope()
    since = int(time.mktime(time.strptime(scope["since"] + " 00:00:00", "%Y-%m-%d %H:%M:%S")))
    want = set(scope["chats"])

    os.makedirs(OUT, exist_ok=True)
    con = wxsnap.open_plain(os.path.join("message", "message_0.db"))
    contacts = wxsnap.load_contacts()
    idx = wxsnap.build_name_index(con)

    # 会话名 → username
    by_display = {}
    for u, d in contacts.items():
        by_display.setdefault(d, u)

    summary = []
    for name in scope["chats"]:
        username = by_display.get(name)
        if not username:
            # 模糊匹配
            for u, d in contacts.items():
                if name in d or d in name:
                    username = u
                    break
        if not username:
            print("⚠️ 找不到会话：%s" % name)
            continue
        tbl = wxsnap.chat_table(con, username)
        if tbl not in wxsnap._tables(con):
            print("⚠️ 没有消息表：%s (%s)" % (name, username))
            continue

        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{tbl}")')]
        rows = list(con.execute(f'SELECT * FROM "{tbl}" ORDER BY rowid'))
        is_group = username.endswith("@chatroom")

        out_path = os.path.join(OUT, safe(name) + ".jsonl")
        n_all = n_keep = n_mine = n_mine_sticker = n_mine_short = 0
        tmin = tmax = None
        with open(out_path, "w", encoding="utf-8") as f:
            for r in rows:
                d = dict(zip(cols, r))
                ts = int(d.get("create_time") or 0)
                if ts < since:
                    continue
                mtype = d.get("local_type")
                if mtype in (10000, 10002):
                    continue
                sender = idx.get(str(d.get("real_sender_id")), str(d.get("real_sender_id")))
                mine = (sender == SELF)
                if mtype == 1:
                    text = wxsnap.decode_message_content(
                        d.get("message_content"), d.get("WCDB_CT_message_content"))
                else:
                    text = TYPE_LABEL.get(mtype, "[type=%s]" % mtype)

                n_all += 1
                if mine:
                    n_mine += 1
                    if mtype == 47:
                        n_mine_sticker += 1
                    if mtype == 1 and len((text or "").strip()) <= 2:
                        n_mine_short += 1
                n_keep += 1
                tmin = ts if tmin is None else min(tmin, ts)
                tmax = ts if tmax is None else max(tmax, ts)

                f.write(json.dumps({
                    "ts": ts,
                    "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                    "who": "me" if mine else "other",
                    "sender": contacts.get(sender, sender) if not mine else "我",
                    "type": mtype,
                    "text": text,
                    "local_id": d.get("local_id"),
                }, ensure_ascii=False) + "\n")

        summary.append({
            "chat": name, "username": username, "group": is_group,
            "messages": n_all, "mine": n_mine,
            "mine_sticker": n_mine_sticker, "mine_short": n_mine_short,
            "file": os.path.relpath(out_path, ROOT),
            "from": time.strftime("%Y-%m-%d", time.localtime(tmin)) if tmin else None,
            "to": time.strftime("%Y-%m-%d", time.localtime(tmax)) if tmax else None,
        })
        print("  %-22s %6d 条 ｜ 我发 %5d（表情 %4d ｜ 单字 %4d） ｜ %s → %s"
              % (name[:22], n_all, n_mine, n_mine_sticker, n_mine_short,
                 summary[-1]["from"], summary[-1]["to"]))

    con.close()
    with open(os.path.join(OUT, "_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    tot = sum(s["messages"] for s in summary)
    mi = sum(s["mine"] for s in summary)
    print("-" * 90)
    print("导出完成：%d 个会话 ｜ 消息 %d 条 ｜ 我发 %d 条 ｜ 表情 %d ｜ 单字 %d"
          % (len(summary), tot, mi,
             sum(s["mine_sticker"] for s in summary),
             sum(s["mine_short"] for s in summary)))
    print("输出目录：%s" % os.path.relpath(OUT, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
