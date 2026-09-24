"""硬化版观察器 runner。

用法：
    python recon/observe2.py --rva 0x39ec560 --reads 0 --for 240
参数：
    --rva    目标函数 RVA（十六进制字符串）
    --reads  0 = 只记指针值（最安全，先跑这个）；1 = 用 safeRead 读参数
    --for    观察时长秒数

全程：attach -> 等 ready -> 流式记录 -> 到时 unload/detach -> 微信存活自检。
"""
import argparse
import ctypes
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
JS = os.path.join(HERE, "observe2.js")
LOG = os.path.join(HERE, "observe2.log")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def is_alive(pid):
    h = _k32.OpenProcess(0x1000, False, pid)
    if h:
        _k32.CloseHandle(h)
        return True
    return False


def find_pids(name="Weixin.exe"):
    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32),
                    ("th32ProcessID", ctypes.c_uint32),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
                    ("th32ParentProcessID", ctypes.c_uint32),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_uint32),
                    ("szExeFile", ctypes.c_wchar * 260)]
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    snap = k.CreateToolhelp32Snapshot(0x02, 0)
    out = []
    if not snap or snap == (1 << 64) - 1:
        return out
    try:
        pe = PE32()
        pe.dwSize = ctypes.sizeof(pe)
        ok = k.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            if pe.szExeFile.lower() == name.lower():
                out.append(pe.th32ProcessID)
            ok = k.Process32NextW(snap, ctypes.byref(pe))
    finally:
        k.CloseHandle(snap)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rva", required=True)
    ap.add_argument("--js", default=JS, help="agent 文件（默认 observe2.js）")
    ap.add_argument("--reads", type=int, default=0, choices=(0, 1, 2, 3))
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--for", dest="duration", type=float, default=240.0)
    args = ap.parse_args()

    import frida
    log("=" * 70)
    log(f"观察2代开始 | RVA={args.rva} | reads={args.reads} | "
        f"持续 {args.duration:.0f}s | frida {frida.__version__}")

    pids = find_pids("Weixin.exe")
    if not pids:
        log("没有微信进程")
        return 2
    pid = args.pid or pids[0]
    log(f"目标 pid={pid} | 注入前存活: {is_alive(pid)}")

    with open(args.js, encoding="utf-8") as f:
        source = f.read()
    source = source.replace("RVA_PLACEHOLDER", args.rva)
    source = source.replace("READS_PLACEHOLDER", str(args.reads))

    hits = []

    def on_message(message, data):
        if not isinstance(message, dict):
            return
        if message.get("type") == "error":
            log(f"agent 错误: {message.get('description')}")
            return
        payload = message.get("payload") or {}
        if payload.get("kind") == "ready":
            log(f"观察点就绪: {payload}")
        elif payload.get("kind") == "hit":
            rec = payload.get("rec") or {}
            hits.append(rec)
            log(f"HIT #{rec.get('n')} tid={rec.get('tid')} "
                f"caller={rec.get('caller')} "
                f"args=({rec.get('a0')}, {rec.get('a1')}, "
                f"{rec.get('a2')}, {rec.get('a3')})")
            for line in (rec.get("r") or rec.get("d") or []):
                log(f"    {line[:400]}")
            for f in (rec.get("bt") or []):
                log(f"    BT: {f}")
        elif payload.get("kind") == "leave":
            for line in (payload.get("d") or []):
                log(f"    {line[:400]}")
        else:
            # 通用：probe / patched / patch-error 等自定义消息
            log(f"[{payload.get('kind')}] " +
                json.dumps({k: v for k, v in payload.items() if k != "kind"},
                           ensure_ascii=False)[:400])

    dev = frida.get_local_device()
    session = None
    script = None
    try:
        session = dev.attach(pid)
        script = session.create_script(source)
        script.on("message", on_message)
        script.load()
        log("已注入 —— 请主人在微信里发一条测试消息")
        t0 = time.time()
        while time.time() - t0 < args.duration:
            time.sleep(0.5)
        log(f"观察结束，共 {len(hits)} 次命中")
        script.unload()
        session.detach()
        session = None
        log("已干净卸载")
    except Exception as e:
        log(f"失败: {type(e).__name__}: {e}")
        try:
            if session is not None:
                session.detach()
        except Exception:
            pass
        return 3

    time.sleep(1.0)
    alive = is_alive(pid)
    log(f"微信 pid={pid} 仍存活: {alive}")
    out = os.path.join(HERE, "observe2.records.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(hits, f, ensure_ascii=False, indent=1)
    log(f"记录已写入 {out}")
    return 0 if (alive and hits) else (4 if not alive else 3)


if __name__ == "__main__":
    sys.exit(main())
