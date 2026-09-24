# -*- coding: utf-8 -*-
"""corpus_stats.py —— 只读统计：微信聊天语料到底有多少（决定能不能训练）

统计口径：
  · 表格数 / 消息条数 / 文本条数 / 我发的文本条数
  · 可用的「对方说 → 我回」配对数量（这才是微调/风格提取的样本）
  · 时间跨度、Top 会话
只读解密副本，不碰微信进程与原始库。
"""
from __future__ import annotations

import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wxsnap  # noqa: E402

SELF = "wxid_你的wxid"


def main():
    dbs = sorted(glob.glob(os.path.join(wxsnap.PLAIN, "message", "message*.db")))
    print("消息库：%d 个" % len(dbs))
    tot_msgs = tot_text = tot_mine = 0
    tot_chars_all = tot_chars_mine = 0
    pairs = 0
    per_chat = []
    t_min, t_max = None, None
    names_cache = {}

    for db in dbs:
        base = os.path.basename(db)
        try:
            con = wxsnap.open_plain(os.path.join("message", base))
        except Exception as e:
            print("  %s 打不开：%s" % (base, e))
            continue
        try:
            idx = wxsnap.build_name_index(con)
        except Exception as e:
            print("  %-16s 没有 Name2Id（%s）—— 跳过" % (base, type(e).__name__))
            con.close()
            continue
        tables = [t for t in wxsnap._tables(con) if t.startswith("Msg_")]
        n_msg = n_text = n_mine = 0
        c_all = c_mine = 0
        n_pairs = 0
        for t in tables:
            try:
                rows = list(con.execute(
                    f'SELECT local_type, real_sender_id, create_time, '
                    f'length(message_content) FROM "{t}"'))
            except Exception:
                continue
            prev_other = False
            for ty, sid, ts, ln in rows:
                n_msg += 1
                if ts:
                    t_min = ts if t_min is None else min(t_min, ts)
                    t_max = ts if t_max is None else max(t_max, ts)
                if ty != 1:
                    continue
                n_text += 1
                c_all += (ln or 0)
                mine = idx.get(str(sid)) == SELF
                if mine:
                    n_mine += 1
                    c_mine += (ln or 0)
                    if prev_other:
                        n_pairs += 1
                prev_other = not mine
        con.close()
        tot_msgs += n_msg
        tot_text += n_text
        tot_mine += n_mine
        tot_chars_all += c_all
        tot_chars_mine += c_mine
        pairs += n_pairs
        per_chat.append((n_text, base, len(tables), n_mine))
        print("  %-16s 表 %4d ｜ 消息 %7d ｜ 文本 %7d ｜ 我发 %7d ｜ 我发字数 %9d ｜ 可配对 %6d"
              % (base, len(tables), n_msg, n_text, n_mine, c_mine, n_pairs))

    print("-" * 100)
    print("合计：")
    print("  消息条数        %8d" % tot_msgs)
    print("  文本条数        %8d" % tot_text)
    print("  我发的文本      %8d  （占 %.1f%%）" % (tot_mine, 100.0 * tot_mine / max(tot_text, 1)))
    print("  全部文本字数    %8d 字（约 %.1f 万）" % (tot_chars_all, tot_chars_all / 10000.0))
    print("  我发的字数      %8d 字（约 %.1f 万）" % (tot_chars_mine, tot_chars_mine / 10000.0))
    print("  可用配对样本    %8d 对（对方说 → 我回）" % pairs)
    if t_min and t_max:
        import time
        print("  时间跨度        %s → %s"
              % (time.strftime("%Y-%m-%d", time.localtime(t_min)),
                 time.strftime("%Y-%m-%d", time.localtime(t_max))))
    print("-" * 100)
    print("训练样本量参考：")
    print("  · LoRA/QLoRA 微调 7B：一般需要 1k~10k 条指令样本")
    print("  · 风格提取 / 人设归纳：几百条高质量样例就够（甚至更好）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
