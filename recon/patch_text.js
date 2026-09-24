'use strict';
/*
 * 实验一（v2）：正文改写 —— 解析写法与 path_probe.js 完全一致，并带分步诊断。
 *
 * 路径（08:54 实测钉死）：
 *   A0 = args[2]
 *   A1 = *(A0 + 0x08)
 *   A2 = *(A1 + 0x00)
 *   N1(目标包装器) = *(A2 + 0x08)  → *(N1 + 0x08) = 目标 wxid 字符串
 *   N2(正文包装器) = *(A2 + 0x10)  → *(N2 + 0x00) = 正文字符串
 *   N2 布局：+0x00 数据指针 | +0x10 字节长度 | +0x18 容量
 *
 * 闸门：目标必须是 filehelper，原文必须含 UITRIGGER。
 */
(function () {
    var RVA = RVA_PLACEHOLDER;
    var READS = READS_PLACEHOLDER;

    var TRIGGER = 'UITRIGGER';
    var NEWTEXT = 'HOOKPATCH-OK1 \u94a9\u5b50\u6539\u6587\u672c';

    var done = 0;
    var probes = 0;

    function asPtr(v) {
        try { return ptr('0x' + v.toString(16)); } catch (e) { return null; }
    }
    function q(p, off) {
        try { return asPtr(p.add(off).readU64()); } catch (e) { return null; }
    }
    function rd(p, n) {
        try { return p.readByteArray(n); } catch (e) { return null; }
    }
    // 与 path_probe.js 完全一致的文本读取（先读字节校验，再 readUtf8String(len)）
    function txt(p, n) {
        try {
            if (!p || p.isNull()) return null;
            var b = rd(p, n);
            if (!b) return null;
            var a = new Uint8Array(b), pr = 0, bad = 0, len = 0;
            for (var i = 0; i < a.length; i++) {
                if (a[i] === 0) { if (len > 0) break; continue; }
                len++;
                if ((a[i] >= 32 && a[i] < 127) || a[i] >= 0x80) pr++; else bad++;
            }
            if (pr < 2 || bad > pr / 3) return null;
            return p.readUtf8String(len);
        } catch (e) { return null; }
    }
    function utf8len(s) {
        var n = 0;
        for (var i = 0; i < s.length; i++) {
            var c = s.charCodeAt(i);
            n += c < 0x80 ? 1 : c < 0x800 ? 2 : 3;
        }
        return n;
    }

    var mod = Process.getModuleByName('Weixin.dll');
    var addr = mod.base.add(RVA);

    Interceptor.attach(addr, {
        onEnter: function (args) {
            var diag = { n: ++probes };
            var A0 = args[2];
            diag.a0 = A0.toString();
            var A1 = q(A0, 8);
            diag.a1 = A1 ? A1.toString() : 'NULL';
            if (!A1) { if (probes <= 6) send({ kind: 'diag', diag: diag }); return; }
            var A2 = q(A1, 0);
            diag.a2 = A2 ? A2.toString() : 'NULL';
            if (!A2) { if (probes <= 6) send({ kind: 'diag', diag: diag }); return; }
            var N1 = q(A2, 8);
            var target = N1 ? txt(q(N1, 8), 64) : null;
            var N2 = q(A2, 0x10);
            var cur = N2 ? txt(q(N2, 0), 128) : null;
            diag.n1 = N1 ? N1.toString() : 'NULL';
            diag.n2 = N2 ? N2.toString() : 'NULL';
            diag.target = target;
            diag.cur = cur;
            if (probes <= 6 || target === 'filehelper') {
                send({ kind: 'diag', diag: diag });
            }

            if (done > 0) return;
            if (target !== 'filehelper') return;
            if (!cur || cur.indexOf(TRIGGER) < 0) return;

            done++;
            try {
                var buf = Memory.allocUtf8String(NEWTEXT);
                var len = utf8len(NEWTEXT);
                N2.writePointer(buf);
                N2.add(0x10).writeU32(len);
                N2.add(0x18).writeU32(len + 1);
                send({ kind: 'patched', old: cur, neu: NEWTEXT,
                       ptr: buf.toString(), len: len });
            } catch (e) {
                send({ kind: 'patch-error', err: String(e).slice(0, 160) });
            }
        }
    });

    send({ kind: 'ready', rva: '0x' + RVA.toString(16), addr: addr.toString(),
           trigger: TRIGGER, newtext: NEWTEXT });
})();
