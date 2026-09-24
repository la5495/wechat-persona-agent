"""把一个绝对地址（或 RVA）归到 .pdata 里的函数，并列出其字符串引用。"""
import re
import struct
import sys

import pefile

DLL = r"<微信安装目录>"
REX = bytes([0x48, 0x49, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F])
ASCII_RUN = re.compile(rb"[\x20-\x7e]{6,}")


def main():
    addr = int(sys.argv[1], 16)
    rt_base = int(sys.argv[2], 16) if len(sys.argv) > 2 else 0x7FFD8FF80000
    pe = pefile.PE(DLL, fast_load=True)
    base = pe.OPTIONAL_HEADER.ImageBase
    # 运行时绝对地址 → RVA（用运行时基址减，不是磁盘首选基址）
    rva = (addr - rt_base) & 0xFFFFFFFFFFFFFFFF if addr >= rt_base else addr
    secs = {s.Name.rstrip(b"\x00").decode("ascii", "replace"): s for s in pe.sections}

    # .pdata
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

    def containing(r):
        lo, hi = 0, len(funcs) - 1
        while lo <= hi:
            m = (lo + hi) // 2
            b, e = funcs[m]
            if r < b:
                hi = m - 1
            elif r >= e:
                lo = m + 1
            else:
                return funcs[m]
        return None

    f = containing(rva)
    if not f:
        print(f"地址 0x{addr:x} (RVA 0x{rva:x}) 不在任何函数里")
        return 2
    fb, fe = f
    print(f"地址 0x{addr:x} = 函数 {base+fb:#x} (RVA {fb:#x}) "
          f"内偏移 +0x{rva-fb:x}（共 {fe-fb} B）")

    # 字符串引用
    text = secs[".text"]
    t_va, t_pr, t_ssz = text.VirtualAddress, text.PointerToRawData, text.SizeOfRawData
    rd = secs[".rdata"]
    with open(DLL, "rb") as f:
        f.seek(rd.PointerToRawData)
        rdata = f.read(rd.SizeOfRawData)
    strmap = {}
    for m in ASCII_RUN.finditer(rdata):
        va = base + rd.VirtualAddress + m.start()
        strmap[va] = m.group(0).decode("ascii", "replace")
    with open(DLL, "rb") as f:
        f.seek(t_pr)
        code = f.read(t_ssz)
    refs = {}
    lo, hi = max(fb - t_va, 0), min(fe - t_va, len(code) - 7)
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
    print("字符串引用：")
    for s in sorted(refs)[:30]:
        print(f"    {s[:120]!r}")
    pe.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
