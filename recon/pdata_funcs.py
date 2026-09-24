"""用 .pdata 拿函数边界 + 解析函数引用到的字符串（只读磁盘，不需要反汇编器）。

.pdata (IMAGE_DIRECTORY_ENTRY_EXCEPTION) 是 RUNTIME_FUNCTION 数组：
    DWORD BeginAddress; DWORD EndAddress; DWORD UnwindInfoAddress;   (全是 RVA)
每个函数一条 → 可以直接把任意代码地址归到它所属的函数。

再把函数体内的 rip 相对引用解析成 .rdata 里的字符串，就能猜出函数在干什么。
"""
import re
import struct
import sys

import pefile

DLL = r"<微信安装目录>"
CLUSTER = [0x039EC0A3, 0x039EC6E6, 0x039EDED6, 0x039EEA1F, 0x039EEBA8,
           0x039EEC3C, 0x039EECD3, 0x039EEF63, 0x039EEFA3]

REX = bytes([0x48, 0x49, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F])
ASCII_RUN = re.compile(rb"[\x20-\x7e]{6,}")


def rip_refs(code, t_va, lo, hi):
    """返回函数体内所有 rip 相对引用的 (指令RVA, 目标绝对VA)。"""
    out = []
    start = lo - t_va
    end = min(hi - t_va, len(code) - 7)
    for op in (0x8D, 0x8B):
        for rx in REX:
            pat = bytes([rx, op])
            p = start
            while True:
                i = code.find(pat, p, end)
                if i < 0:
                    break
                p = i + 1
                modrm = code[i + 2]
                if (modrm >> 6) != 0 or (modrm & 7) != 5:
                    continue
                disp = struct.unpack_from("<i", code, i + 3)[0]
                tgt = t_va + i + 7 + disp
                out.append((i, tgt))
    return out


def main():
    pe = pefile.PE(DLL, fast_load=True)
    base = pe.OPTIONAL_HEADER.ImageBase
    secs = {s.Name.rstrip(b"\x00").decode("ascii", "replace"): s for s in pe.sections}
    text = secs[".text"]
    t_va, t_pr, t_ssz = text.VirtualAddress, text.PointerToRawData, text.SizeOfRawData
    with open(DLL, "rb") as f:
        f.seek(t_pr)
        code = f.read(t_ssz)

    # ---- .pdata ----
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
    print(f".pdata: {len(funcs)} 个函数条目")


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

    print("\n=== 9 个 lea 各属于哪个函数")
    involved = {}
    for rva in CLUSTER:
        f = containing(rva)
        if not f:
            print(f"  {rva:#x} -> (不在 .pdata 里)")
            continue
        involved.setdefault(f, []).append(rva)
        print(f"  lea {rva:#x}  ->  函数 {base+f[0]:#x} "
              f"(RVA {f[0]:#x}, 大小 {f[1]-f[0]} 字节)  "
              f"偏移 +0x{rva-f[0]:x}")

    # ---- 收集 .rdata 字符串，做目标→字符串映射 ----
    rd = secs[".rdata"]
    with open(DLL, "rb") as f:
        f.seek(rd.PointerToRawData)
        rdata = f.read(rd.SizeOfRawData)
    strmap = {}
    for m in ASCII_RUN.finditer(rdata):
        va = base + rd.VirtualAddress + m.start()
        strmap[va] = m.group(0).decode("ascii", "replace")

    print(f"\n=== 这 {len(involved)} 个函数引用的字符串（看它们在干什么）")
    for (fb, fe) in sorted(involved):
        print(f"\n--- 函数 {base+fb:#x}  (RVA {fb:#x}, {fe-fb} 字节)")
        refs = rip_refs(code, t_va, fb, fe)
        seen = {}
        for insn_off, tgt in refs:
            s = strmap.get(tgt)
            if s:
                seen.setdefault(s, []).append(t_va + insn_off)
        if not seen:
            print("     （没有指向 .rdata 字符串的引用）")
        for s in sorted(seen, key=lambda k: -len(seen[k]))[:22]:
            print(f"     {s[:110]!r}   x{len(seen[s])}")
    pe.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
