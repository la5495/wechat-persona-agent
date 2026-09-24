'use strict';
/*
 * 空心针 (hollow needle)
 * ------------------------------------------------------------------
 * 这个 agent 什么也不 hook、什么也不写。
 * 它只做只读自报：确认自己真的活在目标进程里，看得到它要看的模块。
 * 目的：用最小代价问清三个未知量 ——
 *   1) 能不能注入
 *   2) Defender / 安全软件拦不拦
 *   3) 微信会不会崩或掉线
 * 然后干净卸载，不留任何痕迹。
 */
(function () {
    var report = {
        pid: Process.id,
        arch: Process.arch,
        platform: Process.platform,
        pointerSize: Process.pointerSize,
        pageSize: Process.pageSize,
    };

    try {
        var main = Process.mainModule;
        report.mainModule = main ? main.name : null;
        report.mainModuleBase = main ? main.base.toString() : null;
    } catch (e) {
        report.mainModuleError = String(e);
    }

    try {
        var mods = Process.enumerateModules();
        report.moduleCount = mods.length;
        report.weixinModules = mods
            .filter(function (m) { return /weixin\.dll/i.test(m.name); })
            .map(function (m) {
                return { name: m.name, base: m.base.toString(), size: m.size };
            });
    } catch (e) {
        report.enumerateModulesError = String(e);
    }

    // 只读一个字节，证明"手真的伸进去了"（不写、不改）
    try {
        var probe = Process.getModuleByName('Weixin.dll');
        report.firstBytesOfWeixinDll = probe.base.readByteArray(2)
            ? Array.from(new Uint8Array(probe.base.readByteArray(2))).join(' ')
            : null;
        report.mzOk = probe.base.readUtf8String(2) === 'MZ';
    } catch (e) {
        report.readTestError = String(e);
    }

    send({ kind: 'hollow-needle-report', report: report });
    console.log('[hollow] alive: pid=' + Process.id + ' arch=' + Process.arch +
                ' modules=' + (report.moduleCount !== undefined ? report.moduleCount : '?'));
})();
