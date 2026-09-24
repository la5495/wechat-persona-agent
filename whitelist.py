# -*- coding: utf-8 -*-
"""whitelist.py —— 白名单增删查

    python whitelist.py list
    python whitelist.py suggest          # 列出微信里的会话名，方便复制
    python whitelist.py add "好友C"
    python whitelist.py remove "好友C"
"""
from __future__ import annotations

import json
import os
import sys
import time

import wxsnap
from guard import Guard, load_json, save_json

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    g = Guard(cfg, HERE)
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "list").lower()

    if cmd == "list":
        wl = g.whitelist()
        if not wl:
            print("（白名单是空的 —— 谁都不会被自动回复）")
            return 0
        for e in wl:
            print("  · %s%s" % (e.get("name"), ("   # " + e["note"]) if e.get("note") else ""))
        return 0

    if cmd == "suggest":
        contacts = wxsnap.load_contacts()
        rows = wxsnap.open_plain("session/session.db").execute(
            "SELECT username FROM SessionTable ORDER BY sort_timestamp DESC LIMIT 40")
        print("微信里的会话名（复制用）：")
        for (u,) in rows:
            print("  %s" % contacts.get(u, u))
        return 0

    if cmd == "add":
        if len(sys.argv) < 3:
            print('用法：python whitelist.py add "名字"')
            return 1
        name = sys.argv[2]
        wl = g.whitelist()
        if any(e.get("name") == name for e in wl):
            print("已经在名单里了：%s" % name)
            return 0
        wl.append({"name": name, "note": "cli", "added_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        save_json(g.path_wl, wl)
        print("✅ 已加入：%s（现在自动回复会覆盖它）" % name)
        return 0

    if cmd == "remove":
        if len(sys.argv) < 3:
            print('用法：python whitelist.py remove "名字"')
            return 1
        name = sys.argv[2]
        wl = [e for e in g.whitelist() if e.get("name") != name]
        save_json(g.path_wl, wl)
        print("✅ 已移出：%s" % name)
        return 0

    print("用法：python whitelist.py [list|suggest|add|remove]")
    return 1


if __name__ == "__main__":
    sys.exit(main())
