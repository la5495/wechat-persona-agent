"""wxsnap -- WeChat 4.x 只读快照工具（只操作副本）

思路：
    原库 (xwechat_files) 只碰一次 —— 拷贝成 snapshot/
    之后所有解密/读取都在副本上做，产出 plain/ 明文副本
    原库不会被写、不会被锁、不会被改

用法：
    python wxsnap.py keys                 # 只读进程内存，检查能拿到密钥（不落盘）
    python wxsnap.py snapshot             # 拷贝原库 -> snapshot/（含 -wal/-shm）
    python wxsnap.py decrypt              # 解密 snapshot/ -> plain/（含 WAL 合并）
    python wxsnap.py refresh              # snapshot + decrypt
    python wxsnap.py verify               # 对 plain/ 全部做 PRAGMA quick_check
    python wxsnap.py sessions             # 会话列表（未读/预览/时间）
    python wxsnap.py messages <会话名或ID> [条数]
    python wxsnap.py sql <库相对路径> "<SQL>"

依赖：cryptography、zstandard
安全：
    * 只读进程内存（OpenProcess(VM_READ) + ReadProcessMemory + VirtualQueryEx）
      —— 不写进程、不注入、不 hook
    * 密钥绝不打印、绝不写入磁盘（只活在本次进程内存里）
    * 原库目录只读；一切写入都落在本工作区
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import hmac as hmac_mod
import json
import os
import re
import shutil
import sqlite3
import struct
import sys
import time
from ctypes import wintypes

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ---------------------------------------------------------------------------
# constants (SQLCipher 4 as used by WeChat 4.x / WCDB)
# ---------------------------------------------------------------------------
PAGE_SZ = 4096
RESERVE_SZ = 80                 # IV(16) + HMAC-SHA512(64)
WAL_HEADER_SZ = 32
WAL_FRAME_SZ = 4120             # 24-byte frame header + 4096-byte encrypted page

CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
HEX_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")

HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT = os.path.join(HERE, "snapshot", "db_storage")
PLAIN = os.path.join(HERE, "plain", "db_storage")

# 原库位置 —— 本机实测；换机器只改这里（或 set WXSNAP_SRC）
DEFAULT_SRC = (r"<微信数据目录>"
               r"\wxid_你的wxid_b422\db_storage")
SRC = os.environ.get("WXSNAP_SRC", DEFAULT_SRC)

PROC_NAME = "Weixin.exe"

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.POINTER(ctypes.c_size_t)]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                               ctypes.c_void_p, ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p),
                ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD),
                ("__a1", wintypes.DWORD),
                ("RegionSize", ctypes.c_size_t),
                ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("__a2", wintypes.DWORD)]


# ---------------------------------------------------------------------------
# crypto
# ---------------------------------------------------------------------------
def pbkdf2(passwd: bytes, salt: bytes, iters: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", passwd, salt, iters, dklen=32)


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return d.update(data) + d.finalize()


def verify_key(key: bytes, page1: bytes, salt: bytes | None = None) -> bool:
    """SQLCipher4 page-1 HMAC-SHA512 —— 密码学强校验，零误报。

    salt 为 None 时用文件头前 16 字节（标准形式）；显式传入用于
    「明文头模式」的 48 字节 key+salt 候选。
    """
    if len(page1) < PAGE_SZ:
        return False
    if salt is None:
        salt = page1[:16]
    elif len(salt) != 16:
        return False
    mac_key = pbkdf2(key, bytes(b ^ 0x3A for b in salt), 2)
    hm = hmac_mod.new(mac_key, page1[16:PAGE_SZ - RESERVE_SZ + 16], hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == page1[PAGE_SZ - 64:PAGE_SZ]


def decrypt_page(key: bytes, page: bytes, pgno: int) -> bytes:
    iv = page[PAGE_SZ - RESERVE_SZ:PAGE_SZ - RESERVE_SZ + 16]
    if pgno == 1:
        return (b"SQLite format 3\x00"
                + aes_cbc_decrypt(key, iv, page[16:PAGE_SZ - RESERVE_SZ])
                + b"\x00" * RESERVE_SZ)
    return (aes_cbc_decrypt(key, iv, page[:PAGE_SZ - RESERVE_SZ])
            + b"\x00" * RESERVE_SZ)


# ---------------------------------------------------------------------------
# process memory (read-only)
# ---------------------------------------------------------------------------
def find_pids(name: str) -> list[int]:
    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    snap = k32.CreateToolhelp32Snapshot(0x02, 0)
    if not snap or snap == (1 << 64) - 1:
        return []
    out = []
    try:
        pe = PE32()
        pe.dwSize = ctypes.sizeof(pe)
        ok = k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            if pe.szExeFile.lower() == name.lower():
                out.append(pe.th32ProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        k32.CloseHandle(snap)
    return out


def open_proc(pid: int):
    """VM_READ | QUERY_INFORMATION —— 只读，实测在 workspace-write 下可用。"""
    return k32.OpenProcess(0x0010 | 0x0400, False, pid)


def make_reader(h):
    def read(addr: int, n: int):
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t(0)
        if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n,
                                 ctypes.byref(got)) and got.value:
            return buf.raw[:got.value]
        return None
    return read


def find_bytes(h, read, needle: bytes) -> list[int]:
    hits, addr = [], 0
    while True:
        mbi = MBI()
        if k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                              ctypes.sizeof(mbi)) == 0:
            break
        if (mbi.State == 0x1000 and (mbi.Protect & 0xFF) & 0xE6
                and not (mbi.Protect & 0x100) and 0 < mbi.RegionSize < 0x10000000):
            buf = read(mbi.BaseAddress or 0, mbi.RegionSize)
            if buf:
                base, pos = mbi.BaseAddress or 0, 0
                while True:
                    pos = buf.find(needle, pos)
                    if pos < 0:
                        break
                    hits.append(base + pos)
                    pos += 1
        addr = (mbi.BaseAddress or 0) + mbi.RegionSize
    return hits


def collect_key_material(pid: int) -> set[tuple[bytes, bytes | None]]:
    """扫内存里的 Config.Cipher 对象，取出候选库密钥材料。不做账号校验。"""
    h = open_proc(pid)
    if not h:
        return set()
    out: set[tuple[bytes, bytes | None]] = set()
    try:
        read = make_reader(h)
        needles = find_bytes(h, read, CONFIG_CIPHER_NAME)
        if not needles:
            return set()
        pairs = [struct.pack("<Q", a) + struct.pack("<Q", len(CONFIG_CIPHER_NAME))
                 for a in needles]
        seen: set[bytes] = set()
        for pair in pairs:
            for qaddr in find_bytes(h, read, pair):
                node = read(qaddr - 0x10, 0x50)
                if not node or len(node) < 0x40:
                    continue
                if struct.unpack_from("<Q", node, 0x10)[0] not in needles:
                    continue
                if struct.unpack_from("<Q", node, 0x18)[0] != len(CONFIG_CIPHER_NAME):
                    continue
                cfg_ptr = struct.unpack_from("<Q", node, 0x28)[0]
                if not (0x10000 <= cfg_ptr < 0x800000000000):
                    continue
                obj = read(cfg_ptr + 0x88, 0x28)
                if not obj or len(obj) < 0x18:
                    continue
                data_ptr = struct.unpack_from("<Q", obj, 0x8)[0]
                data_len = struct.unpack_from("<Q", obj, 0x10)[0]
                if not (0 < data_len <= 1024 and 0x10000 <= data_ptr < 0x800000000000):
                    continue
                blob = read(data_ptr, int(data_len))
                if not blob or len(blob) != data_len:
                    continue
                decoded = bytes(v ^ CONFIG_XOR_MASK[i % len(CONFIG_XOR_MASK)]
                                for i, v in enumerate(blob))
                for m in HEX_LITERAL_RE.finditer(decoded):
                    run = m.group(1).decode().lower()
                    starts = [0]
                    if len(run) > 96:
                        starts += list(range(0, len(run) - 63, 32))
                        starts.append(len(run) - 64)
                    for s in dict.fromkeys(starts):
                        if s + 64 > len(run):
                            continue
                        cand = bytes.fromhex(run[s:s + 64])
                        if len(set(cand)) < 15 or cand in seen:
                            continue
                        seen.add(cand)
                        out.add((cand, None))
                        if s + 96 <= len(run):
                            out.add((cand, bytes.fromhex(run[s + 64:s + 96])))
    finally:
        k32.CloseHandle(h)
    return out


def extract_keys_for(todo: list[tuple[str, str]]) -> dict[str, bytes]:
    """对 [(rel, snapshot_path), ...] 逐个 HMAC 校验候选材料，返回 {rel: key}。"""
    workers = []
    cands: set[tuple[bytes, bytes | None]] = set()
    for pid in find_pids(PROC_NAME):
        got = collect_key_material(pid)
        if got:
            workers.append((pid, len(got)))
            cands |= got
    print(f"  微信进程 {find_pids(PROC_NAME)} | 有密钥的 {workers} | "
          f"候选材料 {len(cands)} 个")
    keys: dict[str, bytes] = {}
    for rel, path in todo:
        if not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            page1 = f.read(PAGE_SZ)
        for cand, salt in cands:
            if verify_key(cand, page1) or (salt and verify_key(cand, page1, salt=salt)):
                keys[rel] = cand
                break
    return keys


# ---------------------------------------------------------------------------
# snapshot / decrypt
# ---------------------------------------------------------------------------
def all_rels(root: str) -> list[str]:
    """所有主库相对路径（不算 -wal/-shm）。"""
    out = []
    for dp, _d, fs in os.walk(root):
        for fn in fs:
            if fn.endswith(".db"):
                out.append(os.path.relpath(os.path.join(dp, fn), root)
                           .replace("\\", "/"))
    return sorted(out)


def compat_check(cfg: dict = None) -> dict:
    """版本适配检查（第 ⑬ 项）：微信升级会改库结构/偏移，先报警别瞎跑。

    返回 {ok, wechat_version, expected, issues:[...], notes:[...]}
    """
    out = {"ok": True, "wechat_version": None, "expected": None,
           "issues": [], "notes": []}
    cfg = cfg or {}
    expected = ((cfg.get("compat") or {}).get("wechat_version")) or None
    out["expected"] = expected

    # 1) 微信可执行文件版本（pywin32）
    try:
        import win32api
        exe = _wechat_exe()
        if exe:
            info = win32api.GetFileVersionInfo(exe, "\\")
            ms, ls = info["FileVersionMS"], info["FileVersionLS"]
            ver = "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
            out["wechat_version"] = ver
            if expected and ver != expected:
                out["ok"] = False
                out["issues"].append(
                    "微信版本 %s ≠ 已知可用版本 %s —— 库结构或内存偏移可能已变，"
                    "先跑 wxsnap.py keys / refresh 验证，别直接开自动回复" % (ver, expected))
        else:
            out["notes"].append("找不到微信主程序（可能没在运行）")
    except Exception as e:
        out["notes"].append("读微信版本失败：%s" % type(e).__name__)

    # 2) 解密副本结构是否还在
    try:
        p = os.path.join(PLAIN, "session", "session.db")
        if os.path.exists(p):
            con = sqlite3.connect(p)
            t = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            con.close()
            if "SessionTable" not in t:
                out["ok"] = False
                out["issues"].append("session.db 里没有 SessionTable —— 库结构变了")
            else:
                out["notes"].append("session.db 结构正常")
        else:
            out["notes"].append("还没有解密副本（先跑 refresh）")

        m = os.path.join(PLAIN, "message", "message_0.db")
        if os.path.exists(m):
            con = sqlite3.connect(m)
            n = len([r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'Msg\\_%' ESCAPE '\\'").fetchall()])
            con.close()
            if n == 0:
                out["ok"] = False
                out["issues"].append("message_0.db 里没有 Msg_* 表 —— 表名规则变了")
            else:
                out["notes"].append("message_0.db 有 %d 张消息表" % n)
    except Exception as e:
        out["notes"].append("结构检查异常：%s" % type(e).__name__)

    # 3) 密钥还能不能拿到
    try:
        todo = [(r, os.path.join(SNAPSHOT, r.replace("/", os.sep)))
                for r in all_rels(SNAPSHOT)][:8] if os.path.isdir(SNAPSHOT) else []
        if todo:
            ks = keys_for(todo)
            if not ks:
                out["ok"] = False
                out["issues"].append("一个密钥都取不到 —— 密钥提取方式可能失效了")
            else:
                out["notes"].append("密钥提取正常（%d/%d）" % (len(ks), len(todo)))
    except Exception as e:
        out["notes"].append("密钥检查异常：%s" % type(e).__name__)
    return out


def _wechat_exe() -> str:
    """找微信主程序路径。

    优先问**正在运行的进程**要（最可靠），找不到再猜常见安装目录。
    """
    # 1) 运行中的进程
    try:
        import psutil
        for p in psutil.process_iter(["name", "exe"]):
            nm = (p.info.get("name") or "").lower()
            if nm in ("weixin.exe", "wechat.exe"):
                exe = p.info.get("exe")
                if exe and os.path.exists(exe):
                    return exe
    except Exception:
        pass
    # 2) 常见安装位置
    import glob
    cands = [
        os.path.expandvars(r"%ProgramFiles%\Tencent\Weixin\Weixin.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Tencent\Weixin\Weixin.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Tencent\Weixin\Weixin.exe"),
        r"C:\Program Files\Tencent\Weixin\Weixin.exe",
        r"D:\Program Files\Tencent\Weixin\Weixin.exe",
        r"D:\Program Files (x86)\Tencent\Weixin\Weixin.exe",
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    for base in (r"C:\Program Files\Tencent", r"D:\Program Files\Tencent",
                 r"D:\Program Files (x86)\Tencent",
                 os.path.expandvars(r"%LOCALAPPDATA%\Tencent")):
        try:
            hit = glob.glob(os.path.join(base, "**", "Weixin.exe"), recursive=True)
            if hit:
                return hit[0]
        except Exception:
            continue
    return ""


def _sig(path: str) -> str:
    """文件的快速指纹（大小 + mtime 纳秒）。变了就说明要重新处理。"""
    try:
        st = os.stat(path)
        return "%d:%d" % (st.st_size, st.st_mtime_ns)
    except OSError:
        return ""


# 进程内密钥缓存：常驻循环里第一轮扫一次内存，之后各轮直接复用
# （扫微信进程内存找密钥要 1-2 秒，是每轮刷新的主要开销）
_KEY_CACHE: dict = {}


def keys_for(todo) -> dict:
    """带缓存的取密钥。解密/合并失败时调用方应 keys_forget(rel) 让它重扫。"""
    need = [(r, s) for r, s in todo if r not in _KEY_CACHE]
    if need:
        got = extract_keys_for(need)
        _KEY_CACHE.update(got)
    return {r: _KEY_CACHE[r] for r, _ in todo if r in _KEY_CACHE}


def keys_forget(rel: str) -> None:
    _KEY_CACHE.pop(rel, None)



def _load_manifest(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_manifest(path: str, obj: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
    except OSError:
        pass


def do_snapshot(incremental: bool = True) -> int:
    """把原库拷到 snapshot/。

    incremental=True（默认）：只拷「大小或修改时间变了」的文件 ——
    实测能把每轮 92MB 的全量拷贝降到几百 KB（WAL 而已）。
    """
    if not os.path.isdir(SRC):
        print(f"原库不存在：{SRC}")
        return 2
    man_path = os.path.join(SNAPSHOT, ".manifest.json")
    man = _load_manifest(man_path) if incremental else {}
    if not incremental and os.path.isdir(SNAPSHOT):
        shutil.rmtree(SNAPSHOT)
        man = {}

    n = copied = total = skipped = 0
    for dp, _d, fs in os.walk(SRC):
        for fn in fs:
            if not re.search(r"\.db(-wal|-shm)?$", fn):
                continue
            src = os.path.join(dp, fn)
            rel = os.path.relpath(src, SRC).replace("\\", "/")
            dst = os.path.join(SNAPSHOT, os.path.relpath(src, SRC))
            sig = _sig(src)
            n += 1
            if incremental and man.get(rel) == sig and os.path.exists(dst):
                skipped += 1
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)          # 原库：只读
            man[rel] = sig
            copied += 1
            total += os.path.getsize(src)
    _save_manifest(man_path, man)
    n_wal = len([1 for dp, _d, fs in os.walk(SNAPSHOT) for f in fs if f.endswith("-wal")])
    print(f"[snapshot] 共 {n} 个文件 ｜ 本次拷 {copied} 个 / {total/1024/1024:.2f} MB "
          f"｜ 跳过未变 {skipped} 个（含 {n_wal} 个 -wal）")
    return 0


def decrypt_db(src: str, dst: str, key: bytes) -> None:
    size = os.path.getsize(src)
    pages = size // PAGE_SZ + (1 if size % PAGE_SZ else 0)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        for pgno in range(1, pages + 1):
            page = fin.read(PAGE_SZ)
            if not page:
                break
            if len(page) < PAGE_SZ:
                page += b"\x00" * (PAGE_SZ - len(page))
            fout.write(decrypt_page(key, page, pgno))


def merge_wal(dst: str, wal_path: str, key: bytes, retries: int = 3) -> int:
    """把 -wal 的加密帧按页号覆盖进已解密的 dst，返回应用的帧数。

    帧结构（WCDB，大端）：[0:4] 页号 [4:8] 提交标记 [8:16] salt [16:24] 校验
    salt 与 WAL 头不一致的帧会被跳过（微信 checkpoint 会重置 salt，
    合并旧世代帧会用过期页覆盖新数据）。
    """
    if not os.path.exists(dst) or not os.path.exists(wal_path):
        return 0
    out = open(dst, "r+b")
    try:
        max_pgno, last = 0, 0
        with open(wal_path, "rb") as wal:
            wal_hdr = wal.read(WAL_HEADER_SZ)
            if len(wal_hdr) < WAL_HEADER_SZ:
                return 0
            wal_salt = wal_hdr[16:24]
            n = (os.path.getsize(wal_path) - WAL_HEADER_SZ) // WAL_FRAME_SZ
            for i in range(n):
                wal.seek(WAL_HEADER_SZ + i * WAL_FRAME_SZ)
                hdr = wal.read(24)
                page = wal.read(PAGE_SZ)
                if len(hdr) < 24 or len(page) < PAGE_SZ:
                    break
                pgno = struct.unpack(">I", hdr[:4])[0]
                last = i + 1
                if hdr[8:16] != wal_salt:
                    continue
                pt = decrypt_page(key, page, pgno)
                if pgno == 1:
                    if pt[:16] != b"SQLite format 3\x00":
                        continue
                elif pt[0] not in (0, 2, 5, 10, 13):
                    continue
                out.seek((pgno - 1) * PAGE_SZ)
                out.write(pt)
                max_pgno = max(max_pgno, pgno)
        out.flush()
        # 修正头部页数（WAL 可能带入超出原页数的页）
        out.seek(0)
        page1 = out.read(PAGE_SZ)
        hdr_pages = struct.unpack(">I", page1[28:32])[0]
        db_pages = (os.path.getsize(dst) + PAGE_SZ - 1) // PAGE_SZ
        new_pages = max(hdr_pages, max_pgno, db_pages)
        if new_pages != hdr_pages:
            out.seek(28)
            out.write(struct.pack(">I", new_pages))
        out.flush()
        return last
    finally:
        out.close()


def quick_check(path: str) -> bool:
    try:
        con = sqlite3.connect(f"file:{path.replace(os.sep, '/')}?mode=ro", uri=True)
        try:
            rows = con.execute("PRAGMA quick_check").fetchall()
        finally:
            con.close()
        return bool(rows) and all(str(r[0]) == "ok" for r in rows)
    except sqlite3.Error:
        return False


def do_decrypt(incremental: bool = True) -> int:
    """解密 snapshot/ → plain/。

    incremental=True（默认）：
      · 源库和它的 -wal 都没变 → 整库跳过（不重复解密）
      · 只有 -wal 变了 → **不再全量解密，只把新 WAL 帧合并进已有明文**（快得多）
      · 源库变了 → 才重新全量解密
    实测：把每轮 5-8 秒降到亚秒级。
    """
    if not os.path.isdir(SNAPSHOT):
        print("还没有副本，先跑 snapshot")
        return 2
    rels = all_rels(SNAPSHOT)
    todo = [(r, os.path.join(SNAPSHOT, r.replace("/", os.sep))) for r in rels]
    man_path = os.path.join(PLAIN, ".manifest.json")
    man = _load_manifest(man_path) if incremental else {}

    # 先筛出真正需要处理的（省掉不必要的密钥校验）
    need = []
    skipped = 0
    for rel, src in todo:
        dst = os.path.join(PLAIN, rel.replace("/", os.sep))
        sig, wal_sig = _sig(src), _sig(src + "-wal")
        prev = man.get(rel) or {}
        if incremental and os.path.exists(dst) and prev.get("sig") == sig \
                and prev.get("wal_sig") == wal_sig:
            skipped += 1
            continue
        need.append((rel, src, sig, wal_sig, prev, dst))

    t0 = time.time()
    if not need:
        print(f"[decrypt] 全部 {len(rels)} 个库都没变，跳过（{time.time()-t0:.1f}s）")
        return 0

    keys = keys_for([(r, s) for r, s, *_ in need])
    print(f"[decrypt] 需要处理 {len(need)}/{len(rels)} 个库（跳过 {skipped}），"
          f"拿到 {len(keys)} 个密钥（{time.time()-t0:.1f}s）")

    ok = 0
    wal_only = 0
    bad = []
    merged_frames = 0
    for rel, src, sig, wal_sig, prev, dst in need:
        key = keys.get(rel)
        if key is None:
            bad.append((rel, "无密钥"))
            continue
        # ---- 只 WAL 变了：合并进已有明文，不重新解密 ----
        if incremental and os.path.exists(dst) and prev.get("sig") == sig \
                and prev.get("wal_sig") != wal_sig:
            try:
                frames = merge_wal(dst, src + "-wal", key)
                if quick_check(dst):
                    man[rel] = {"sig": sig, "wal_sig": wal_sig}
                    ok += 1
                    wal_only += 1
                    merged_frames += frames
                    continue
                # 合并后校验失败 → 落到下面全量重解
            except Exception:
                pass
        # ---- 全量解密 ----
        tmp = dst + ".tmp"
        try:
            decrypt_db(src, tmp, key)
            frames = merge_wal(tmp, src + "-wal", key)
            if quick_check(tmp):
                os.replace(tmp, dst)
                man[rel] = {"sig": sig, "wal_sig": _sig(src + "-wal")}
                ok += 1
                merged_frames += frames
            else:
                bad.append((rel, "quick_check 失败"))
        except Exception as e:
            bad.append((rel, repr(e)))
    _save_manifest(man_path, man)
    print(f"[decrypt] 成功 {ok}（其中仅合并 WAL {wal_only}）｜ 跳过未变 {skipped} ｜ "
          f"WAL 帧 {merged_frames} ｜ {time.time()-t0:.1f}s")
    if bad:
        # *_fts.db 是「可重建的全文索引」，quick_check 一直失败（微信自己也没修），
        # 不算真失败 —— 否则每轮都会误报。
        real = [(r, w) for r, w in bad if not r.endswith("_fts.db")]
        if real:
            print("  失败清单：")
            for rel, why in bad:
                print(f"    {rel}: {why}")
        else:
            print("  （仅有 %d 个 *_fts.db 未通过校验，属已知可重建索引，忽略）" % len(bad))
        return 0 if (ok or not real) else 3
    return 0


# ---------------------------------------------------------------------------
# read layer（只读 plain/）
# ---------------------------------------------------------------------------
def open_plain(rel: str) -> sqlite3.Connection:
    path = os.path.join(PLAIN, rel.replace("/", os.sep))
    if not os.path.exists(path):
        raise SystemExit(f"没有解密副本：{rel}（先跑 decrypt）")
    con = sqlite3.connect(f"file:{path.replace(os.sep, '/')}?mode=ro", uri=True)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    return con


def _tables(con, like: str | None = None) -> list[str]:
    q = "SELECT name FROM sqlite_master WHERE type='table'"
    if like:
        q += f" AND name LIKE '{like}'"
    return [r[0] for r in con.execute(q) if not r[0].startswith("sqlite_")]


def build_name_index(con_msg) -> dict[str, str]:
    """Name2Id: rowid -> user_name"""
    idx = {}
    for row in con_msg.execute("SELECT rowid, user_name FROM Name2Id"):
        idx[str(row[0])] = row[1]
    return idx


def load_contacts() -> dict[str, str]:
    """username -> 显示名（备注优先，其次昵称）。"""
    out = {}
    try:
        con = open_plain("contact/contact.db")
    except SystemExit:
        return out
    try:
        for u, remark, nick in con.execute(
                "SELECT username, remark, nick_name FROM contact"):
            out[u] = (remark or "").strip() or (nick or "").strip() or u
    except sqlite3.Error:
        pass
    con.close()
    return out


def display(contacts: dict[str, str], username: str) -> str:
    if not username:
        return ""
    if username.startswith("_$_CUSTOM_USERNAME_PREFIX_$_"):
        return username.rsplit("_", 1)[-1]
    return contacts.get(username, username)


def chat_table(con_msg, chat: str) -> str | None:
    """会话 id -> Msg_<md5> 表名；也接受直接给 md5/表名。"""
    if chat.startswith("Msg_"):
        return chat
    if re.fullmatch(r"[0-9a-f]{32}", chat):
        return "Msg_" + chat
    if chat == "文件传输助手":
        chat = "filehelper"
    return "Msg_" + hashlib.md5(chat.encode("utf-8")).hexdigest()


def _fmt_ts(v) -> str:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return str(v)
    return (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v))
            if 10 ** 9 < v < 10 ** 10 else str(v))


def cmd_sessions(args) -> int:
    """会话列表：直接读 session.db，不碰 UI、不截图、不动鼠标。"""
    contacts = load_contacts()
    con = open_plain("session/session.db")
    sql = ("SELECT username, unread_count, summary, last_timestamp, last_msg_type,"
           " last_msg_sender, is_hidden FROM SessionTable")
    rows = list(con.execute(sql))
    con.close()
    rows.sort(key=lambda r: int(r[3] or 0), reverse=True)
    if args.unread:
        rows = [r for r in rows if int(r[1] or 0) > 0]
    if args.skip_gh:
        rows = [r for r in rows if not r[0].startswith(("gh_", "brandsessionholder"))]
    rows = rows[:args.n]
    print(f"== 会话列表（{len(rows)} 条，按最近活跃排序）==")
    print(f"{'未读':>4}  {'时间':<19}  {'会话':<26} 预览")
    for u, uc, sm, t_, mt, snd, hid in rows:
        mark = "●" if int(uc or 0) > 0 else " "
        name = display(contacts, u)
        tag = " [群]" if u.endswith("@chatroom") else ""
        s = str(sm or "").replace("\n", " ")[:40]
        print(f"{mark}{uc:>3}  {_fmt_ts(t_):<19}  {(name+tag)[:26]:<26} {s}")
    return 0


def resolve_chat(arg: str, contacts: dict[str, str]) -> str:
    """把用户给的参数（显示名 或 username 或 md5）统一成会话 username。"""
    if arg.startswith("Msg_") or re.fullmatch(r"[0-9a-f]{32}", arg):
        return arg
    if arg in contacts:
        return arg
    if arg == "文件传输助手":
        return "filehelper"
    # 按显示名反查（显示名可能重复，取第一个）
    for u, disp in contacts.items():
        if disp == arg:
            return u
    return arg


MSG_TYPE_NAMES = {
    1: "文本", 3: "图片", 34: "语音", 42: "名片", 43: "视频", 47: "动画表情",
    48: "位置", 49: "链接/文件/卡片", 50: "音视频通话", 10000: "系统消息",
    11000: "动画表情", 8594229559345: "红包",
}


def summarize(local_type, raw: str) -> str:
    """非文本消息给个短标签，不要把一大段 XML 甩出来。"""
    if local_type == 1:
        return raw
    name = MSG_TYPE_NAMES.get(local_type, f"type={local_type}")
    if local_type == 49:
        m = re.search(r"<title>(.*?)</title>", raw, re.S)
        if m:
            return f"[{name}] {m.group(1).strip()[:60]}"
    return f"[{name}]"


def cmd_messages(args) -> int:
    contacts = load_contacts()
    chat = resolve_chat(args.chat, contacts)
    con = open_plain("message/message_0.db")
    tbl = chat_table(con, chat)
    names = _tables(con)
    if tbl not in names:
        near = [t for t in names if t.startswith("Msg_")][:5]
        print(f"找不到表 {tbl}（会话 {args.chat!r} -> {chat!r} 未在此分片）")
        print("  现有 Msg_ 表示例：", ", ".join(near))
        con.close()
        return 2
    idx = build_name_index(con)
    cols = [r[1] for r in con.execute(f'PRAGMA table_info("{tbl}")')]
    total = con.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]
    print(f"== {tbl}  ({display(contacts, chat)})  "
          f"共 {total} 条，显示最新 {args.n} 条 ==")
    rows = list(con.execute(
        f'SELECT * FROM "{tbl}" ORDER BY rowid DESC LIMIT ?', (args.n,)))
    for row in reversed(rows):
        d = dict(zip(cols, row))
        when = _fmt_ts(d.get("create_time"))
        raw = decode_message_content(d.get("message_content"),
                                     d.get("WCDB_CT_message_content"))
        # 群聊正文形如 "wxid_xxx:\n正文" —— 把发送者剥出来
        sender = idx.get(str(d.get("real_sender_id")), str(d.get("real_sender_id")))
        m = re.match(r"^([A-Za-z0-9_\-]+@?[A-Za-z0-9_\-. ]*):\n(.*)$", raw, re.S)
        if m:
            sender, raw = m.group(1), m.group(2)
        raw = summarize(d.get("local_type"), raw)
        print(f"[{when}] {display(contacts, sender)}  (type={d.get('local_type')})")
        for line in raw.splitlines() or [""]:
            print(f"    {line}")
    con.close()
    return 0


def cmd_fresh(args) -> int:
    """全局扫一遍：每个会话表的最新一条消息（用于判断快照新鲜度）。"""
    con = open_plain("message/message_0.db")
    rows = []
    for t in _tables(con):
        if not t.startswith("Msg_"):
            continue
        try:
            r = con.execute(f'SELECT MAX(create_time), COUNT(*) FROM "{t}"').fetchone()
            if r and r[0]:
                rows.append((int(r[0]), t, int(r[1] or 0)))
        except sqlite3.Error:
            pass
    con.close()
    rows.sort(reverse=True)
    print(f"== {len(rows)} 个会话表，最新 {args.n} 个 ==")
    for t_, name, n in rows[:args.n]:
        print(f"   {_fmt_ts(t_)}  {name}  ({n} 条)")
    return 0



def decode_message_content(content, ctype) -> str:
    """消息正文：0=明文；其它=zstd 压缩帧（WCDB 字段级压缩）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        raw = content.encode("utf-8", "replace")
    else:
        raw = bytes(content)
    if ctype in (0, None):
        return raw.decode("utf-8", "replace").strip()
    try:
        import zstandard
        return zstandard.ZstdDecompressor().decompress(
            raw, max_output_size=200000).decode("utf-8", "ignore").strip()
    except Exception:
        try:
            return raw[10:].split(b"\x01\x00")[0].decode("utf-8", "replace").strip()
        except Exception:
            return f"<{len(raw)}B 无法解码>"


