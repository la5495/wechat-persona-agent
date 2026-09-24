"""微信健康检查：窗口 + 是否仍在写库（登录态判据）。"""
import ctypes
import ctypes.wintypes as w
import os
import sys
import time

u = ctypes.WinDLL("user32", use_last_error=True)
WCB = ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)

PIDS = {20872, 12916, 16132, 16480, 22660}   # 旧 PID（微信重启后失效，下面动态覆盖）
try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from recon.observe2 import find_pids
    _live = find_pids("Weixin.exe")
    if _live:
        PIDS = set(_live)
except Exception:
    pass
out = []


def cb(h, l):
    n = u.GetWindowTextLengthW(h)
    b = ctypes.create_unicode_buffer(512)
    u.GetWindowTextW(h, b, 512)
    cls = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(h, cls, 256)
    p = w.DWORD(0)
    u.GetWindowThreadProcessId(h, ctypes.byref(p))
    if p.value in PIDS:
        out.append((p.value, cls.value, b.value, bool(u.IsWindowVisible(h)), n))
    return True


u.EnumWindows(WCB(cb), 0)
print(f"微信顶层窗口 {len(out)} 个：")
for pid, cls, title, vis, _n in sorted(out, key=lambda x: -len(x[2])):
    print(f"  pid={pid} visible={vis} class={cls!r} title={title!r}")

print("\n窗口标题里像登录/扫码的：")
hits = [o for o in out if any(k in o[2] for k in ("登录", "微信", "Weixin", "WeChat"))]
for o in hits:
    print("  ", o[1], repr(o[2]), "visible=", o[3])
if not hits:
    print("   无（登录页通常是标题 '微信' 的窗口，只有 Qt 空壳）")

print("\n数据库写入活跃度（登录且联网时会持续变）：")
base = r"<微信数据目录>"
now = time.time()
for rel in ["session/session.db", "session/session.db-wal",
            "message/message_0.db-wal", "contact/contact.db-wal"]:
    p = os.path.join(base, rel.replace("/", os.sep))
    if os.path.exists(p):
        age = now - os.path.getmtime(p)
        print(f"  {rel:<28} 最后修改 {age:.0f} 秒前  ({os.path.getsize(p)/1024:.0f} KB)")
    else:
        print(f"  {rel:<28} 不存在")
