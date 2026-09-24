"""把特征字符串附近的**完整字符串**挖出来（只读磁盘）。

目的：拿到内嵌的 SQL 原文（建表 / 插入），确认表名列名与"发消息"落库的那条语句，
为后面"找发送函数"提供锚点。
"""
import re
import sys

DLL = r"<微信安装目录>"

# file offset -> 说明
TARGETS = [
    (0x8EC3B78, "message_content / packed_info_data（消息表列名，唯一出现）"),
    (0x8EC5178, "同上（RVA）"),
    (0x9887F38, "INSERT INTO 第 1 处"),
    (0x9888BB8, "CREATE TABLE 第 1 处"),
    (0x988F2F0, "INSERT OR REPLACE 第 1 处"),
    (0x8EC3B00, "message_content 再往前一点"),
    (0xA26443, "SendMsg 第 1 处"),
    (0x322773, "sendmsg（小写）第 1 处"),
    (0x93532AC, "SendText 第 1 处"),
    (0x93BA009, "send_text 第 1 处"),
    (0x9015390, "com.Tencent.WCDB 第 1 处"),
    (0x1542578, "Msg_ 第 1 处"),
    (0x5853F2D, "sqlite3_ 第 1 处"),
    (0x94F1574, "session_list 第 1 处"),
]

PRINTABLE = re.compile(rb"[\x20-\x7e\t]{%d,}")


def strings_near(buf, base_off, span, minlen=12):
    """从 buf 里抽出所有可打印串，附带偏移。"""
    out = []
    for m in PRINTABLE.finditer(buf):
        if len(m.group(0)) < minlen:
            continue
        out.append((base_off + m.start(), m.group(0).decode("ascii", "replace")))
    return out


def main():
    with open(DLL, "rb") as f:
        for off, label in TARGETS:
            span = 0x600
            start = max(0, off - span // 2)
            f.seek(start)
            buf = f.read(span)
            print("=" * 78)
            print(f"### file 0x{off:x}  —— {label}")
            found = strings_near(buf, start, span)
            if not found:
                print("    （附近没有 >=12 字符的可打印串）")
            for o, s in found[:14]:
                mark = "  <== 目标" if abs(o - off) < 64 else ""
                print(f"    @0x{o:x} len={len(s):<4} {s[:300]!r}{mark}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