def cmd_verify(args) -> int:
    bad = []
    n = 0
    for rel in all_rels(PLAIN):
        p = os.path.join(PLAIN, rel.replace("/", os.sep))
        n += 1
        if not quick_check(p):
            bad.append(rel)
    print(f"[verify] {n} 个库，{n-len(bad)} 个 quick_check 通过")
    for r in bad:
        print("   BAD", r)
    return 0 if not bad else 3


def cmd_sql(args) -> int:
    con = open_plain(args.db)
    cur = con.execute(args.query)
    cols = [d[0] for d in cur.description] if cur.description else []
    print(" | ".join(cols))
    for row in cur.fetchall()[:200]:
        print(" | ".join(
            str(v)[:80] if not isinstance(v, (bytes, bytearray)) else f"<blob {len(v)}B>"
            for v in row))
    con.close()
    return 0


def cmd_keys(args) -> int:
    pids = find_pids(PROC_NAME)
    print(f"微信进程: {pids}")
    cands = set()
    for pid in pids:
        t0 = time.time()
        got = collect_key_material(pid)
        print(f"  pid={pid}: {len(got)} 个候选材料（{time.time()-t0:.1f}s）")
        cands |= got
    print(f"候选合计 {len(cands)}（仅内存，未落盘、未打印）")
    return 0 if cands else 3


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wxsnap", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("keys", cmd_keys),
                     ("snapshot", lambda a: do_snapshot(not getattr(a, "full", False))),
                     ("decrypt", lambda a: do_decrypt(not getattr(a, "full", False))),
                     ("verify", cmd_verify)]:
        pp = sub.add_parser(name)
        if name in ("snapshot", "decrypt"):
            pp.add_argument("--full", action="store_true", help="强制全量（忽略增量缓存）")
        pp.set_defaults(fn=fn)
    p = sub.add_parser("refresh")
    p.add_argument("--full", action="store_true", help="强制全量")
    p.set_defaults(fn=lambda a: do_snapshot(not a.full) or do_decrypt(not a.full))
    p = sub.add_parser("sessions")
    p.add_argument("-n", type=int, default=40, help="显示条数")
    p.add_argument("--unread", action="store_true", help="只显示有未读的")
    p.add_argument("--skip-gh", action="store_true",
                   help="跳过公众号/服务号（gh_*、brandsessionholder）")
    p.set_defaults(fn=cmd_sessions)
    p = sub.add_parser("fresh")
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(fn=cmd_fresh)
    p = sub.add_parser("messages")
    p.add_argument("chat")
    p.add_argument("n", nargs="?", type=int, default=20)
    p.set_defaults(fn=cmd_messages)
    p = sub.add_parser("sql")
    p.add_argument("db")
    p.add_argument("query")
    p.set_defaults(fn=cmd_sql)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
