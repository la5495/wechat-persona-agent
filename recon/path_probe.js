'use strict';
/*
 * 精确路径探针（只读）
 * 目标：把 0x39EC560 入口处「消息元素 → 目标/正文」这条链的每一跳地址、
 *       内容、以及字符串包装器的长度/容量字段，干净地打出来。
 *
 * 已知路径（08:49 实测）：
 *   A0 = args[2]
 *   A1 = *(A0 + 0x08)
 *   A2 = *(A1 + 0x00)
 *   目标包装器 N1 = *(A2 + 0x08) → *(N1 + 0x08) = "filehelper"
 *   正文包装器 N2 = *(A2 + 0x10) → *(N2 + 0x00) = 正文
 */
(function () {
    var RVA = RVA_PLACEHOLDER;
    var READS = READS_PLACEHOLDER;
    var seen = 0;

    function asPtr(v) {
        try { return ptr('0x' + v.toString(16)); } catch (e) { return null; }
    }
    function rd(p, n) {
        try { return p.readByteArray(n); } catch (e) { return null; }
    }
    function hexOf(b) {
        return Array.from(new Uint8Array(b))
            .map(function (x) { return ('0' + x.toString(16)).slice(-2); })
            .join('');
    }
    function q(p, off) {
        try { return asPtr(p.add(off).readU64()); } catch (e) { return null; }
    }
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
    function dump(p, label, len, rec) {
        if (!p || p.isNull()) { rec.push(label + ' = null'); return; }
        var b = rd(p, len);
        rec.push(label + ' @' + p + ' :' + (b ? hexOf(b) : '<unreadable>'));
        if (b) {
            for (var off = 0; off + 8 <= len; off += 8) {
                var v = q(p, off);
                if (!v || v.isNull()) continue;
                var t = txt(v, 64);
                if (t) rec.push(label + '+0x' + off.toString(16) + ' -> TEXT: ' + t.slice(0, 70));
            }
        }
    }

    var mod = Process.getModuleByName('Weixin.dll');
    var addr = mod.base.add(RVA);

    Interceptor.attach(addr, {
        onEnter: function (args) {
            seen++;
            if (seen > 2) return;
            var rec = { n: seen, a0: args[0].toString(), a1: args[1].toString(),
                        a2: args[2].toString(), a3: args[3].toString(), d: [] };
            var A0 = args[2];
            dump(A0, 'A0', 48, rec.d);
            var A1 = q(A0, 8);
            if (A1) {
                dump(A1, 'A1', 48, rec.d);
                var A2 = q(A1, 0);
                if (A2) {
                    dump(A2, 'A2', 48, rec.d);
                    var N1 = q(A2, 8);
                    if (N1) {
                        dump(N1, 'N1(target wrapper)', 48, rec.d);
                        var T = q(N1, 8);
                        if (T) dump(T, 'TARGET', 48, rec.d);
                    }
                    var N2 = q(A2, 0x10);
                    if (N2) {
                        dump(N2, 'N2(text wrapper)', 48, rec.d);
                        var X = q(N2, 0);
                        if (X) dump(X, 'TEXT', 48, rec.d);
                    }
                }
            }
            send({ kind: 'hit', rec: rec });
        }
    });

    send({ kind: 'ready', rva: '0x' + RVA.toString(16), addr: addr.toString() });
})();
