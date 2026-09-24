"""x64 静态 xref 扫描器（只读磁盘）。

思路：
  1) 在 DLL 里找目标字符串，拿到它的 RVA
  2) 在 .text 段扫 `REX.W LEA reg, [rip+disp32]` （48..4F 8D ModRM=mod00 rm101）
     以及 `REX.W MOV reg, [rip+disp32]` （48..4F 8B ...）
  3) 目标 = 下一条指令地址 + disp32，与字符串 RVA 相等即为引用者

意义：`newsendmsg` 这种字符串的引用者，就在"构造发送请求"的函数里。
这一步不需要 IDA，也不需要注入。
"""
import struct
import sys

import pefile

DLL = r"<微信安装目录>"

NEEDLES = [
    b"/cgi-bin/micromsg-bin/newsendmsg",
    b"newsendmsg",
    b"micromsg-bin",
    b"sendmsg",
    b"SendMsg",
    b"msgsource",
]


def main():
    pe = pefile.PE(DLL, fast_load=True)
    image_base = pe.OPTIONAL_HEADER.ImageBase
    sections = []
    for s in pe.sections:
        nm = s.Name.rstrip(b"\x00").decode("ascii", "replace")
        sections.append((nm, s.VirtualAddress, s.Misc_VirtualSize,
                         s.PointerToRawData, s.SizeOfRawData))
    print(f"image base = {image_base:#x}")
    for nm, va, vsz, pr, ssz in sections:
        print(f"  {nm:<10} RVA {va:#010x} vsize {vsz:#010x} raw {pr:#010x} "
              f"rawsize {ssz:#010x}")

    text = next(s for s in sections if s[0] == ".text")
    _tname, t_va, t_vsz, t_pr, t_ssz = text
    with open(DLL, "rb") as f:
        f.seek(t_pr)
        code = f.read(t_ssz)
    print(f"\n.text: {len(code)/1024/1024:.1f} MB（RVA 基址 {t_va:#x}）")

    def rva_to_off(rva):
        for _nm, va, vsz, pr, ssz in sections:
            if va <= rva < va + max(vsz, ssz):
                return pr + (rva - va)
        return None

    # ---- 找字符串 ----
    print("\n=== 字符串定位")
    with open(DLL, "rb") as f:
        blob = f.read()
    targets = {}
    for nd in NEEDLES:
        offs = []
        p = 0
        while len(offs) < 8:
            i = blob.find(nd, p)
            if i < 0:
                break
            offs.append(i)
            p = i + 1
        for off in offs:
            try:
                rva = pe.get_rva_from_offset(off)
            except Exception:
                continue
            targets.setdefault(rva, []).append(nd.decode("ascii", "replace"))
            print(f"  {nd.decode('ascii','replace'):<32} file {off:#x}  RVA {rva:#x}")
    del blob

    # ---- 扫 lea / mov rip 相对引用 ----
    want = set(targets)
    print(f"\n=== 扫描 RIP 相对引用（目标 {len(want)} 个字符串地址）")
    hits = {}
    rex_set = bytes([0x48, 0x49, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F])
    for opcode, kind in ((0x8D, "lea"), (0x8B, "mov"), (0x89, "mov!")):
        for rx in rex_set:
            pat = bytes([rx, opcode])
            p = 0
            while True:
                i = code.find(pat, p)
                if i < 0:
                    break
                p = i + 1
                if i + 7 > len(code):
                    continue
                modrm = code[i + 2]
                mod = modrm >> 6
                rm = modrm & 7
                if mod != 0 or rm != 5:
                    continue
                disp = struct.unpack_from("<i", code, i + 3)[0]
                insn_end_rva = t_va + i + 7
                tgt = (insn_end_rva + disp) & 0xFFFFFFFFFFFFFFFF
                if tgt in want:
                    hits.setdefault(tgt, []).append(
                        (kind, t_va + i, image_base + t_va + i))
    if not hits:
        print("  没找到任何引用。")
    for tgt in sorted(hits):
        names = "/".join(sorted(set(targets.get(tgt, []))))
        print(f"\n  --- 字符串 RVA {tgt:#x}  <{names}>")
        for kind, rva, abso in hits[tgt]:
            print(f"      {kind:<5} 引用地址 RVA {rva:#010x}  绝对 {abso:#014x}")
    pe.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
