"""诊断：frida 在当前沙箱下到底能不能用？

自注入（attach 自己）是最干净的判据：
  成功 -> frida 的管道/助手链路正常，之前看到 0 个进程只是**权限过滤**
  失败 -> 沙箱把 frida 的通信通道掐了，跟权限无关
"""
import os
import sys
import time

import frida

print("frida:", frida.__version__)
print("our pid:", os.getpid(), "elevated-check skipped")

try:
    mgr = frida.get_device_manager()
    devs = mgr.enumerate_devices()
    print("devices:", [(d.id, d.type, d.name) for d in devs])
except Exception as e:
    print("enumerate_devices FAILED:", type(e).__name__, e)

dev = frida.get_local_device()
print("local device:", dev)

try:
    procs = dev.enumerate_processes()
    print(f"enumerate_processes -> {len(procs)} 个")
except Exception as e:
    print("enumerate_processes RAISED:", type(e).__name__, e)

print("\n--- 自注入测试 ---")
try:
    t0 = time.time()
    session = dev.attach(os.getpid())
    print(f"attach(self) OK in {time.time()-t0:.2f}s")
    got = []
    script = session.create_script(
        "send({ok: true, pid: Process.id, arch: Process.arch});"
        "console.log('[self] hello from inside');")
    script.on("message", lambda m, d: got.append(m))
    script.load()
    time.sleep(0.8)
    print("messages:", got)
    script.unload()
    session.detach()
    print("self-injection pipeline: OK")
except Exception as e:
    print("attach(self) FAILED:", type(e).__name__, e)
    print("self-injection pipeline: BROKEN")

print("\n--- frida 助手进程 ---")
try:
    names = sorted({p.name for p in dev.enumerate_processes()})
    print("含 frida 的:", [n for n in names if "frida" in n.lower()] or "无")
except Exception as e:
    print("查不到:", e)
