"""walk 递归逐轮诊断。"""
import os
import sys
import time

import frida

TEST = r'''
'use strict';
var str = Memory.allocUtf8String('FOUND_IT');
var B = Memory.alloc(16);
B.writePointer(ptr('0x0'));
B.add(8).writePointer(str);
var A = Memory.alloc(16);
A.writePointer(B);
A.add(8).writePointer(ptr('0x0'));

var dbg = { A: A.toString(), B: B.toString(), str: str.toString() };
try { dbg.p0x = ptr('0x' + A.readU64().toString(16)).toString(); } catch (e) { dbg.p0x = 'ERR:' + e; }
try { dbg.equalsB = ptr('0x' + A.readU64().toString(16)).equals(B); } catch (e) { dbg.equalsB = 'ERR:' + e; }

function safeRead(p, n) {
    try {
        if (p.isNull()) return null;
        if (p.compare(ptr('0x10000')) < 0) return null;
        if (p.compare(ptr('0x0000800000000000')) >= 0) return null;
        if (n > 256) n = 256;
        return p.readByteArray(n);
    } catch (e) { return null; }
}

var b = safeRead(A, 96);
dbg.bLen = b ? b.length : 'null';
var iter = [];
if (b) {
    for (var off = 0; off + 8 <= b.length; off += 8) {
        var u = A.add(off).readU64();
        var q = null;
        try { q = ptr('0x' + u.toString(16)); } catch (e) { q = null; }
        iter.push({ off: off, u16: u.toString(16), q: q ? q.toString() : 'NULL',
                    s16: q ? q.toString(16) : '-', slen: q ? q.toString(16).length : 0 });
        if (off >= 24) break;
    }
}
dbg.iter = iter;
send({ kind: 'dbg', dbg: dbg });
'''


def main():
    dev = frida.get_local_device()
    session = dev.attach(os.getpid())
    script = session.create_script(TEST)
    msgs = []
    script.on("message", lambda m, d: msgs.append(m))
    script.load()
    time.sleep(0.6)
    script.unload()
    session.detach()
    for m in msgs:
        if m.get("type") == "send":
            print(m["payload"]["dbg"])
        else:
            print(m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
