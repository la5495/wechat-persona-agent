# -*- coding: utf-8 -*-
"""service.py —— 程序开关：启动 / 停止 / 重启 / 看状态 / 看日志

**和微信遥控的分工（主人 2026-09-24 指定，别搞混）**：

| 层 | 谁控制 | 作用 |
|---|---|---|
| **程序层** | 本脚本（只能在本机跑） | 启动/退出那个后台循环**进程** |
| **回复层** | 微信给「文件传输助手」发「停止/开始」 | 进程照跑，只写草稿 ↔ 真发 切换 |

用法::

    python service.py start        # 启动（后台常驻；启动后默认「只写草稿」）
    python service.py stop         # 停止（进程真的退出）
    python service.py restart
    python service.py status       # 活着吗？开关？最近在干嘛？
    python service.py logs [N]     # 看最近 N 行日志（默认 40）
    python service.py once         # 不用常驻，只跑一轮（调试用）

退出码：0 正常 ｜ 2 已经在跑/没在跑 ｜ 3 急停 ｜ 5 微信没开
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, ".venv", "Scripts", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable
STATE = os.path.join(HERE, "state", "service.json")
LOG = os.path.join(HERE, "logs", "service.log")
HEART = os.path.join(HERE, "state", "heartbeat.json")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(int(pid)) and \
            psutil.Process(int(pid)).status() != psutil.STATUS_ZOMBIE
    except Exception:
        try:
            out = subprocess.run(["tasklist", "/FI", "PID eq %s" % pid, "/NH"],
                                 capture_output=True, timeout=15).stdout or b""
            return str(pid).encode() in out
        except Exception:
            return False


def _wechat_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/NH"],
                             capture_output=True, timeout=20).stdout or b""
        return b"Weixin.exe" in out
    except Exception:
        return False


def _guard():
    sys.path.insert(0, HERE)
    from guard import Guard
    cfg = _load(os.path.join(HERE, "config.json"), {}) or {}
    return Guard(cfg, HERE), cfg


def cmd_start(args):
    st = _load(STATE, {}) or {}
    if _alive(st.get("pid")):
        print("ℹ️  已经在跑了（pid=%s，启动于 %s）—— 要重启用 restart"
              % (st.get("pid"), st.get("started_at")))
        return 2
    g, cfg = _guard()
    if g.stopped():
        print("⛔ 急停开关存在（%s）—— 先删掉它" % g.path_stop)
        return 3
    if not _wechat_running():
        print("⚠️  微信没在运行 —— 仍会启动（读不到库时每轮会跳过）")

    # 启动后默认「只写草稿」：开关置为关，等主人发「开始」才真发
    # （--keep-switch 则保留当前开关状态，用于「重启但别丢我的意图」）
    if getattr(args, "keep_switch", False):
        print("   （--keep-switch：保留当前开关 = %s）"
              % ("开（真发）" if g.auto_enabled() else "关（只写草稿）"))
    else:
        g.set_auto(False, by="service-start", paused=False, reason="服务启动，等待「开始」")

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    interval = getattr(args, "interval", None) or \
        int((cfg.get("watch") or {}).get("interval_sec", 20))
    cmd = [PY, "-u", "run.py", "--loop", "--send", "--interval", str(interval)]
    logf = open(LOG, "a", encoding="utf-8")
    logf.write("\n" + "=" * 70 + "\n")
    logf.write("[service] start %s  cmd=%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                 " ".join(cmd)))
    logf.flush()
    proc = subprocess.Popen(cmd, cwd=HERE, stdout=logf, stderr=subprocess.STDOUT,
                            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                            close_fds=True)
    _save(STATE, {"pid": proc.pid, "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "cmd": " ".join(cmd), "log": LOG, "interval": interval,
                  "expected": True})
    time.sleep(2.5)
    ok = _alive(proc.pid)
    print("🚀 已启动（pid=%d，每 %ds 一轮）%s" % (proc.pid, interval, "" if ok else " ⚠️ 进程似乎没起来"))
    if getattr(args, "keep_switch", False) and g.auto_enabled():
        print("   🟢 开关保留为「真发」—— 主人发「停止」可随时暂停")
    else:
        print("   📝 当前是「只写草稿」模式 —— 用微信给「文件传输助手」发「开始」才会真发")
    print("   日志：%s" % LOG)
    return 0 if ok else 2


def cmd_stop(args):
    st = _load(STATE, {}) or {}
    pid = st.get("pid")
    if not _alive(pid):
        print("ℹ️  程序没在跑")
        _save(STATE, {"expected": False})       # 记下「主人主动关的」
        return 2
    print("🛑 正在停止 pid=%s …" % pid)
    subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, timeout=30)
    for _ in range(10):
        time.sleep(0.6)
        if not _alive(pid):
            break
    if _alive(pid):
        print("   温柔停止没成功，强制结束…")
        subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"],
                       capture_output=True, timeout=30)
        time.sleep(1.0)
    alive = _alive(pid)
    # expected=False —— 好让看门狗分清「主人主动关的」和「自己挂了的」
    _save(STATE, dict(st, expected=False) if alive else {"expected": False})
    print("   %s" % ("✅ 已停止" if not alive else "⚠️ 还在？请手动查 tasklist"))
    return 0 if not alive else 2


def health() -> dict:
    """健康检查（给 update.py 的简报和 status 用）。"""
    st = _load(STATE, {}) or {}
    hb = _load(HEART, {}) or {}
    pid = st.get("pid")
    alive = _alive(pid)
    age = (time.time() - hb["ts"]) if hb.get("ts") else None
    expected = st.get("expected")
    g, _cfg = _guard()
    q = _load(os.path.join(HERE, "state", "retry_queue.json"), []) or []
    return {
        "alive": alive, "pid": pid, "expected": expected,
        "heartbeat_age": age, "heartbeat_state": hb.get("state"),
        "wechat": _wechat_running(),
        "auto": g.auto_enabled(), "paused": g.paused(),
        "stopped_file": g.stopped(),
        "retry_queue": len(q),
        "verdict": ("running" if alive and (age is None or age < 120)
                    else "hung" if alive
                    else "crashed" if expected
                    else "stopped"),
    }


def cmd_health(args):
    h = health()
    icon = {"running": "🟢", "hung": "🟠", "crashed": "🔴", "stopped": "⚪"}[h["verdict"]]
    word = {"running": "运行中", "hung": "进程在但心跳停了（可能卡死）",
            "crashed": "⚠️ 本该在跑但已停止（异常退出）", "stopped": "未运行（主人主动关的）"}
    print("%s 程序：%s" % (icon, word[h["verdict"]]))
    if h["heartbeat_age"] is not None:
        print("   心跳：%.0f 秒前（%s）" % (h["heartbeat_age"], h["heartbeat_state"]))
    print("   微信：%s ｜ 开关：%s ｜ 暂停：%s ｜ 待重发：%d 条"
          % ("在运行" if h["wechat"] else "未运行",
             "开" if h["auto"] else "关", "是" if h["paused"] else "否", h["retry_queue"]))
    return 0 if h["verdict"] in ("running", "stopped") else 4


def cmd_status(args):
    st = _load(STATE, {}) or {}
    g, cfg = _guard()
    pid = st.get("pid")
    alive = _alive(pid)
    hb = _load(HEART, {}) or {}
    print("=" * 66)
    print("① 程序层")
    if alive:
        age = ""
        if hb.get("ts"):
            age = " ｜ 最近心跳 %.0f 秒前" % (time.time() - hb["ts"])
        print("   🟢 运行中  pid=%s ｜ 启动于 %s%s" % (pid, st.get("started_at"), age))
        if hb.get("state"):
            print("   状态：%s%s" % (hb["state"],
                                    ("（%s）" % hb["error"]) if hb.get("error") else ""))
    else:
        print("   🔴 未运行（用 python service.py start 启动）")
    print("② 回复层（微信遥控）")
    print("   自动发送开关：%s" % ("🟢 开（真发）" if g.auto_enabled() else "🔴 关（只写草稿）"))
    if g.paused():
        print("   暂停状态：⏸️ 已暂停 —— %s" % g.pause_reason())
        print("   （给「文件传输助手」发「开始」即恢复）")
    else:
        print("   暂停状态：未暂停")
    print("③ 其他")
    print("   急停文件：%s" % ("⛔ 存在（%s）" % g.path_stop if g.stopped() else "正常（无）"))
    print("   白名单　：%s" % g.whitelist_names())
    print("   微信　　：%s" % ("在运行" if _wechat_running() else "未运行"))
    print("   日志　　：%s" % LOG)
    print("=" * 66)
    print("提示：微信发「停止/开始」= 暂停/恢复回复（程序照跑）；")
    print("      本脚本 stop = 程序真的退出。两件事不一样。")
    return 0


def cmd_logs(args):
    n = args.n or 40
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for l in lines[-n:]:
            print(l.rstrip())
    except OSError:
        print("（还没有日志：%s）" % LOG)
    return 0


def cmd_once(args):
    cmd = [PY, "-u", "run.py", "--once", "--send" if args.send else "--no-draft"]
    if not args.send:
        cmd = [PY, "-u", "run.py", "--once"]
    return subprocess.run(cmd, cwd=HERE).returncode


def main():
    ap = argparse.ArgumentParser(description="微信自动回复 · 程序开关")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("start", help="启动后台常驻（默认只写草稿）")
    p.add_argument("--interval", type=int, default=None)
    p.add_argument("--keep-switch", action="store_true",
                   help="保留当前开关状态（默认启动会强制回到「只写草稿」）")
    sub.add_parser("stop", help="停止程序（进程退出）")
    p4 = sub.add_parser("restart", help="重启")
    p4.add_argument("--keep-switch", action="store_true", help="重启时保留开关状态")
    sub.add_parser("status", help="看程序与回复两层状态")
    sub.add_parser("health", help="健康检查（一行结论，给看门狗用）")
    p2 = sub.add_parser("logs", help="看日志")
    p2.add_argument("n", nargs="?", type=int, default=40)
    p3 = sub.add_parser("once", help="只跑一轮（调试）")
    p3.add_argument("--send", action="store_true", help="这一轮允许真发（仍需开关打开）")
    args = ap.parse_args()

    cmd = args.cmd or "status"
    if cmd == "start":
        return cmd_start(args)
    if cmd == "stop":
        return cmd_stop(args)
    if cmd == "restart":
        cmd_stop(args)
        time.sleep(1.0)
        return cmd_start(args)
    if cmd == "status":
        return cmd_status(args)
    if cmd == "health":
        return cmd_health(args)
    if cmd == "logs":
        return cmd_logs(args)
    if cmd == "once":
        return cmd_once(args)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
