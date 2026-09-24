# -*- coding: utf-8 -*-
"""scope_stats.py —— 只读：按主人给的范围统计工程量

范围（2026-09-24 主人指定）：
  · 只抽取 2026-07-18 00:00 之后的记录
  · 群聊只抽「某兴趣群」和「某多人群」，其余群聊不抽
  · 单聊按上面的日期过滤

用途：算清楚到底还剩多少数据、要跑多久。
"""
from __future__ import annotations

import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wxsnap  # noqa: E402

SELF = "wxid_你的wxid"
SINCE = int(time.mktime(time.strptime("2026-07-18 00:00:00", "%Y-%m-%d %H:%M:%S")))
GROUP_ALLOW = ["福瑞控", "契约"]      # 子串匹配（含表情的群名容易打错）


def main():
    dbs = sorted(glob.glob(os.path.join(wxsnap.PLAIN, "message", "message*.db")))
    con = None
    for db in dbs:
        if os.path.basename(db) == "message_0.db":
            con = wxsnap.open_plain(os.path.join("message", "message_0.db"))
    if con is None:
        print("找不到 message_0.db")
        return 2

    contacts = wxsnap.load_contacts()
    idx = wxsnap.build_name_index(con)
    tables = [t for t in wxsnap._tables(con) if t.startswith("Msg_")]

    rows_out = []
    for t in tables:
        try:
            rows = list(con.execute(
                f'SELECT local_type, real_sender_id, create_time, '
                f'length(message_content) FROM "{t}"'))
        except Exception:
            continue
        username = None
        # 反查表名对应的会话
        for u in contacts:
            if wxsnap.chat_table(con, u) == t:
                username = u
                break
        display = contacts.get(username or "", username or t)
        is_group = bool(username and username.endswith("@chatroom"))

        n_txt = n_mine = 0
        c_mine = 0
        tmin = tmax = None
        n_txt_scoped = n_mine_scoped = c_mine_scoped = 0
        for ty, sid, ts, ln in rows:
            if ts:
                tmin = ts if tmin is None else min(tmin, ts)
                tmax = ts if tmax is None else max(tmax, ts)
            if ty != 1:
                continue
            n_txt += 1
            mine = idx.get(str(sid)) == SELF
            if mine:
                n_mine += 1
                c_mine += (ln or 0)
            if ts and ts >= SINCE:
                n_txt_scoped += 1
                if mine:
                    n_mine_scoped += 1
                    c_mine_scoped += (ln or 0)
        rows_out.append({
            "table": t, "username": username or "?", "display": display,
            "group": is_group, "n_txt": n_txt, "n_mine": n_mine, "c_mine": c_mine,
            "n_txt_s": n_txt_scoped, "n_mine_s": n_mine_scoped, "c_mine_s": c_mine_scoped,
            "tmin": tmin, "tmax": tmax,
        })
    con.close()

    def in_scope(r):
        if r["group"] and not any(g in r["display"] for g in GROUP_ALLOW):
            return False
        return True

    scope = [r for r in rows_out if in_scope(r)]
    out_scope = [r for r in rows_out if not in_scope(r)]

    print("会话总数 %d ｜ 范围内 %d ｜ 排除 %d（群聊）" % (len(rows_out), len(scope), len(out_scope)))
    print()
    print("== 被排除的群聊 ==")
    for r in sorted(out_scope, key=lambda x: -x["n_txt"])[:15]:
        print("   %-28s 文本 %6d ｜ 我发 %5d" % (r["display"][:28], r["n_txt"], r["n_mine"]))
    if len(out_scope) > 15:
        print("   …还有 %d 个" % (len(out_scope) - 15))

    print()
    print("== 范围内的会话（按我的发言量排序，前 20） ==")
    for r in sorted(scope, key=lambda x: -x["n_mine_s"])[:20]:
        tag = "群" if r["group"] else "单"
        print("   [%s] %-26s 7/18后文本 %6d ｜ 我发 %5d ｜ 我发字数 %6d"
              % (tag, r["display"][:26], r["n_txt_s"], r["n_mine_s"], r["c_mine_s"]))

    print()
    print("=" * 96)
    tot_txt_s = sum(r["n_txt_s"] for r in scope)
    tot_mine_s = sum(r["n_mine_s"] for r in scope)
    tot_chars_s = sum(r["c_mine_s"] for r in scope)
    tot_txt_all = sum(r["n_txt"] for r in rows_out)
    tot_chars_all = sum(r["c_mine"] for r in rows_out)
    print("范围总量（7/18 之后 + 指定群）：")
    print("  文本条数       %7d 条   （全量是 %d 条，缩到 %.0f%%）"
          % (tot_txt_s, tot_txt_all, 100.0 * tot_txt_s / max(tot_txt_all, 1)))
    print("  我发的文本     %7d 条   （全量 %d 条）" % (tot_mine_s, sum(r["n_mine"] for r in rows_out)))
    print("  我发的字数     %7d 字  （全量 %d 字，缩到 %.0f%%）"
          % (tot_chars_s, tot_chars_all, 100.0 * tot_chars_s / max(tot_chars_all, 1)))
    print()
    print("本地跑批估算（qwen2.5:7b @ 4060，约 25-40 token/s，1 汉字≈1 token）：")
    est_tokens = int(tot_chars_s * 3.2)      # 我发+对方发的全部文本，粗估（对方占多数）
    print("  预计要处理的 token 量级：约 %.1f 万" % (est_tokens / 10000.0))
    print("  粗估耗时：%.1f ~ %.1f 小时" % (est_tokens / 40 / 3600.0, est_tokens / 25 / 3600.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
