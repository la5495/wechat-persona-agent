'use strict';
/*
 * 实验一（v3 · 原地覆写）—— 不再分配内存、不再动指针所有权
 * ------------------------------------------------------------------
 * v2 的教训：把 N2+0x00 指向 frida 分配的缓冲 → 微信稍后释放该指针时
 * 释放的不是自己的内存 → 堆损坏 → 微信退出。
 *
 * v3 只做两件事：
 *   1. 把新文本【原地写进微信自己的缓冲】（*(N2+0x00)，容量 *(N2+0x18) 之内）
 *   2. 改长度字段 N2+0x10
 * 指针与容量都不动 → 所有权不变 → 不会 invalid free。
 *
 * 闸门：目标必须是 filehelper、原文必须含 UITRIGGER、且新文本装得下。
 */
(function () {
    var RVA = RVA_PLACEHOLDER;
    var READS = READS_PLACEHOLDER;

    var TRIGGER = 'UITRIGGER';
    var NEWTEXT = 'HOOKLOCAL-OK1 \u539f\u5730\u8986\u5199';   // 原地覆写

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
            var A1 = q(A0, 8);
            if (!A1) { if (probes <= 4) send({ kind: 'diag', diag: diag }); return; }
            var A2 = q(A1, 0);
            if (!A2) { if (probes <= 4) send({ kind: 'diag', diag: diag }); return; }
            var N1 = q(A2, 8);
            var target = N1 ? txt(q(N1, 8), 64) : null;
            var N2 = q(A2, 0x10);
            if (!N2) { if (probes <= 4) send({ kind: 'diag', diag: diag }); return; }
            var cur = txt(q(N2, 0), 128);
            diag.target = target;
            diag.cur = cur;
            diag.n2 = N2.toString();
            if (probes <= 4 || target === 'filehelper') send({ kind: 'diag', diag: diag });

            if (done > 0) return;
            if (target !== 'filehelper') return;
            if (!cur || cur.indexOf(TRIGGER) < 0) return;

            // ---- 原地覆写 ----
            var dataPtr = q(N2, 0);
            if (!dataPtr) { send({ kind: 'patch-error', err: 'no data ptr' }); return; }
            var cap = N2.add(0x18).readU32();
            var len = utf8len(NEWTEXT);
            var before = txt(dataPtr, 128);
            if (len + 1 > cap) {
                send({ kind: 'patch-skip', why: 'too long', need: len + 1, cap: cap });
                return;
            }
            done++;
            try {
                dataPtr.writeUtf8String(NEWTEXT);      // 原地写内容 + NUL
                N2.add(0x10).writeU32(len);            // 只改长度
                var after = txt(dataPtr, 128);
                send({ kind: 'patched-inplace', old: before, neu: after,
                       data: dataPtr.toString(), cap: cap, len: len,
                       lenField: N2.add(0x10).readU32() });
            } catch (e) {
                send({ kind: 'patch-error', err: String(e).slice(0, 160) });
            }
        }
    });

    send({ kind: 'ready', rva: '0x' + RVA.toString(16), addr: addr.toString(),
           trigger: TRIGGER, newtext: NEWTEXT });
})();
