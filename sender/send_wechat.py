# -*- coding: utf-8 -*-
"""send_wechat.py —— 「手」：把一句话发出去（带全套保镖）

安全设计（从第一行代码就在）
---------------------------
1. **默认干跑**：不给 `--confirm` 就只打印"我打算做什么"，一个动作都不做。
2. **目标白名单**：默认只允许发到 `send.allowed_targets`（出厂只有「文件传输助手」= 发给自己）。
   想发给别人必须显式加 `--allow-any-target`。
3. **脱敏校验**：发送前按 `redaction.patterns` 扫一遍，命中即**中止**。
4. **急停开关**：`STOP` 文件存在即拒绝运行。
5. **剪贴板先存后还** —— 发送走 Ctrl+V，会占用主人的剪贴板。
6. **回读验证**：发完读一次控件树确认消息真的出现在列表里，
   **不碰数据库**（所以 `verify` 永远保持 false）。
7. **审计台账**：每次尝试都写 `audit.jsonl`。

用法::

    # 干跑（看看会做什么，不发）
    python send_wechat.py --text "测试一下"

    # 真发（只允许发给自己）
    python send_wechat.py --text "测试一下" --confirm

    # 指定会话（不在白名单里会被拒）
    python send_wechat.py --to 文件传输助手 --text "你好" --confirm

退出码：0 成功 ｜ 2 微信未就绪 ｜ 3 急停 ｜ 5 目标不在白名单 ｜ 6 脱敏命中 ｜ 7 发送失败
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import time

import wxcore
from wxcore import WeChatBridge

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")


def _fg_window():
    """当前前台窗口句柄（发送前记下，发送后还回去）。"""
    try:
        return ctypes.windll.user32.GetForegroundWindow() or None
    except Exception:
        return None


def _restore_fg(hwnd):
    """把前台还给主人原来的窗口。"""
    try:
        if hwnd and ctypes.windll.user32.IsWindow(hwnd):
            ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass

try:   # 会话名可能含 emoji，GBK 控制台下会崩
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def flat_cfg(cfg):
    flat = dict(cfg)
    flat.update(cfg.get("read") or {})
    return flat


def audit(cfg, record):
    rel = (cfg.get("paths") or {}).get("audit", "audit.jsonl")
    path = os.path.join(HERE, rel)
    record = dict(record)
    record.setdefault("time", wxcore.now_str())
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return path
    except Exception:
        return None


def scan_sensitive(text, patterns):
    hits = []
    for p in patterns or []:
        try:
            m = re.search(p, text or "", re.IGNORECASE)
            if m:
                hits.append({"pattern": p, "match": m.group(0)[:40]})
        except re.error:
            continue
    return hits


def clipboard_get():
    try:
        import pyperclip
        return pyperclip.paste()
    except Exception:
        return None


def clipboard_set(text):
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        return False


def readback(br, text, timeout=6.0):
    """发完读一次控件树，确认消息出现在消息列表里（不碰数据库）。"""
    key = (text or "").strip()[:20]
    if not key:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msgs = br.read_current_messages(limit=15)
        except Exception:
            msgs = []
        for m in msgs:
            if key and key in (m.get("text") or ""):
                return True
        time.sleep(0.8)
    return False


def main():
    ap = argparse.ArgumentParser(description="发送一条微信消息（默认只允许发给自己）")
    ap.add_argument("--text", default="", help="要发送的内容")
    ap.add_argument("--text-file", default="",
                    help="从 UTF-8 文件读取要发的内空（避免中文在命令行里转义出问题）")
    ap.add_argument("--to", default="", help="目标会话名（默认发到当前已打开的会话）")
    ap.add_argument("--confirm", action="store_true", help="真的发送；不加则只干跑")
    ap.add_argument("--allow-any-target", action="store_true",
                    help="允许发到白名单以外的会话（危险）")
    ap.add_argument("--no-readback", action="store_true", help="跳过发送后回读验证")
    ap.add_argument("--force", action="store_true",
                    help="跳过「主人正在用电脑」的空闲闸门（默认要求空闲 ≥ --min-idle 秒）")
    ap.add_argument("--min-idle", type=float, default=None,
                    help="空闲闸门阈值（秒），默认取 config.send.min_user_idle_sec 或 5")
    args = ap.parse_args()

    cfg = load_config()
    send_cfg = cfg.get("send") or {}
    stop_path = os.path.join(HERE, (cfg.get("paths") or {}).get("stop", "STOP"))
    if args.text_file:
        with open(args.text_file, encoding="utf-8") as f:
            text = f.read().strip()
    else:
        text = (args.text or "").strip()
    if not text:
        print("⛔ 要发送的内容为空（用 --text 或 --text-file）")
        return 1

    print("=" * 74)
    # ---- 1. 急停 ----
    if os.path.exists(stop_path):
        print("⛔ 急停开关已打开：%s —— 拒绝运行" % stop_path)
        audit(cfg, {"action": "send", "to": args.to, "text": text,
                    "ok": False, "blocked_by": "STOP"})
        return 3

    # ---- 2. 目标白名单：state/whitelist.json（正式名单）+ send.allowed_targets（测试用） ----
    try:
        import guard as _guard
        wl_names = [e.get("name") for e in _guard.load_whitelist(cfg) if e.get("name")]
    except Exception:
        wl_names = []
    allowed = list(dict.fromkeys(wl_names + (send_cfg.get("allowed_targets") or [])))
    print("📋 允许的目标（白名单 %d + 测试名单）：%s" % (len(wl_names), allowed))
    if args.to and args.to not in allowed and not args.allow_any_target:
        _matched = any(args.to == a or a in args.to or args.to in a for a in allowed)
        if not _matched:
            print("⛔ 目标 %r 不在白名单里 —— 拒绝发送。" % args.to)
            print("   要发给它，先：python whitelist.py add \"%s\"" % args.to)
            print("   或者明确接受风险加 --allow-any-target。")
            audit(cfg, {"action": "send", "to": args.to, "text": text,
                        "ok": False, "blocked_by": "whitelist"})
            return 5

    # ---- 3. 脱敏 ----
    red = cfg.get("redaction") or {}
    if red.get("enabled", True):
        hits = scan_sensitive(text, red.get("patterns"))
        if hits and red.get("block_on_hit", True):
            print("⛔ 脱敏校验命中，已中止：")
            for h in hits:
                print("     规则 %r 命中 %r" % (h["pattern"], h["match"]))
            audit(cfg, {"action": "send", "to": args.to, "text": text,
                        "ok": False, "blocked_by": "redaction", "hits": hits})
            return 6
        if hits:
            print("⚠️ 脱敏命中但未开启阻断：%r" % hits)

    print("✍️ 内容：%s" % text)
    print("🎯 目标：%s" % (args.to or "（当前已打开的会话）"))
    print("🔒 verify=False（绝不碰数据库）")

    # ---- 干跑 ----
    if not args.confirm:
        print("-" * 74)
        print("🟡 干跑模式：什么都没做。确认无误后加 --confirm 真发。")
        return 0

    # ---- 3.5 空闲闸门：主人刚动过键鼠就绝不动手（防抢焦点）----
    min_idle = args.min_idle
    if min_idle is None:
        min_idle = float(send_cfg.get("min_user_idle_sec", 5.0))
    prev_fg = _fg_window()
    if not args.force:
        try:
            idle = float(wxcore.user_idle_sec())
        except Exception:
            idle = -1.0
        if idle >= 0 and idle < min_idle:
            print("✋ 主人正在使用电脑（键鼠空闲 %.1fs < %.1fs）→ 本次不动手，不抢焦点。"
                  % (idle, min_idle))
            print("   确实要发就加 --force")
            audit(cfg, {"action": "send", "to": args.to, "text": text,
                        "ok": False, "blocked_by": "user-active", "idle_sec": idle})
            return 8
        print("✅ 空闲 %.1fs ≥ %.1fs —— 主人没在操作电脑，可以动手（发送后还原前台）"
              % (idle, min_idle))

    # ---- 4. 连微信 ----
    print("-" * 74)
    br = WeChatBridge(flat_cfg(cfg), verbose=True)
    try:
        ok, why = br.ensure_ready(activate=True)
        if not ok:
            print("❌ 未就绪：%s" % why)
            audit(cfg, {"action": "send", "to": args.to, "text": text,
                        "ok": False, "blocked_by": "not-ready", "detail": why})
            return 2
        print("✅ 就绪：%s" % why)

        drv = br.pinned_driver()
        if drv is None:
            print("❌ 拿不到钉住窗口的驱动器")
            return 2
        print("🔧 已把库的窗口指针钉到 hwnd=%d（绕开它的选窗 bug）" % br.hwnd)

        cur = br.current_chat(drv)
        print("💬 当前会话：%r" % cur)

        # ---- 5. 切换会话（如果需要）----
        if args.to and cur != args.to:
            print("➡️ 切换到目标会话：%s" % args.to)
            t = time.time()
            # 用 session_item_<名> 精确定位点击；实测比上游的搜索式 open_chat
            # 快得多也可靠得多（0.5s vs 25.6s，且含 emoji 的群名搜索会失败）
            switched, why3 = br.open_session(args.to)
            print("   切换%s：%s（%.1fs）" % ("成功" if switched else "失败", why3, time.time() - t))
            if not switched:
                # 兜底：退回上游的搜索式切换
                print("   回退上游 open_chat ……")
                switched = drv.open_chat(args.to)
                print("   回退结果：%s" % switched)
            if not switched:
                audit(cfg, {"action": "send", "to": args.to, "text": text,
                            "ok": False, "blocked_by": "switch-failed"})
                return 7

        # ---- 6. 窗口必须真实可见（否则点击落到虚空）----
        ok, why = br.restore_window()
        print("🪟 %s：%s" % ("窗口已就绪" if ok else "窗口还原失败", why))
        if not ok:
            audit(cfg, {"action": "send", "to": args.to, "text": text,
                        "ok": False, "blocked_by": "window-not-restored"})
            return 7

        # ---- 7. 剪贴板先存后还 ----
        saved = clipboard_get() if send_cfg.get("restore_clipboard", True) else None
        t = time.time()
        try:
            sent = drv.send_text(text)
        finally:
            if send_cfg.get("restore_clipboard", True) and saved is not None:
                clipboard_set(saved)
                print("📋 剪贴板已还原")
        print("📤 发送%s（%.1fs）" % ("成功" if sent else "失败", time.time() - t))

        verified = None
        if sent and send_cfg.get("readback_verify", True) and not args.no_readback:
            verified = readback(br, text)
            print("🔍 回读验证：%s" % ("✅ 消息已出现在聊天列表" if verified
                                      else "⚠️ 未在列表中确认到（可能发送失败或未及时刷新）"))

        try:
            chat_after = br.current_chat(drv)
        except Exception:
            chat_after = None
        audit(cfg, {"action": "send", "to": args.to,
                    "chat_before": cur, "chat_after": chat_after,
                    "text": text, "ok": bool(sent), "readback": verified,
                    "auto": False})
        return 0 if sent else 7
    finally:
        # 把前台窗口还给主人（我们为了发送把微信提到了前面）
        _restore_fg(prev_fg)
        print("↩️ 前台窗口已还原" if prev_fg else "（无前台窗口可还原）")
        br.close()


if __name__ == "__main__":
    sys.exit(main())
