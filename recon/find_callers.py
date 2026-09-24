"""静态找 0x39EC560 的调用者（纯磁盘分析，零注入）。

近调用 `E8 rel32` / 尾跳 `E9 rel32` 的目标 = 目标 RVA 的，就是调用者。
再用 .pdata 把每个调用者归到它的函数，并解析函数引用的字符串来识别身份。
"""
import re
import struct
import sys

import pefile

DLL = r"<微信安装目录>"
TARGET = int(sys.argv[1], 16) if len(sys.argv) > 1 else 0x39EC560

REX = bytes([0x48, 0x49, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F])
ASCII_RUN = re.compile(rb"[\x20-\x7e]{6,}")


def main():
    pe = pefile.PE(DLL, fast_load=True)
    base = pe.OPTIONAL_HEADER.ImageBase
    secs = {s.Name.rstrip(b"\x00").decode("ascii", "replace"): s for s in pe.sections}
    text = secs[".text"]
    t_va, t_pr, t_ssz = text.VirtualAddress, text.PointerToRawData, text.SizeOfRawData
    with open(DLL, "rb") as f:
        f.seek(t_pr)
        code = f.read(t_ssz)

    # ---- 1) 找调用者 ----
    callers = []
    for op in (0xE8, 0xE9):          # call rel32 / jmp rel32
        p = 0
        while True:
            i = code.find(bytes([op]), p)
            if i < 0:
                break
            p = i + 1
            if i + 5 > len(code):
                continue
            rel = struct.unpack_from("<i", code, i + 1)[0]
            tgt = (t_va + i + 5 + rel) & 0xFFFFFFFFFFFFFFFF
            if tgt == TARGET:
                callers.append((t_va + i, "call" if op == 0xE8 else "jmp"))
    print(f"目标 0x{TARGET:x} 的调用者：{len(callers)} 个")
    for rva, kind in callers:
        print(f"  {kind} @ RVA {rva:#010x}  绝对 {base+rva:#014x}")

    # ---- 2) .pdata 函数边界 ----
    pd = secs[".pdata"]
    with open(DLL, "rb") as f:
        f.seek(pd.PointerToRawData)
        raw = f.read(pd.SizeOfRawData)
    funcs = []
    for i in range(0, len(raw) - 11, 12):
        b, e, _u = struct.unpack_from("<III", raw, i)
        if b and e > b:
            funcs.append((b, e))
    funcs.sort()

    def containing(rva):
        lo, hi = 0, len(funcs) - 1
        while lo <= hi:
            m = (lo + hi) // 2
            b, e = funcs[m]
            if rva < b:
                hi = m - 1
            elif rva >= e:
                lo = m + 1
            else:
                return funcs[m]
        return None

    # ---- 3) rdata 字符串映射 ----
    rd = secs[".rdata"]
    with open(DLL, "rb") as f:
        f.seek(rd.PointerToRawData)
        rdata = f.read(rd.SizeOfRawData)
    strmap = {}
    for m in ASCII_RUN.finditer(rdata):
        va = base + rd.VirtualAddress + m.start()
        strmap[va] = m.group(0).decode("ascii", "replace")

    # ---- 4) 每个调用者函数：引用什么字符串 ----
    seen_funcs = set()
    for crva, kind in callers:
        f = containing(crva)
        if not f or f in seen_funcs:
            continue
        seen_funcs.add(f)
        fb, fe = f
        print(f"\n=== 调用者函数 {base+fb:#x} (RVA {fb:#x}, {fe-fb} B)"
              f"   [调用点在 +0x{crva-fb:x}]")
        # 函数内 rip 相对引用 → 字符串
        refs = {}
        lo = max(fb - t_va, 0)
        hi = min(fe - t_va, len(code) - 7)
        for op in (0x8D, 0x8B):
            for rx in REX:
                pat = bytes([rx, op])
                p = lo
                while True:
                    i = code.find(pat, p, hi)
                    if i < 0:
                        break
                    p = i + 1
                    if i + 7 > len(code):
                        continue
                    modrm = code[i + 2]
                    if (modrm >> 6) != 0 or (modrm & 7) != 5:
                        continue
                    disp = struct.unpack_from("<i", code, i + 3)[0]
                    tgt = base + t_va + i + 7 + disp
                    s = strmap.get(tgt)
                    if s:
                        refs.setdefault(s, 0)
                        refs[s] += 1
        if not refs:
            print("    （无字符串引用）")
        for s in sorted(refs)[:25]:
            print(f"    {s[:120]!r}")
    pe.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
