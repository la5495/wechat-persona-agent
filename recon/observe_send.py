"""只读观察器：挂上候选函数的观察点，持续监听一段时间（默认 5 分钟）。

用法：
    python recon/observe_send.py --for 300
期间**主人手动**从微信发一条测试消息（建议发给「文件传输助手」）。
观察点只读参数/内存/调用栈，绝不调用或修改微信的发送逻辑。

注入结束会自动 unload/detach，并做微信存活自检。
"""
import argparse
import ctypes
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JS = os.path.join(HERE, "observe_send.js")
LOG = os.path.join(HERE, "observe_send.log")

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
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--for", dest="duration", type=float, default=300.0)
    args = ap.parse_args()

    import frida
    log("=" * 70)
    log(f"只读观察开始 | frida {frida.__version__} | 持续 {args.duration:.0f}s")
    # 内存里累积所有记录，结束后统一写文件
    records = []

    pids = find_pids("Weixin.exe")
    if not pids:
        log("没有微信进程")
        return 2
    pid = args.pid or pids[0]
    log(f"目标 pid={pid}")

    with open(JS, encoding="utf-8") as f:
        source = f.read()

    def on_message(message, data):
        if not isinstance(message, dict):
            return
        if message.get("type") == "error":
            log(f"agent 错误: {message.get('description')}")
            return
        payload = message.get("payload") or {}
        kind = payload.get("kind")
        if kind == "ready":
            log(f"观察点已就绪: {payload}")
        elif kind == "hook-error":
            log(f"挂钩失败: {payload}")
        elif kind == "counts":
            log(f"调用计数: {payload.get('counts')}  已回报 {payload.get('sent')} 条")
        elif kind == "call":
            rec = payload.get("rec") or {}
            records.append(rec)
            strs = [a.get("s") for a in rec.get("args", []) if a.get("s")]
            log(f"CALL {rec.get('fn')} #{rec.get('n')}  strs={strs}")

    dev = frida.get_local_device()
    session = None
    script = None
    try:
        session = dev.attach(pid)
        script = session.create_script(source)
        script.on("message", on_message)
        script.load()
        log("已注入，开始监听 —— 请主人现在去微信发一条测试消息")
        t0 = time.time()
        while time.time() - t0 < args.duration:
            time.sleep(0.5)
        log(f"监听到期，记录 {len(records)} 条")
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

    # 落盘（只留观察到的参数与调用栈，不含任何密钥）
    out = os.path.join(HERE, "observe_send.records.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=1)
    log(f"记录已写入 {out}")
    log(f"微信 pid={pid} 仍存活: {is_alive(pid)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
