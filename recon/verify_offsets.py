"""核实：直接全文件搜索，打印真实命中偏移 + hexdump。"""
import sys

DLL = r"<微信安装目录>"
NEEDLES = [b"message_content", b"packed_info_data", b"INSERT INTO",
           b"CREATE TABLE", b"SendMsg", b"com.Tencent.WCDB", b"sqlite3_"]


def main():
    size = 0
    hits = {n: [] for n in NEEDLES}
    overlap = 64
    with open(DLL, "rb") as f:
        base = 0
        chunk = f.read(1 << 23)
        while chunk:
            for n in NEEDLES:
                s = 0
                while True:
                    i = chunk.find(n, s)
                    if i < 0:
                        break
                    if len(hits[n]) < 6:
                        hits[n].append(base + i)
                    s = i + 1
            size = base + len(chunk)
            tail = chunk[-overlap:]
            nxt = f.read(1 << 23)
            if not nxt:
                break
            base += len(chunk) - overlap
            chunk = tail + nxt
    print(f"文件大小 {size/1024/1024:.1f} MB")

    with open(DLL, "rb") as f:
        for n in NEEDLES:
            hs = hits[n]
            print("=" * 74)
            print(f"### {n.decode()}  命中 {len(hs)} 处（最多显示 6）")
            for off in hs[:3]:
                f.seek(off)
                buf = f.read(96)
                hx = " ".join(f"{b:02x}" for b in buf[:48])
                asc = "".join(chr(b) if 32 <= b < 127 else "." for b in buf[:48])
                # 往前后各扩一点，把整条字符串抓出来
                f.seek(max(0, off - 160))
                wide = f.read(400)
                runs = []
                cur = []
                for b in wide:
                    if 32 <= b < 127 or b == 9:
                        cur.append(chr(b))
                    else:
                        if len(cur) >= 10:
                            runs.append("".join(cur))
                        cur = []
                if len(cur) >= 10:
                    runs.append("".join(cur))
                print(f"  --- 真实 file offset 0x{off:x}")
                print(f"      hex: {hx}")
                print(f"      asc: {asc}")
                for r in runs[:4]:
                    print(f"      str: {r[:260]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
