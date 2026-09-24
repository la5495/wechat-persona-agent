"""静态侦察 Weixin.dll：导出表 / 导入表 / 特征字符串。

全程只读磁盘文件，**不注入、不碰进程**。
目的：搞清楚"能 hook 什么" —— sqlite3/WCDB 是否可在运行时定位、网络层怎么走、
消息表名和列名是否以字符串形式内嵌（能内嵌就有 xref 可追）。
"""
import os
import re
import sys
from collections import Counter

import pefile

DLL = r"<微信安装目录>"

# 关心什么
EXPORT_HINTS = re.compile(
    r"sqlite|wcdb|send|recv|msg|message|socket|sock|http|wsasend|encrypt|decrypt"
    r"|tls|proto|session|contact|login|mmtls|crypt", re.I)
IMPORT_HINTS = re.compile(
    r"^send$|^recv$|^WSASend|^WSARecv|^WriteFile|^ReadFile|^NtWriteFile"
    r"|^CreateFile|^GetFinalPathName|^SendMessage|^PostMessage|^SetWindowText"
    r"|^GetWindowText|^CreateRemoteThread|^AdjustTokenPrivileges|^OpenProcess"
    r"|^CryptProtect|^closesocket|^connect$|^select$|^LoadLibrary", re.I)

NEEDLES = [
    b"sqlite3_", b"sqlite3_prepare", b"sqlite3_bind", b"sqlite3_step",
    b"CREATE TABLE", b"create table",
    b"INSERT INTO", b"insert into", b"INSERT OR REPLACE",
    b"WCDB", b"WCDBCT", b"wcdb",
    b"message_content", b"packed_info_data", b"WCDB_CT_",
    b"Msg_", b"session_list", b"ChatSessionCell",
    b"SendMsg", b"sendmsg", b"SendText", b"SendTextMsg", b"send_text",
    b"mmtls", b"MMTLS", b"protobuf", b"Protobuf",
    b"wxid_", b"chatroom",
    b"SendMessage", b"WSASend",
    b"global_config", b"com.Tencent.WCDB",
]


def main():
    if not os.path.exists(DLL):
        print("找不到", DLL)
        return 2
    size = os.path.getsize(DLL)
    print(f"=== {DLL}")
    print(f"    大小 {size/1024/1024:.1f} MB")

    pe = pefile.PE(DLL, fast_load=True)
    dirs = [pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]]
    pe.parse_data_directories(directories=dirs)

    # ---------------- exports ----------------
    exps = []
    if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
        for s in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            nm = (s.name or b"").decode("utf-8", "replace")
            exps.append((nm, s.address))
        print(f"\n=== 导出函数 {len(exps)} 个")
        hits = [(n, a) for n, a in exps if EXPORT_HINTS.search(n)]
        print(f"    其中命中关键词的 {len(hits)} 个（前 40）：")
        for n, a in hits[:40]:
            print(f"      {a:#010x}  {n[:100]}")
        if not hits:
            print("      （一个都没有 —— 说明是内部静态链接，导出表帮不上忙）")
        print("    导出名样式样本（前 10）：")
        for n, a in exps[:10]:
            print(f"      {a:#010x}  {n[:80]}")
    else:
        print("\n=== 无导出表")

    # ---------------- imports ----------------
    def dump_imports(label, entries):
        n_imp = 0
        interesting = []
        for entry in entries:
            dll = (entry.dll or b"").decode("utf-8", "replace")
            funcs = []
            for imp in getattr(entry, "imports", []) or []:
                fn = (imp.name or b"").decode("utf-8", "replace")
                funcs.append(fn)
            n_imp += len(funcs)
            for fn in funcs:
                if IMPORT_HINTS.search(fn):
                    interesting.append((dll, fn))
        print(f"\n=== {label}：{len(entries)} 个 DLL / {n_imp} 个函数")
        print(f"    关心的 {len(interesting)} 个：")
        for dll, fn in sorted(set(interesting))[:45]:
            print(f"      {dll}  ->  {fn}")
        return [e.dll.decode('utf-8', 'replace') for e in entries]

    dlls = []
    if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
        dlls = dump_imports("常规导入", pe.DIRECTORY_ENTRY_IMPORT)
    if hasattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT"):
        dump_imports("延迟导入", pe.DIRECTORY_ENTRY_DELAY_IMPORT)
    print(f"\n依赖 DLL（{len(dlls)}）：{', '.join(sorted(set(dlls)))[:600]}")

    # ---------------- strings ----------------
    print(f"\n=== 特征字符串扫描（{len(NEEDLES)} 个模式）")
    counts = Counter()
    first_off = {}
    overlap = 64
    with open(DLL, "rb") as f:
        base = 0
        chunk = f.read(1 << 23)
        while chunk:
            for nd in NEEDLES:
                start = 0
                while True:
                    i = chunk.find(nd, start)
                    if i < 0:
                        break
                    counts[nd] += 1
                    if nd not in first_off:
                        first_off[nd] = base + i
                    start = i + 1
            if not chunk:
                break
            base += len(chunk)
            prev_tail = chunk[-overlap:]
            chunk = f.read(1 << 23)
            if not chunk:
                break
            chunk = prev_tail + chunk
            base -= len(prev_tail)
    for nd in NEEDLES:
        c = counts.get(nd, 0)
        if not c:
            print(f"    {nd.decode('utf-8','replace'):<24} 0")
            continue
        off = first_off[nd]
        try:
            rva = pe.get_rva_from_offset(off)
        except Exception:
            rva = None
        loc = f"首个 @file 0x{off:x}" + (f"  RVA 0x{rva:x}" if rva else "")
        print(f"    {nd.decode('utf-8','replace'):<24} {c:<7} {loc}")

    pe.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
