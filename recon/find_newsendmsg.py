"""两级 xref：定位 newsendmsg 字符串的引用者（只读磁盘）。

直接 lea 引不到时，字符串通常挂在 .rdata/.data 的**指针表**里：
  1) 在 .rdata/.data 里找存放「字符串绝对地址」的 8 字节指针槽
  2) 在 .text 里找 rip 相对引用这些指针槽的指令
  3) 同时也接受直接引用（字符串 RVA 附近 ±0x200 容差）
"""
import struct
import sys

import pefile

DLL = r"<微信安装目录>"
NEEDLE = b"/cgi-bin/micromsg-bin/newsendmsg"
NEEDLE2 = b"newsendmsg"


def main():
    pe = pefile.PE(DLL, fast_load=True)
    base = pe.OPTIONAL_HEADER.ImageBase
    secs = [(s.Name.rstrip(b"\x00").decode("ascii", "replace"), s.VirtualAddress,
             s.Misc_VirtualSize, s.PointerToRawData, s.SizeOfRawData)
            for s in pe.sections]
    text = next(s for s in secs if s[0] == ".text")
    _tn, t_va, t_vsz, t_pr, t_ssz = text
    with open(DLL, "rb") as f:
        f.seek(t_pr)
        code = f.read(t_ssz)
    with open(DLL, "rb") as f:
        allblob = f.read()

    def off_to_rva(off):
        return pe.get_rva_from_offset(off)

    def rva_to_off(rva):
        for _nm, va, vsz, pr, ssz in secs:
            if va <= rva < va + max(vsz, ssz):
                return pr + (rva - va)
        return None

    # ---- 1. 找字符串 ----
    strs = []
    for nd in (NEEDLE, NEEDLE2):
        p = 0
        while True:
            i = allblob.find(nd, p)
            if i < 0:
                break
            rva = off_to_rva(i)
            strs.append((nd.decode("ascii", "replace"), rva, i))
            p = i + 1
    print("=== 字符串")
    for nm, rva, off in strs:
        print(f"  {nm:<34} RVA {rva:#x}  file {off:#x}  (绝对 {base+rva:#x})")

    # ---- 2. 找指针槽 ----
    print("\n=== 指针槽（.rdata/.data 里存放字符串绝对地址的 8 字节）")
    slots = {}
    for nm, rva, _off in strs:
        for target_val in (base + rva, rva):
            pat = struct.pack("<Q", target_val)
            for sname, va, vsz, pr, ssz in secs:
                if sname not in (".rdata", ".data", "_RDATA"):
                    continue
                p = 0
                while True:
                    i = allblob.find(pat, pr, pr + ssz)
                    if i < 0:
                        break
                    slot_rva = off_to_rva(i)
                    slots.setdefault(slot_rva, set()).add(f"{nm}<-{sname}")
                    p = i + 1
    if not slots:
        print("  没找到指针槽 —— 字符串可能是内联在更大的结构/数组中")
    for srva in sorted(slots):
        print(f"  slot RVA {srva:#x}  (绝对 {base+srva:#x})  指向 {sorted(slots[srva])}")

    # ---- 3. 扫 .text 的 rip 相对引用 ----
    targets = {}
    for nm, rva, _off in strs:
        for delta in range(-0x200, 0x40):
            targets.setdefault((rva + delta) & 0xFFFFFFFF, set()).add(nm)
    for srva in slots:
        targets.setdefault(srva, set()).add("PTR-SLOT")

    print(f"\n=== 扫 .text（{len(targets)} 个目标地址，含 ±0x200 容差）")
    hit = []
    rex_set = bytes([0x48, 0x49, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F])
    for op, kind in ((0x8D, "lea"), (0x8B, "mov"), (0x89, "mov-store")):
        for rx in rex_set:
            pat = bytes([rx, op])
            p = 0
            while True:
                i = code.find(pat, p)
                if i < 0:
                    break
                p = i + 1
                if i + 7 > len(code):
                    continue
                modrm = code[i + 2]
                if (modrm >> 6) != 0 or (modrm & 7) != 5:
                    continue
                disp = struct.unpack_from("<i", code, i + 3)[0]
                tgt = (t_va + i + 7 + disp) & 0xFFFFFFFFFFFFFFFF
                if tgt in targets:
                    hit.append((kind, t_va + i, base + t_va + i, tgt,
                                sorted(targets[tgt])))
    if not hit:
        print("  无引用。")
    seen = set()
    for kind, rva, abso, tgt, names in sorted(hit, key=lambda x: x[1]):
        key = (rva, kind)
        if key in seen:
            continue
        seen.add(key)
        print(f"  {kind:<9} @RVA {rva:#010x} 绝对 {abso:#014x}  ->  "
              f"目标 {tgt:#x} {names}")
    pe.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
