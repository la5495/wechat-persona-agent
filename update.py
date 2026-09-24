# -*- coding: utf-8 -*-
"""update.py —— 一条命令：增量更新 语料 → 人设 → 记忆

主人的用法（2026-09-24 指定）：
  「每次启动 dsh 之后，如果微信开着就去自己获取浏览，然后更新」

流程（全部只读微信库副本 + 本地模型，零 API 费）：
  1. 微信在跑吗？不在就跳过（不报错）
  2. 刷新解密快照（wxsnap refresh）
  3. 增量导出语料（corpus/export.py → corpus/pairs.py → corpus/style_stats.py）
  4. 重建人设包（corpus/build_persona.py，秒级）
  5. 增量建记忆（corpus/memory_build.py，只处理新片段）
  6. 打一份简报 + 记录到 state/update.json

用法：
  python update.py              # 常规增量
  python update.py --quiet      # 只输出结果
  python update.py --skip-memory  # 只更新人设，不跑本地模型
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
STATE = os.path.join(HERE, "state", "update.json")


def sh(args, quiet=False, timeout=3600):
    """跑一个子步骤，返回 (返回码, 输出尾行)。"""
    try:
        p = subprocess.run(args, cwd=HERE, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "超时"
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    tail = [l for l in (out.strip().splitlines() or []) if l.strip()]
    if p.returncode != 0 and not quiet:
        print("   ⚠️ 退出码 %d：%s" % (p.returncode, (err or out).strip()[:200]))
    return p.returncode, "\n".join(tail[-3:])


def wechat_running() -> bool:
    try:
        p = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/NH"],
            capture_output=True, timeout=20)
        return b"Weixin.exe" in (p.stdout or b"")
    except Exception:
        return False


def count_new(before: dict, after: dict) -> dict:
    return {k: after.get(k, 0) - before.get(k, 0) for k in after}


def db_stats():
    """从记忆库读统计。"""
    try:
        sys.path.insert(0, os.path.join(HERE, "corpus"))
        import memory
        con = memory.connect()
        st = memory.stats(con)
        con.close()
        return st
    except Exception:
        return {}


def corpus_stats():
    """语料条数与最近一条消息时间。"""
    turns = os.path.join(HERE, "corpus", "turns")
    n_msg = n_chat = 0
    newest = 0
    try:
        for fp in os.listdir(turns):
            if not fp.endswith(".jsonl"):
                continue
            n_chat += 1
            with open(os.path.join(turns, fp), encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        n_msg += 1
                        try:
                            ts = json.loads(line).get("ts") or 0
                            newest = max(newest, ts)
                        except Exception:
                            pass
    except OSError:
        pass
    return {"messages": n_msg, "chats": n_chat, "newest_ts": newest}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--skip-memory", action="store_true")
    ap.add_argument("--force", action="store_true", help="微信没开也硬更新（用旧快照）")
    args = ap.parse_args()

    t0 = time.time()
    print("=" * 70)
    print("🔄 微信语料自动更新  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))

    if not wechat_running() and not args.force:
        print("⏸️  微信没在运行 —— 跳过更新（主人开微信后再跑一次即可）")
        return 0
    print("✅ 微信在运行")

    before_corpus = corpus_stats()
    before_db = db_stats()

    print("① 刷新解密快照…")
    rc, tail = sh([PY, "-u", "wxsnap.py", "refresh"], quiet=args.quiet)
    if rc != 0:
        print("   ❌ 快照刷新失败，终止：%s" % tail)
        return 2
    print("   ✅ 完成")

    print("② 增量导出语料…")
    for step, script in (("导出", "corpus/export.py"), ("配对", "corpus/pairs.py"),
                         ("统计", "corpus/style_stats.py")):
        rc, tail = sh([PY, "-u", script], quiet=args.quiet)
        print("   %s %s" % ("✅" if rc == 0 else "⚠️", step))

    print("③ 重建人设包…")
    excl = os.path.join(HERE, "corpus", "blindtest", "holdout.jsonl")
    extra = ["--exclude", excl] if os.path.exists(excl) else []
    rc, tail = sh([PY, "-u", "corpus/build_persona.py"] + extra, quiet=args.quiet)
    print("   %s %s" % ("✅" if rc == 0 else "⚠️", tail.splitlines()[0] if tail else ""))

    if not args.skip_memory:
        print("④ 增量建记忆（本地模型，零 API 费）…")
        rc, tail = sh([PY, "-u", "corpus/memory_build.py"], quiet=args.quiet)
        print("   %s %s" % ("✅" if rc == 0 else "⚠️", tail.splitlines()[-1] if tail else ""))
        # 每周给记忆库做一次大扫除（去重 + 清过期），否则只增不减越来越吵
        tidy_path = os.path.join(HERE, "state", "tidy.json")
        last = 0
        try:
            last = json.load(open(tidy_path, encoding="utf-8")).get("ts", 0)
        except Exception:
            pass
        if time.time() - last > 7 * 86400:
            print("   🧹 记忆大扫除（每 7 天一次）…")
            rc, tail = sh([PY, "-u", "corpus/memory_tidy.py"], quiet=args.quiet)
            keep = [l for l in (tail or "").splitlines() if "清理后" in l or "库内现状" in l]
            for l in keep:
                print("      " + l.strip())
            try:
                json.dump({"ts": int(time.time())}, open(tidy_path, "w", encoding="utf-8"))
            except OSError:
                pass
    else:
        print("④ 跳过记忆构建（--skip-memory）")

    after_corpus = corpus_stats()
    after_db = db_stats()
    delta = count_new(before_db, after_db)

    rec = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "took_sec": round(time.time() - t0, 1),
        "messages_total": after_corpus["messages"],
        "messages_new": after_corpus["messages"] - before_corpus["messages"],
        "newest_msg": time.strftime("%Y-%m-%d %H:%M",
                                    time.localtime(after_corpus["newest_ts"] or 0)),
        "episode_total": after_db.get("episode", 0),
        "episode_new": delta.get("episode", 0),
        "fact_total": after_db.get("fact", 0),
        "fact_new": delta.get("fact", 0),
    }
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)

    print("-" * 70)
    print("📊 简报")
    print("   语料：%d 条消息 / %d 个会话（本次新增 %d 条）"
          % (rec["messages_total"], after_corpus["chats"], rec["messages_new"]))
    print("   最新消息时间：%s" % rec["newest_msg"])
    print("   记忆：片段 %d（+%d）｜ 事实 %d（+%d）"
          % (rec["episode_total"], rec["episode_new"],
             rec["fact_total"], rec["fact_new"]))
    print("   用时 %.1f 秒" % rec["took_sec"])

    # ---- ⭐ 看门狗：程序健康（2026-09-24 新增）----
    try:
        sys.path.insert(0, HERE)
        import service as svc
        h = svc.health()
        icon = {"running": "🟢", "hung": "🟠", "crashed": "🔴", "stopped": "⚪"}[h["verdict"]]
        word = {"running": "运行中", "hung": "进程在但心跳停了",
                "crashed": "⚠️ 本该在跑却停了（异常退出，需要重开）",
                "stopped": "未运行（主人主动关的，正常）"}[h["verdict"]]
        extra = ""
        if h.get("heartbeat_age") is not None:
            extra = " ｜ 心跳 %.0f 秒前" % h["heartbeat_age"]
        print("  程序：%s %s%s" % (icon, word, extra))
        if h["verdict"] == "crashed":
            print("     ↳ 重开命令：python service.py start --keep-switch")
        if h["retry_queue"]:
            print("  ⚠️ 待重发消息：%d 条（见 state/retry_queue.json）" % h["retry_queue"])
        if not h["wechat"]:
            print("  ⚠️ 微信没在运行 —— 自动回复无法工作")
        rec["service"] = h["verdict"]
    except Exception as e:
        print("  （健康检查跳过：%s）" % type(e).__name__)

    # ---- ⭐ 版本适配检查（第 ⑬ 项）：微信升级会改库结构/偏移 ----
    try:
        sys.path.insert(0, HERE)
        import wxsnap
        import json as _j
        _cfg = _j.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
        r = wxsnap.compat_check(_cfg)
        if r["ok"]:
            print("  兼容：✅ 微信 %s ｜ %s"
                  % (r["wechat_version"] or "?", "；".join(r["notes"][:2])))
        else:
            print("  兼容：❌ 发现问题 —— 先别开自动回复！")
            for i in r["issues"]:
                print("     ⚠️ %s" % i)
        rec["compat"] = r["ok"]
    except Exception as e:
        print("  （版本检查跳过：%s）" % type(e).__name__)

    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
