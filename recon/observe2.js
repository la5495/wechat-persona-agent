'use strict';
/*
 * 硬化版只读观察（吸取 2026-09-23 22:31 崩溃教训）：
 *   1. 一次只挂【一个】钩子
 *   2. 回调里默认【零内存读】—— 只报指针值本身（十六进制）
 *   3. 需要读时走 safeRead（指针形状过滤 + 限长 + try/catch），
 *      坏地址会抛可捕获异常（已在牺牲进程上验证）
 *   4. 不扫栈、不调任何微信函数、send 限流
 */
(function () {
    var RVA = RVA_PLACEHOLDER;      // python 注入时替换
    var READS = READS_PLACEHOLDER;  // 0 = 纯记录；1 = 带范围守卫读参数
    var CAP = 60;
    var count = 0;
    var curArgs = null;   // onEnter 存、onLeave 用

    function safeRead(p, n) {
        try {
            if (p.isNull()) return null;
            if (p.compare(ptr('0x10000')) < 0) return null;              // 小整数伪指针
            if (p.compare(ptr('0x0000800000000000')) >= 0) return null;   // 内核段
            if (n > 256) n = 256;
            return p.readByteArray(n);
        } catch (e) {
            return null;
        }
    }

    var mod = Process.getModuleByName('Weixin.dll');
    var addr = mod.base.add(RVA);

    // ---- READS=2 用的深挖工具（全部有守卫） ----
    function hexOf(b) {
        return Array.from(new Uint8Array(b))
            .map(function (x) { return ('0' + x.toString(16)).slice(-2); })
            .join('');
    }

    function plausiblePtr(v) {
        try {
            var s = v.toString(16);
            return s.length >= 7 && s.length <= 12;
        } catch (e) { return false; }
    }

    function asPtr(v) {
        // UInt64.toString() 默认十进制；ptr() 只认 0x 前缀十六进制或十进制数字串。
        // 显式 "0x"+base16，毫无歧义。
        try { return ptr('0x' + v.toString(16)); } catch (e) { return null; }
    }

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

    // 有上限的递归指针走查：深度 3、每层最多 4 个堆指针、每跳都试文本
    // 只跟「堆指针」（排除 Weixin.dll 模块内地址 = vtable/代码），才追得到正文
    var MOD_BASE = mod.base;
    var MOD_END = mod.base.add(mod.size);

    function walk(p, depth, label, rec) {
        if (depth > 3) return;
        var LEN = 128;
        var b = safeRead(p, LEN);
        if (!b) { rec.push(label + ':-'); return; }
        rec.push(label + '[' + depth + ']:' + hexOf(b).slice(0, 128));
        var followed = 0;
        for (var off = 0; off + 8 <= LEN; off += 8) {
            var u = p.add(off).readU64();
            var q = asPtr(u);
            if (!q) continue;
            var t = tryText(q, 64);
            if (t) rec.push(label + '[' + depth + ']+0x' + off.toString(16) + '->TEXT:' + t.slice(0, 80));
            if (depth < 3 && followed < 4) {
                // 用 NativePointer(q) 做范围比较（UInt64.compare 只吃 UInt64）
                if (q.compare(ptr('0x10000')) < 0) continue;
                if (q.compare(MOD_BASE) >= 0 && q.compare(MOD_END) < 0) continue;  // 模块内=非堆
                walk(q, depth + 1, label + '>' + off.toString(16), rec);
                followed++;
            }
        }
    }

    function deepInspect(p, label, len, rec) {
        // 先 hex 当前缓冲区，再对其中每个 8 字节对齐的"疑似指针"跳一层读文本
        try {
            var b = safeRead(p, len);
            if (!b) { rec.push(label + ':-'); return; }
            rec.push(label + ':' + hexOf(b).slice(0, 220));
            for (var off = 0; off + 8 <= len; off += 8) {
                var q = p.add(off).readU64();
                if (!plausiblePtr(q)) continue;
                var t = tryText(asPtr(q), 64);
                if (t) rec.push(label + '+0x' + off.toString(16) + '->TEXT:' + t.slice(0, 90));
            }
        } catch (e) {
            rec.push(label + ':ERR:' + String(e).slice(0, 60));
        }
    }

    function bigdump(p, label, total, rec) {
        // 分块 hex 大缓冲（找正文用）：64 字节一块
        for (var off = 0; off < total; off += 64) {
            var b = safeRead(p.add(off), 64);
            if (!b) { rec.push(label + '+0x' + off.toString(16) + ':-'); break; }
            rec.push(label + '+0x' + off.toString(16) + ':' + hexOf(b));
        }
    }

    Interceptor.attach(addr, {
        onEnter: function (args) {
            count++;
            var rec = {
                n: count,
                tid: this.threadId,
                addr: addr.toString(),
                a0: args[0].toString(),
                a1: args[1].toString(),
                a2: args[2].toString(),
                a3: args[3].toString()
            };
            // x64: 函数入口时 [rsp] = 返回地址 —— 一个指针读，零风险拿调用者
            curArgs = args;
            try {
                rec.caller = this.context.sp.readPointer().toString();
            } catch (e) { rec.caller = 'err'; }
            // READS=3：一次性 ACCURATE 回溯（基于异常展开表，非 FUZZY 扫栈），
            // 只跑第一次命中，try/catch 兜底
            if (READS === 3 && count === 1) {
                try {
                    rec.bt = Thread.backtrace(this.context, Backtracer.ACCURATE)
                        .slice(0, 16)
                        .map(function (a) { return a.toString(); });
                } catch (e) { rec.bt = ['ERR:' + String(e).slice(0, 60)]; }
            }
            if (READS === 1) {
                rec.r = [];
                for (var i = 0; i < 4; i++) {
                    var b = safeRead(args[i], 64);
                    if (!b) { rec.r.push('R' + i + ':-'); continue; }
                    rec.r.push('R' + i + ':' + hexOf(b).slice(0, 64));
                }
            } else if (READS === 2) {
                rec.d = [];
                // 参数直接就是字符串？（比如 CGI 路径）
                for (var i = 0; i < 4; i++) {
                    var t = tryText(args[i], 96);
                    if (t) rec.d.push('DIR' + i + ':TEXT:' + t.slice(0, 90));
                }
                // 递归走查前三个参数（深度上限 2、每层最多 3 分支）
                walk(args[0], 0, 'W0', rec.d);
                walk(args[1], 0, 'W1', rec.d);
                walk(args[2], 0, 'W2', rec.d);
                // 大块 dump：找正文（序列化缓冲）
                bigdump(args[1], 'B1', 1024, rec.d);
                bigdump(args[0], 'B0', 256, rec.d);
            }
            if (count <= CAP) send({ kind: 'hit', rec: rec });
        },
        onLeave: function (retval) {
            // 函数跑完再 dump：序列化缓冲此时应已填满（正文在里面）
            // 注意：onLeave 里拿不到 onEnter 的 args，必须自己存
            if (READS === 2 && count <= CAP && curArgs) {
                var d = [];
                bigdump(curArgs[0], 'L0', 1024, d);
                bigdump(curArgs[1], 'L1', 256, d);
                walk(curArgs[0], 0, 'LW0', d);
                send({ kind: 'leave', d: d });
            }
        }
    });

    send({ kind: 'ready', rva: '0x' + RVA.toString(16),
           addr: addr.toString(), reads: READS === 1 });
})();
