"""空心针注入器 —— 把 hollow_needle.js 注入 Weixin.exe，收报告，然后干净卸载。

必须**以管理员身份**运行（提权）。原因（实测）：
    我们非提权 -> OpenProcess(VM_WRITE / VM_OPERATION / CREATE_THREAD) 全部 err=5
    Weixin.exe 是已提权进程 -> 非提权进程拿不到它的写权限
所以本脚本要在提权的进程里跑（run_hollow.cmd 由主人点 UAC 确认）。

安全：agent 零 hook、零写入；本脚本 attach -> load -> 收消息 -> unload -> detach。
"""
import argparse
import ctypes
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
JS = os.path.join(HERE, "hollow_needle.js")
LOG = os.path.join(HERE, "hollow_needle.log")

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def is_alive(pid: int) -> bool:
    h = _k32.OpenProcess(0x1000, False, pid)
    if h:
        _k32.CloseHandle(h)
        return True
    return False


def find_pids_ctypes(name: str = "Weixin.exe") -> list[int]:
    """自己用 toolhelp 找 pid。

    为什么不用 frida.enumerate_processes()：本机实测它恒返回 0 个
    （frida winjector 报 "Error setting ACLs"），但注入链路本身是好的
    （自注入完整跑通）。所以 pid 我们自己拿，frida 只负责注入。
    """
    import ctypes as C

    class PE32(C.Structure):
        _fields_ = [("dwSize", C.c_uint32), ("cntUsage", C.c_uint32),
                    ("th32ProcessID", C.c_uint32),
                    ("th32DefaultHeapID", C.POINTER(C.c_ulong)),
                    ("th32ModuleID", C.c_uint32), ("cntThreads", C.c_uint32),
                    ("th32ParentProcessID", C.c_uint32),
                    ("pcPriClassBase", C.c_long), ("dwFlags", C.c_uint32),
                    ("szExeFile", C.c_wchar * 260)]

    k = C.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = C.c_void_p
    k.CreateToolhelp32Snapshot.argtypes = [C.c_uint32, C.c_uint32]
    snap = k.CreateToolhelp32Snapshot(0x02, 0)
    out = []
    if not snap or snap == (1 << 64) - 1:
        return out
    try:
        pe = PE32()
        pe.dwSize = C.sizeof(pe)
        ok = k.Process32FirstW(snap, C.byref(pe))
        while ok:
            if pe.szExeFile.lower() == name.lower():
                out.append(pe.th32ProcessID)
            ok = k.Process32NextW(snap, C.byref(pe))
    finally:
        k.CloseHandle(snap)
    return out


def elevated() -> bool:
    """当前进程是否提权（TokenElevation）。"""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                        ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                           ctypes.c_void_p, ctypes.c_uint32,
                                           ctypes.POINTER(ctypes.c_uint32)]
    ht = ctypes.c_void_p(0)
    if not advapi.OpenProcessToken(ctypes.c_void_p(-1), 0x0008, ctypes.byref(ht)):
        return False
    try:
        val = ctypes.c_uint32(0)
        need = ctypes.c_uint32(0)
        advapi.GetTokenInformation(ht, 20, ctypes.byref(val), 4, ctypes.byref(need))
        return bool(val.value)
    finally:
        _k32.CloseHandle(ht)


def can_write(pid: int) -> tuple[bool, int]:
    h = _k32.OpenProcess(0x0020, False, pid)          # PROCESS_VM_WRITE
    err = ctypes.get_last_error()
    if h:
        _k32.CloseHandle(h)
        return True, 0
    return False, err


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--keep", action="store_true",
                    help="收到报告后不卸载（默认卸载并 detach）")
    args = ap.parse_args()

    log("=" * 68)
    log(f"空心针开始 | python={sys.executable}")
    log(f"当前进程提权状态: elevated={elevated()}")

    try:
        import frida
    except ImportError as e:
        log(f"frida 没装：{e!r}")
        return 5
    log(f"frida 版本: {frida.__version__}")

    device = frida.get_local_device()
    try:
        allprocs = device.enumerate_processes()
    except Exception as e:
        allprocs = []
        log(f"frida 列进程失败（预期，走 ctypes 回退）: {e!r}")
    procs = [p for p in allprocs if p.name.lower().startswith("weixin")]
    log(f"frida 能看到 {len(allprocs)} 个进程；匹配 weixin* 的有 {len(procs)} 个")

    candidates = [p.pid for p in procs] or find_pids_ctypes("Weixin.exe")
    log(f"候选 Weixin pid（frida+ctypes）: {candidates}")
    if not candidates:
        log("没有微信进程，先启动并登录微信")
        return 2
    pid = args.pid or candidates[0]
    writable, err = can_write(pid)
    log(f"目标 pid={pid} | OpenProcess(VM_WRITE) -> "
        f"{'OK' if writable else f'DENIED err={err}'}"
        + ("   <== 没提权，注入一定会失败" if not writable else ""))

    if not os.path.exists(JS):
        log(f"缺 agent 文件: {JS}")
        return 5
    with open(JS, encoding="utf-8") as f:
        source = f.read()

    got = {}

    def on_message(message, data):
        log(f"agent 消息: {json.dumps(message, ensure_ascii=False)[:1200]}")
        if isinstance(message, dict) and message.get("type") == "send":
            payload = message.get("payload") or {}
            if payload.get("kind") == "hollow-needle-report":
                got.update(payload.get("report") or {})

    session = None
    script = None
    try:
        log("attach ...")
        session = device.attach(pid)
        log("create_script ...")
        script = session.create_script(source)
        script.on("message", on_message)
        log("load ...")
        script.load()
        log("已注入。等待 agent 自报 ...")

        t0 = time.time()
        while not got and time.time() - t0 < args.timeout:
            time.sleep(0.2)

        if got:
            log("*** 注入成功，agent 报告 ***")
            for k, v in got.items():
                log(f"    {k} = {v}")
        else:
            log(f"*** {args.timeout:.0f}s 内没收到自报 —— 注入可能被拦或 agent 没跑起来 ***")

        if not args.keep:
            log("unload / detach ...")
            try:
                script.unload()
            except Exception as e:
                log(f"unload 异常: {e!r}")
            session.detach()
            session = None
            log("已干净卸载")
    except Exception as e:
        log(f"注入失败: {type(e).__name__}: {e}")
        try:
            if session is not None:
                session.detach()
        except Exception:
            pass
        log("--- 结尾自检 ---")
        log(f"Weixin pid={pid} 仍然存活: {is_alive(pid)}")
        return 3

    time.sleep(1.0)
    alive = is_alive(pid)
    log("--- 结尾自检 ---")
    log(f"Weixin pid={pid} 仍然存活: {alive}")
    log("空心针结束")
    return 0 if got and alive else (4 if not alive else 3)


if __name__ == "__main__":
    sys.exit(main())
