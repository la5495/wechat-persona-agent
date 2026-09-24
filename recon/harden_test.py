"""牺牲进程自测：验证「最小回调 + 范围校验读」本身不会崩。

在自己的 python 进程里：
  1) hook kernel32.GetTickCount —— 回调只发计数 + 指针值，不读内存
  2) 触发 3 次，确认回调正常、进程存活
  3) 测试 safeRead：好地址能读、坏地址安全返回 null
都过了，才允许把同一套代码挂到微信上。
"""
import ctypes
import os
import sys
import time

import frida

JS = r'''
'use strict';
var hits = 0;
var SEND_CAP = 20;

function safeRead(p, n) {
    // 本环境 frida 的 findRangeByAddress/enumerate_processes 全失灵，
    // 所以守卫改为：指针形状过滤 + 限长 + try/catch
    try {
        if (p.isNull()) return null;
        var v = p;
        if (v.compare(ptr('0x10000')) < 0) return null;              // 小整数伪指针
        if (v.compare(ptr('0x0000800000000000')) >= 0) return null;   // 内核段
        if (n > 256) n = 256;
        return p.readByteArray(n);
    } catch (e) {
        return null;
    }
}

var k32 = Process.getModuleByName('kernel32.dll');
var target = k32.getExportByName('GetTickCount');
Interceptor.attach(target, {
    onEnter: function (args) {
        hits++;
        if (hits <= SEND_CAP) {
            send({ kind: 'hit', n: hits, tid: this.threadId,
                   args: [args[0].toString(), args[1].toString(),
                          args[2].toString(), args[3].toString()] });
        }
    }
});

// safeRead 测试：好地址 vs 坏地址
var good = Memory.allocUtf8String('HELLO1234');
var bad = ptr('0x1');
var diag = {};
try { diag.directBadRead = bad.readUtf8String(8); } catch (e) { diag.directBadError = 'caught:' + String(e).slice(0, 80); }
var goodResult = safeRead(good, 8);
send({ kind: 'ready', addr: target.toString(), diag: diag,
       safeRead_good: goodResult ? 'ok:' + goodResult.toString() : 'FAIL(null)',
       safeRead_bad: safeRead(bad, 8) === null ? 'ok:null' : 'FAIL(read!)' });
'''


def main():
    print("frida:", frida.__version__, "| 自己 pid =", os.getpid())
    dev = frida.get_local_device()
    session = dev.attach(os.getpid())
    msgs = []
    script = session.create_script(JS)
    script.on("message", lambda m, d: msgs.append(m))
    script.load()
    time.sleep(0.6)

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    for i in range(3):
        t = k.GetTickCount()
        print(f"  触发 GetTickCount #{i + 1} -> {t}")
    time.sleep(0.8)

    script.unload()
    session.detach()
    print("messages:", msgs)
    print("进程存活:", True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
