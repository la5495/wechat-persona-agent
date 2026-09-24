"""自测 walk() 递归走查：构造两级指针链，验证能挖到深处的文本。"""
import os
import sys
import time

import frida

HERE = os.path.dirname(os.path.abspath(__file__))
JS = os.path.join(HERE, "observe2.js")

TEST = r'''
'use strict';
function safeRead(p, n) {
    try {
        if (p.isNull()) return null;
        if (p.compare(ptr('0x10000')) < 0) return null;
        if (p.compare(ptr('0x0000800000000000')) >= 0) return null;
        if (n > 256) n = 256;
        return p.readByteArray(n);
    } catch (e) { return null; }
}
function hexOf(b) {
    return Array.from(new Uint8Array(b)).map(function (x) { return ('0' + x.toString(16)).slice(-2); }).join('');
}
function asPtr(v) { try { return ptr('0x' + v.toString(16)); } catch (e) { return null; } }
function tryText(p, n) {
    try {
        var b = safeRead(p, n);
        if (!b) return null;
        var arr = new Uint8Array(b);
        var printable = 0, bad = 0, len = 0;
        for (var i = 0; i < arr.length; i++) {
            var c = arr[i];
            if (c === 0) { if (len > 0) break; continue; }
            len++;
            if ((c >= 32 && c < 127) || c >= 0x80) printable++; else bad++;
        }
        if (printable < 4 || bad > printable / 3) return null;
        return p.readUtf8String(len);
    } catch (e) { return null; }
}
function walk(p, depth, label, rec) {
    if (depth > 2) return;
    var LEN = 96;
    var b = safeRead(p, LEN);
    if (!b) { rec.push(label + ':-'); return; }
    rec.push(label + '[' + depth + ']:' + hexOf(b).slice(0, 96));
    var followed = 0;
    for (var off = 0; off + 8 <= LEN; off += 8) {
        var q = asPtr(p.add(off).readU64());
        if (!q) continue;
        var t = tryText(q, 64);
        if (t) rec.push(label + '[' + depth + ']+0x' + off.toString(16) + '->TEXT:' + t.slice(0, 80));
        if (depth < 2 && followed < 3) {
            var s = q.toString(16);
            if (s.length === 10 || s.length === 11) {
                walk(q, depth + 1, label + '>0x' + off.toString(16), rec);
                followed++;
            }
        }
    }
}

// 构造：A -> B -> "FOUND_IT_文本"
var str = Memory.allocUtf8String('FOUND_IT_文本');
var B = Memory.alloc(16);
B.writePointer(ptr('0x0'));
B.add(8).writePointer(str);
var A = Memory.alloc(16);
A.writePointer(B);
A.add(8).writePointer(ptr('0x0'));
var rec = [];
walk(A, 0, 'T', rec);
send({ kind: 'selftest', lines: rec, found: rec.some(function (l) { return l.indexOf('FOUND_IT') >= 0; }) });
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
        print(m)
    ok = any(m.get("payload", {}).get("found") for m in msgs if m.get("type") == "send")
    print("walk 找到深处文本:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
