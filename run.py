# -*- coding: utf-8 -*-
"""run.py —— 大脑：读库 → 判定 → 生成 → 按开关「草稿 / 真发」

读：走数据库（wxsnap.py），完全不碰微信窗口
发：走已验证的 send_wechat.py（白名单 / 脱敏 / 审计 / 剪贴板还原 / 空闲闸门 / 前台还原）

用法::

    python run.py --once                 # 扫一轮，只写草稿（默认）
    python run.py --once --no-draft      # 只看「谁需要回复」，不调模型
    python run.py --once --send          # 真发（需先把开关打开：python switch.py on）
    python run.py --loop --send          # 常驻
    python run.py --once --targets 好友C # 只看指定会话

退出码：0 正常 ｜ 2 微信未就绪 ｜ 3 急停 ｜ 5 开关没开 ｜ 9 单实例冲突
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

import wxsnap
from guard import Guard

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def load_cfg():
    import json
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def fmt_ts(v):
    return wxsnap._fmt_ts(v)


# ---------------------------------------------------------------- 读会话
def read_sessions(contacts):
    """返回 [(username, display, unread, summary, last_ts, last_sender, last_type)]"""
    con = wxsnap.open_plain("session/session.db")
    rows = list(con.execute(
        "SELECT username, unread_count, summary, last_timestamp,"
        " last_msg_sender, last_msg_type FROM SessionTable"))
    con.close()
    out = []
    for u, uc, sm, ts, snd, mt in rows:
        out.append({
            "username": u,
            "display": contacts.get(u, u),
            "unread": int(uc or 0),
            "summary": (str(sm or "")).replace("\n", " ")[:60],
            "last_ts": int(ts or 0),
            "last_sender": snd,
            "last_type": mt,
        })
    return out


def read_history(username, limit=12):
    """返回 (rows, table_name)；rows 为最近 limit 条（时间正序）"""
    con = wxsnap.open_plain("message/message_0.db")
    tbl = wxsnap.chat_table(con, username)
    names = wxsnap._tables(con)
    if tbl not in names:
        con.close()
        return [], tbl
    idx = wxsnap.build_name_index(con)
    cols = [r[1] for r in con.execute(f'PRAGMA table_info("{tbl}")')]
    rows = list(con.execute(
        f'SELECT * FROM "{tbl}" ORDER BY rowid DESC LIMIT ?', (limit,)))
    con.close()
    out = []
    for r in reversed(rows):
        d = dict(zip(cols, r))
        sender = idx.get(str(d.get("real_sender_id")), str(d.get("real_sender_id")))
        mtype = d.get("local_type")
        if mtype in (10000, 10002):
            continue        # 系统提示（"你已添加…"之类）不算对话轮次
        if mtype == 1:
            text = wxsnap.decode_message_content(
                d.get("message_content"), d.get("WCDB_CT_message_content"))
        else:
            # 非文本（表情包/图片/语音/链接…）用可读占位，让模型知道对方发了什么
            text = "[%s]" % text_of_type(mtype)
        out.append({
            "sender": sender,
            "text": text,
            "ts": int(d.get("create_time") or 0),
            "local_id": d.get("local_id"),
            "type": mtype,
            "status": d.get("status"),
        })
    return out, tbl


def text_of_type(t):
    return {1: "文本", 3: "图片", 34: "语音", 43: "视频", 47: "动画表情",
            49: "链接/文件", 10000: "系统消息"}.get(t, "type=%s" % t)


# ---------------------------------------------------------------- 判定
def pick_targets(cfg, guard, contacts, sessions, only=None):
    """挑出「该回」的会话（全部数据驱动，不猜）"""
    w = cfg.get("watch") or {}
    max_age = int(w.get("max_message_age_sec", 300))
    echoes = set(w.get("echo_sessions") or [])
    now = int(time.time())
    picked, skipped = [], []
    for s in sessions:
        name = s["display"]
        if only and name not in only and s["username"] not in only:
            continue
        if not guard.in_whitelist(name):
            continue
        if s["username"].startswith(("gh_", "brand")) or s["username"] == "brandsessionholder":
            skipped.append((name, "公众号/服务号"))
            continue
        if s["username"].endswith("@chatroom"):
            skipped.append((name, "群聊（默认不回）"))
            continue
        # 回声会话（如「文件传输助手」）：那里所有消息都是自己发的，
        # 一律把最后一条当成"对方说的"，否则永远触发不了回复
        if name not in echoes and s["last_sender"] == cfg.get("self_wxid"):
            skipped.append((name, "最后一条是我说的"))
            continue
        age = now - s["last_ts"] if s["last_ts"] else 999999
        if age > max_age:
            skipped.append((name, "消息太旧（%.0f 分钟前）" % (age / 60)))
            continue
        picked.append((s, age))
    picked.sort(key=lambda x: x[1])
    return picked, skipped


def build_history(cfg, contacts, username, limit=12):
    rows, tbl = read_history(username, limit)
    self_wxid = cfg.get("self_wxid")
    echoes = set((cfg.get("watch") or {}).get("echo_sessions") or [])
    display = contacts.get(username, username)
    is_echo = display in echoes
    hist = []
    texts = list(rows)          # 表情包/图片等也算（主人要求：对方发什么都要接）
    for i, r in enumerate(texts):
        if is_echo:
            # 回声会话：最后一条当作"对方"说的，其余当作"我"（否则模型看到一整段独白）
            who = "对方" if i == len(texts) - 1 else "我"
        else:
            who = "我" if r["sender"] == self_wxid else "对方"
        hist.append({"who": who, "text": r["text"], "ts": r["ts"],
                     "local_id": r["local_id"], "status": r["status"],
                     "type": r.get("type")})
    return hist, rows, tbl


# ---------------------------------------------------------------- 发送
def do_send(cfg, guard, chat_display, text):
    s = cfg.get("sender") or {}
    py = s.get("python")
    script = s.get("script")
    wd = s.get("workdir")
    if not (py and script and os.path.exists(py) and os.path.exists(script)):
        return False, "找不到发送器（配置里的 python/script 路径）", None
    cmd = [py, script, "--to", chat_display, "--text", text, "--confirm"]
    # 空闲闸门阈值可配（默认 5s，测试时可调小，见 config.sender.min_idle_sec）
    mid = s.get("min_idle_sec")
    if mid is not None:
        cmd += ["--min-idle", str(mid)]
    try:
        p = subprocess.run(cmd, cwd=wd or None, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        return False, "发送超时", None
    out = (p.stdout or b"").decode("utf-8", "replace")
    tail = "\n".join([l for l in out.strip().splitlines() if l.strip()][-3:])
    ok = (p.returncode == 0)
    why = "ok" if ok else ("空闲闸门拒绝（主人在用电脑）" if p.returncode == 8
                           else "退出码 %d" % p.returncode)
    return ok, why, tail


# ---------------------------------------------------------------- 主流程
def check_control(cfg, guard, contacts, text_override=None):
    """微信遥控：读「文件传输助手」里主人最新一句，是「停止/开始」就执行。

    返回 ('stop'|'resume'|None, 说明)。**停止后循环不退出**，只是静默待命，
    这样主人再发「开始」还能把它叫回来。
    """
    c = cfg.get("control") or {}
    chat = c.get("chat") or "文件传输助手"
    username = None
    for u, d in contacts.items():
        if d == chat:
            username = u
            break
    if not username:
        return None, "找不到遥控会话"

    if text_override is not None:
        last = {"text": text_override, "local_id": 0, "ts": 0}
        key = "TEST:" + text_override
    else:
        rows, _ = read_history(username, 3)
        mine = [r for r in rows if r.get("sender") == cfg.get("self_wxid")]
        if not mine:
            return None, "遥控会话里没有主人的消息"
        last = mine[-1]
        key = "%s:%s" % (last.get("ts"), last.get("local_id"))

    if key == guard.control_watermark():
        return None, "这条指令已处理过"
    word = guard.control_word(last.get("text") or "")
    if not word:
        guard.set_control_watermark(key)      # 记水位，避免把普通消息当指令
        return None, "没有指令"

    # ---- 执行 ----
    if word == "stop":
        guard.set_auto(False, by="wechat-command", paused=True,
                       reason="主人发微信「%s」" % (last.get("text")))
        guard.audit({"action": "control", "cmd": "stop",
                     "from": last.get("text"), "ok": True})
        msg = c.get("confirm_stop") or "已停止自动对话"
    else:
        names = guard.whitelist_names()
        if not names:
            return None, "白名单为空，拒绝恢复"
        guard.set_auto(True, by="wechat-command", paused=False)
        guard.audit({"action": "control", "cmd": "resume",
                     "from": last.get("text"), "ok": True})
        msg = c.get("confirm_resume") or "已恢复自动对话"

    guard.set_control_watermark(key, {"cmd": word})
    if c.get("reply_confirm", True) and text_override is None:
        do_send(cfg, guard, chat, msg)
    return word, msg


def write_heartbeat(extra=None):
    """每轮写一次心跳，供 service.py status 判断「活着 + 最近在干嘛」。"""
    rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "ts": int(time.time()),
           "pid": os.getpid()}
    if extra:
        rec.update(extra)
    try:
        os.makedirs(os.path.join(HERE, "state"), exist_ok=True)
        with open(os.path.join(HERE, "state", "heartbeat.json"), "w",
                  encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("   ⚠️ 心跳写入失败：%s" % e)


def verify_sent(cfg, contacts, chat_display, lines, since_ts):
    """发完后**查解密副本**确认消息真的进了会话（替掉不可靠的 UI 回读）。

    今天实测过：发送器会报「成功」但其实没发出去（58.6s 那次假成功）。
    返回 (是否全部确认, 未确认的行)。
    """
    ok, _ = refresh_snapshot()
    username = None
    for u, d in contacts.items():
        if d == chat_display:
            username = u
            break
    if not username:
        return None, ["（找不到会话名 %s）" % chat_display]
    rows, _ = read_history(username, 15)
    mine_recent = [(r.get("text") or "").strip() for r in rows
                   if r.get("sender") == cfg.get("self_wxid")
                   and int(r.get("ts") or 0) >= since_ts - 10]
    missing = []
    for ln in lines:
        t = ln.strip()
        if not any(t == x or (t and t in x) for x in mine_recent):
            missing.append(ln)
    return (len(missing) == 0), missing


def refresh_snapshot(quiet=True):
    """重新拷贝原库并解密（读的是副本；密钥来自微信进程内存）。"""
    import contextlib
    import io
    buf = io.StringIO()
    ctx = contextlib.redirect_stdout(buf) if quiet else contextlib.nullcontext()
    with ctx:
        rc1 = wxsnap.do_snapshot()
        rc2 = wxsnap.do_decrypt()
    return (rc1 == 0 and rc2 == 0), buf.getvalue()


def in_quiet_hours(cfg, now=None) -> bool:
    """现在是不是「安静时段」（真人这时候在睡觉，不该回消息）。

    支持跨午夜，如 ["23:30", "07:30"] 表示 23:30 → 次日 07:30。
    """
    qh = (cfg.get("watch") or {}).get("quiet_hours") or []
    if len(qh) != 2:
        return False
    try:
        sh, sm = [int(x) for x in str(qh[0]).split(":")[:2]]
        eh, em = [int(x) for x in str(qh[1]).split(":")[:2]]
    except Exception:
        return False
    t = now or time.localtime()
    cur = t.tm_hour * 60 + t.tm_min
    a, b = sh * 60 + sm, eh * 60 + em
    return (a <= cur < b) if a <= b else (cur >= a or cur < b)


def process_retry_queue(cfg, guard, contacts, live_send):
    """补发上次「没在库里确认到」的消息（2026-09-24 新增，第 ⑨ 项）。

    · 只在允许真发时补
    · 超过 30 分钟的不再补（过时的话发出去更怪）
    · 同一条最多补 3 次
    · 主人正在跟这个人聊天 → 让路，不补
    """
    qpath = os.path.join(HERE, "state", "retry_queue.json")
    if not os.path.exists(qpath):
        return 0
    try:
        with open(qpath, encoding="utf-8") as f:
            items = json.load(f) or []
    except Exception as e:
        print("   ⚠️ 待重发队列读取失败：%s: %s" % (type(e).__name__, e))
        return 0
    if not items:
        return 0
    if not live_send:
        print("📥 待重发 %d 条（开关关着，暂不补发）" % len(items))
        return 0

    now = int(time.time())
    keep, done = [], 0
    for it in items:
        chat = it.get("chat")
        username = it.get("username")
        lines = it.get("lines") or []
        tries = int(it.get("tries", 0))
        age = now - int(it.get("ts", now))
        if not lines or tries >= 3 or age > 1800:
            print("   🗑️  丢弃过期待重发：%s（%d 次 / %d 分钟前）"
                  % (chat, tries, age // 60))
            continue
        # 主人正在聊 → 让路
        try:
            rows, _ = read_history(username, 8)
            my_last = max([r["ts"] for r in rows
                           if r.get("sender") == cfg.get("self_wxid")] or [0])
            if my_last and now - my_last < int((cfg.get("watch") or {})
                                               .get("master_active_sec", 90)):
                keep.append(it)
                continue
        except Exception:
            pass
        print("   📤 补发 → %s：%s" % (chat, " ⏎ ".join(lines)))
        t0 = int(time.time())
        sent = []
        for line in lines:
            okr, why = guard.rate_check()
            if not okr:
                print("      ⏸️  速率闸门：%s" % why)
                break
            ok, why, _ = do_send(cfg, guard, chat, line)
            if ok:
                guard.rate_mark()
                sent.append(line)
            else:
                print("      ❌ %s" % why)
                break
        if sent:
            verified, missing = verify_sent(cfg, contacts, chat, sent, t0)
            if verified:
                print("      ✅ 补发并校验成功（%d 条）" % len(sent))
                done += 1
                continue
        it["tries"] = tries + 1
        it["ts"] = int(it.get("ts", now))
        keep.append(it)
    try:
        with open(qpath, "w", encoding="utf-8") as f:
            json.dump(keep[-50:], f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    if items:
        print("   📥 待重发队列：补成功 %d ｜ 剩余 %d" % (done, len(keep)))
    return done


def one_round(cfg, guard, args):
    if not args.no_refresh:
        t0 = time.time()
        ok, out = refresh_snapshot()
        print("🔄 快照已刷新（%.1fs）%s" % (time.time() - t0, "" if ok else " ⚠️ 有失败项"))
    contacts = wxsnap.load_contacts()

    # ---- 微信遥控优先：主人发「停止」就立刻停 ----
    cmd, why = check_control(cfg, guard, contacts)
    if cmd == "stop":
        print("🛑 收到微信指令「停止」—— 自动对话已停止（循环继续待命，发「开始」可恢复）")
        return 0
    if cmd == "resume":
        print("▶️ 收到微信指令「开始」—— 自动对话已恢复")

    if guard.paused():
        print("⏸️  当前处于停止状态（%s）—— 只监听遥控指令，不做任何回复"
              % guard.pause_reason())
        return 0

    # ---- 安静时段：真人在睡觉，不回（--force 可跳过）----
    if not args.force and in_quiet_hours(cfg):
        qh = (cfg.get("watch") or {}).get("quiet_hours")
        print("🌙 安静时段（%s ~ %s）—— 不回消息（真人在睡觉）" % (qh[0], qh[1]))
        return 0

    sessions = read_sessions(contacts)
    # ---- 每轮实时看开关：关着就只写草稿（微信发「开始」立即生效）----
    live_send = bool(args.send) and (args.force or guard.auto_enabled())
    if args.send and not live_send:
        print("📝 开关是关的 → 本轮只写草稿（微信发「开始」即恢复真发）")

    # ---- 先补发上次没送达的（若有）----
    process_retry_queue(cfg, guard, contacts, live_send)
    print("📋 会话 %d 个，白名单 %s" % (len(sessions), guard.whitelist_names()))

    only = set(args.targets.split(",")) if args.targets else None
    picked, skipped = pick_targets(cfg, guard, contacts, sessions, only)
    for name, why in skipped[:8]:
        print("   ⏭️  %s —— %s" % (name, why))
    if not picked:
        print("   （本轮没有需要回复的会话）")
        return 0

    sent_any = 0
    for s, age in picked[: int((cfg.get("watch") or {}).get("max_per_round", 3))]:
        name = s["display"]
        hist, rows, tbl = build_history(cfg, contacts, s["username"])
        if not hist:
            print("   ⏭️  %s —— 读不到可回复的文本消息" % name)
            continue
        last = hist[-1]
        key = "%s:%s" % (last["ts"], last["local_id"])
        print("🎯 %s（%.0f 秒前）最后一句：%s" % (name, age, last["text"][:40]))

        # ⭐ 主人正在聊天 → 让路（2026-09-24 新增）
        # 只要主人 N 秒内在这个会话说过话，就说明他人在，机器人别插嘴。
        master_sec = int((cfg.get("watch") or {}).get("master_active_sec", 90))
        if master_sec > 0:
            now_ts = int(time.time())
            my_last = max([r["ts"] for r in rows
                           if r.get("sender") == cfg.get("self_wxid")] or [0])
            if my_last:
                ago = now_ts - my_last
                # 取「主人最后发言」与「对方最后发言」较晚者为基准，避免旧账误判
                if ago < master_sec:
                    print("   ⏭️  主人 %d 秒前刚在这说过话 → 让路（%ds 内不插嘴）"
                          % (ago, master_sec))
                    continue

        # 以【消息表】为准判断最后一条是不是自己发的
        # （会话表的 last_msg_sender 实测可能是空的，不可靠）
        echoes = set((cfg.get("watch") or {}).get("echo_sessions") or [])
        if name not in echoes and last.get("who") == "我":
            print("   ⏭️  最后一条是我说的（以消息表为准）")
            continue

        if guard.is_seen(s["username"], key):
            print("   ⏭️  已经回过这条了（水位线）")
            continue

        # 回声会话防自问自答：如果最后一条正是我们刚回出去的那句 → 不再回
        prev = (guard.seen().get(s["username"]) or {})
        if prev.get("reply") and prev["reply"].strip() == (last["text"] or "").strip():
            print("   ⏭️  最后一条就是我们刚回出去的（防自问自答）")
            continue

        if args.no_draft:
            print("   （--no-draft：只看不写）")
            continue

        import random as _random
        import reply as reply_mod
        pmode = (cfg.get("persona") or {}).get("mode", "imitate")
        if pmode == "imitate":
            lines, info = reply_mod.generate_burst(cfg, name, hist, HERE)
        else:
            one, info = reply_mod.generate(cfg, hist, HERE)
            lines = [one] if one else []
        lines = [l for l in lines if l and l.strip()]
        if not lines:
            print("   ❌ 生成失败：%s" % info)
            guard.audit({"action": "reply", "chat": name, "ok": False,
                         "blocked_by": "generate-failed", "detail": str(info)})
            continue
        text = "\n".join(lines)
        print("   ✍️  草稿（%d 条连发）：%s" % (len(lines), " ⏎ ".join(lines)))

        hits = guard.redact_hits(text)
        if hits and (cfg.get("redaction") or {}).get("block_on_hit", True):
            print("   ⛔ 脱敏命中，中止：%r" % hits)
            guard.audit({"action": "reply", "chat": name, "reply": text,
                         "ok": False, "blocked_by": "redaction", "hits": hits})
            continue

        if not live_send:
            guard.draft({"chat": name, "incoming": last["text"], "draft": text,
                         "lines": lines,
                         "engine": (info or {}).get("model") if isinstance(info, dict) else None,
                         "mode": "draft", "sent": False})
            print("   📝 已写草稿（开关关着；微信发「开始」即真发）")
            continue

        # ---- 连发：逐条发，条间停顿 1.2~3 秒（模拟真人节奏）----
        sent_lines = []
        t_send0 = int(time.time())
        for idx, line in enumerate(lines):
            okr, why = guard.rate_check()
            if not okr:
                print("   ⏸️  速率闸门：%s" % why)
                break
            ok, why, tail = do_send(cfg, guard, name, line)
            if ok:
                guard.rate_mark()
                sent_any += 1
                sent_lines.append(line)
            print("   📤 [%d/%d] %s：%s" % (idx + 1, len(lines),
                                            "成功" if ok else "失败", why))
            if tail and idx == 0:
                print("      " + tail.replace("\n", "\n      "))
            if not ok:
                break
            if idx < len(lines) - 1:
                # 条间停顿：既要像真人（1.2~3s），又不能撞上速率闸门的最小间隔
                gap = float((cfg.get("rate") or {}).get("min_gap_sec", 2.5))
                time.sleep(max(_random.uniform(1.2, 3.0), gap + 0.6))
        ok = bool(sent_lines)
        # ---- ⭐ 数据库校验：发送器说成功不算数，库里查到了才算（2026-09-24）----
        verified = None
        missing = []
        if sent_lines:
            verified, missing = verify_sent(cfg, contacts, name, sent_lines, t_send0)
            if verified:
                print("   ✅ 数据库校验：%d 条全部确认送达" % len(sent_lines))
            else:
                print("   ⚠️ 数据库校验：这些没在库里确认到 → %s" % missing)
                guard.audit({"action": "reply", "chat": name, "incoming": last["text"],
                             "reply": text, "lines": sent_lines, "ok": False,
                             "blocked_by": "send-unverified", "missing": missing,
                             "auto": True})
        guard.audit({"action": "reply", "chat": name, "incoming": last["text"],
                     "reply": text, "lines": sent_lines, "ok": ok,
                     "verified": verified, "auto": True,
                     "partial": len(sent_lines) != len(lines)})
        if missing:
            # 记进待重发表，下一轮补发（不静默丢失）
            try:
                q = os.path.join(HERE, "state", "retry_queue.json")
                items = []
                if os.path.exists(q):
                    items = json.load(open(q, encoding="utf-8")) or []
                items.append({"chat": name, "username": s["username"], "lines": missing,
                              "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "ts": int(time.time()), "tries": 0})
                with open(q, "w", encoding="utf-8") as f:
                    json.dump(items[-50:], f, ensure_ascii=False, indent=2)
                print("   📥 已加入待重发表（%d 条）" % len(missing))
            except Exception as e:
                print("   ⚠️ 待重发表写入失败：%s" % e)
        if ok:
            guard.mark_seen(s["username"], key, {"reply": text})
    print("———— 本轮结束（真发 %d 条）————" % sent_any)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="跑一轮就退出")
    ap.add_argument("--loop", action="store_true", help="常驻循环")
    ap.add_argument("--send", action="store_true", help="真发（默认只写草稿）")
    ap.add_argument("--force", action="store_true", help="真发时跳过开关检查")
    ap.add_argument("--no-draft", action="store_true", help="不调模型，只看谁需要回复")
    ap.add_argument("--targets", default=None, help="只处理这些会话（逗号分隔）")
    ap.add_argument("--interval", type=int, default=None, help="循环间隔秒")
    ap.add_argument("--no-refresh", action="store_true",
                    help="不刷新数据库快照（默认每轮都刷新，否则会回旧消息）")
    args = ap.parse_args()
    if not (args.once or args.loop):
        args.once = True

    cfg = load_cfg()
    guard = Guard(cfg, HERE)

    if guard.stopped():
        print("⛔ 急停开关存在（%s）—— 拒绝运行" % guard.path_stop)
        return 3
    if args.send and not guard.auto_enabled() and not args.force:
        # 不再退出：每轮会实时看开关（关着就只写草稿）。
        # 这样服务启动后能一直待命，主人发「开始」立刻就能真发。
        print("📝 自动发送开关当前是关的 → 只写草稿模式；"
              "微信给「文件传输助手」发「开始」即可真发")

    interval = args.interval or int((cfg.get("watch") or {}).get("interval_sec", 30))
    if args.once:
        return one_round(cfg, guard, args)
    print("🔁 常驻循环启动（间隔 %ds，%s）"
          % (interval, "允许真发（每轮看开关）" if args.send else "只草稿"))
    while True:
        try:
            write_heartbeat({"state": "running", "interval": interval,
                             "send_mode": bool(args.send)})
            one_round(cfg, guard, args)
            write_heartbeat({"state": "idle", "interval": interval,
                             "send_mode": bool(args.send)})
        except KeyboardInterrupt:
            print("收到中断，退出")
            write_heartbeat({"state": "stopped"})
            return 0
        except Exception as e:
            print("⚠️ 本轮异常：%s: %s" % (type(e).__name__, e))
            write_heartbeat({"state": "error", "error": "%s: %s" % (type(e).__name__, e)})
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
