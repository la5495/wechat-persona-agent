'use strict';
/*
 * 只读观察 agent：定位 4.1.15.9 的发送路径
 * ------------------------------------------------------------------
 * 对候选函数（CGI 发送请求构造簇）安装 onEnter 观察点：
 *   - 只读参数、只读内存、只读调用栈
 *   - **绝不调用、绝不修改、绝不改返回值**
 * 然后手动发一条含哨兵串的消息，看哪个函数收到了它。
 */
(function () {
    var CANDIDATES = [
        { rva: 0x39EC070, note: 'cluster-A 221B' },
        { rva: 0x39EC560, note: 'cluster-B 1295B' },
        { rva: 0x39EDD10, note: 'cluster-C 821B' },
        { rva: 0x39EE9F0, note: 'cluster-D 245B' },
        { rva: 0x39EEB80, note: 'cluster-E 97B' },
        { rva: 0x39EEC30, note: 'cluster-F 150B' }
    ];

    var mod = Process.getModuleByName('Weixin.dll');
    var BASE = mod.base;
    var counter = {};
    var MAX_RECORDS = 400;
    var sent = 0;

    function safeStr(ptr, max) {
        try {
            var s = ptr.readUtf8String(max || 96);
            if (!s) return null;
            // 只回报"像人话"的串，过滤指针垃圾
            if (!/^[\x20-\x7e\u00a0-\uffff]{1,96}$/.test(s)) return null;
            if (s.length < 2) return null;
            return s;
        } catch (e) {
            return null;
        }
    }

    function describe(args) {
        var out = [];
        for (var i = 0; i < 4; i++) {
            var p = null;
            try { p = args[i]; } catch (e) { break; }
            var entry = { i: i };
            try { entry.v = p.toString(); } catch (e) { }
            var s = safeStr(p, 96);
            if (s) entry.s = s;
            out.push(entry);
        }
        return out;
    }

    CANDIDATES.forEach(function (c) {
        var addr = BASE.add(c.rva);
        try {
            Interceptor.attach(addr, {
                onEnter: function (args) {
                    counter[c.rva] = (counter[c.rva] || 0) + 1;
                    if (sent >= MAX_RECORDS) return;
                    sent++;
                    var rec = {
                        fn: '0x' + c.rva.toString(16),
                        note: c.note,
                        addr: addr.toString(),
                        n: counter[c.rva],
                        args: describe(args)
                    };
                    try {
                        rec.bt = Thread.backtrace(this.context, Backtracer.FUZZY)
                            .slice(0, 7)
                            .map(function (a) { return a.toString(); });
                    } catch (e) { rec.btError = String(e); }
                    send({ kind: 'call', rec: rec });
                }
            });
        } catch (e) {
            send({ kind: 'hook-error', rva: '0x' + c.rva.toString(16), err: String(e) });
        }
    });

    send({
        kind: 'ready',
        base: BASE.toString(),
        moduleSize: mod.size,
        hooks: CANDIDATES.length
    });

    // 定期汇报计数，方便看"哪个函数真的被调用"
    setInterval(function () {
        var snap = {};
        for (var k in counter) snap['0x' + (+k).toString(16)] = counter[k];
        send({ kind: 'counts', counts: snap, sent: sent });
    }, 5000);
})();
