# -*- coding: utf-8 -*-
"""switch.py —— 自动发送总开关

    python switch.py status
    python switch.py on
    python switch.py off
"""
from __future__ import annotations

import json
import os
import sys

from guard import Guard

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    g = Guard(cfg, HERE)
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()

    if cmd == "status":
        st = g.auto_enabled()
        print("自动发送：%s" % ("🟢 开" if st else "🔴 关（只写草稿）"))
        print("白名单：%s" % (g.whitelist_names() or "（空 —— 谁都不会被自动回复）"))
        print("急停　：%s" % ("⛔ 已打开（STOP 文件存在）" if g.stopped() else "正常"))
        return 0

    if cmd == "on":
        names = g.whitelist_names()
        if not names:
            print("⛔ 白名单是空的 —— 开了也不会回任何人。先：python whitelist.py add \"名字\"")
            return 1
        print("⚠️  即将打开【自动发送】，会对以下对象自动回复：")
        for n in names:
            print("     · %s" % n)
        print("   其余人一律不动。急停：建 STOP 文件，或按 ESC（旧项目）。")
        ans = input("确认请输入 yes：").strip().lower()
        if ans != "yes":
            print("已取消")
            return 1
        g.set_auto(True, by="cli")
        print("🟢 自动发送已打开")
        return 0

    if cmd == "off":
        g.set_auto(False, by="cli")
        print("🔴 自动发送已关闭（回到只写草稿）")
        return 0

    print("用法：python switch.py [status|on|off]")
    return 1


if __name__ == "__main__":
    sys.exit(main())
