# -*- coding: utf-8 -*-
"""doctor.py —— 环境体检：拿到项目先跑这个

它会把「能不能跑起来」需要的每一样东西都查一遍，并告诉你**下一步该做什么**。

用法：
  python doctor.py            # 体检
  python doctor.py --fix      # 顺手把能从示例生成的文件建好（config / secrets / scope / sender 配置）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OK, WARN, BAD, INFO = "✅", "⚠️ ", "❌", "  "
results = []          # (级别, 标题, 说明)


def add(level, title, detail=""):
    results.append((level, title, detail))


def check_python():
    v = sys.version_info
    if v >= (3, 10):
        add(OK, "Python %d.%d.%d" % (v[0], v[1], v[2]))
    else:
        add(BAD, "Python 版本太低（%d.%d）" % (v[0], v[1]), "需要 3.10+，建议 3.12")
    if os.name != "nt":
        add(BAD, "不是 Windows", "这个项目依赖 Windows 的 UI 自动化和微信客户端，macOS/Linux 跑不了")
    else:
        add(OK, "Windows")


def check_deps():
    need = {
        "cryptography": "解密微信数据库",
        "zstandard": "解压消息内容",
        "win32api": "读窗口/进程（pywin32）",
        "uiautomation": "读微信控件树",
        "pyperclip": "剪贴板（发送用）",
        "PIL": "截图（Pillow）",
        "numpy": "图像处理",
        "psutil": "进程与句柄",
    }
    missing = []
    for mod, why in need.items():
        try:
            __import__(mod)
        except Exception:
            missing.append("%s（%s）" % (mod, why))
    if missing:
        add(BAD, "缺 %d 个依赖" % len(missing),
            "pip install -r requirements.txt   ｜ 缺：" + "、".join(missing))
    else:
        add(OK, "依赖齐全（%d 个）" % len(need))


def check_wechat():
    exe, ver = "", None
    try:
        import psutil
        for p in psutil.process_iter(["name", "exe"]):
            if (p.info.get("name") or "").lower() in ("weixin.exe", "wechat.exe"):
                exe = p.info.get("exe") or ""
                break
    except Exception:
        pass
    if not exe:
        try:
            sys.path.insert(0, HERE)
            import wxsnap
            exe = wxsnap._wechat_exe()
        except Exception:
            pass
    if not exe:
        add(BAD, "找不到微信", "先安装并登录微信 4.x（Weixin.exe）")
        return
    try:
        import win32api
        info = win32api.GetFileVersionInfo(exe, "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        ver = "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:
        pass
    expect = None
    try:
        expect = (json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
                  .get("compat") or {}).get("wechat_version")
    except Exception:
        try:
            expect = (json.load(open(os.path.join(HERE, "config.example.json"),
                                     encoding="utf-8")).get("compat") or {}).get("wechat_version")
        except Exception:
            pass
    if ver and expect and ver != expect:
        add(WARN, "微信版本 %s ≠ 已知可用 %s" % (ver, expect),
            "库结构/内存偏移可能已变。先跑 python wxsnap.py keys 验证，别直接开自动回复")
    else:
        add(OK, "微信 %s" % (ver or "已安装"))

    running = False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/NH"],
                             capture_output=True, timeout=20).stdout or b""
        running = b"Weixin.exe" in out
    except Exception:
        pass
    if running:
        add(OK, "微信正在运行", "（提取密钥需要它开着）")
    else:
        add(WARN, "微信没在运行", "先打开并登录微信，再跑 wxsnap.py keys / refresh")


def check_db_root():
    try:
        sys.path.insert(0, HERE)
        import wxsnap
        src = getattr(wxsnap, "SRC", "")
    except Exception as e:
        add(BAD, "导入 wxsnap 失败", "%s: %s" % (type(e).__name__, e))
        return
    if src and os.path.isdir(src):
        n_msg = len([1 for dp, _d, fs in os.walk(src) for f in fs if f.endswith(".db")])
        add(OK, "找到微信数据库目录", "%s（%d 个 .db）" % (src, n_msg))
    else:
        add(BAD, "没找到微信数据库目录",
            "设置环境变量 WXSNAP_SRC 指向 …\\xwechat_files\\<你的wxid>_xxxx\\db_storage，"
            "或写进 config.json 的 paths.db_root")

    if os.path.isdir(os.path.join(HERE, "plain", "db_storage")):
        add(OK, "已有解密副本（plain/）")
    else:
        add(INFO, "还没解密过", "跑：python wxsnap.py refresh")


def check_config(fix=False):
    # config.json
    cfg_p = os.path.join(HERE, "config.json")
    ex_p = os.path.join(HERE, "config.example.json")
    if not os.path.exists(cfg_p):
        if fix and os.path.exists(ex_p):
            shutil.copyfile(ex_p, cfg_p)
            add(WARN, "已从示例生成 config.json", "❗ 打开它，把 self_wxid 改成你自己的")
        else:
            add(BAD, "缺 config.json",
                "copy config.example.json config.json  然后填 self_wxid（python doctor.py --fix 可自动建）")
    else:
        try:
            cfg = json.load(open(cfg_p, encoding="utf-8"))
            w = (cfg.get("self_wxid") or "").strip()
            if w.startswith("wxid_") and "你的" not in w:
                add(OK, "config.json 已填 self_wxid")
            else:
                add(BAD, "config.json 的 self_wxid 还是占位符",
                    "填成你自己的 wxid（在数据库目录名里：…\\xwechat_files\\<wxid>_xxxx\\）")
        except Exception as e:
            add(BAD, "config.json 读不了", str(e))

    # secrets.json
    sec_p = os.path.join(HERE, "secrets.json")
    sec_ex = os.path.join(HERE, "secrets.example.json")
    if not os.path.exists(sec_p):
        if fix and os.path.exists(sec_ex):
            shutil.copyfile(sec_ex, sec_p)
            add(WARN, "已从示例生成 secrets.json", "❗ 打开它，填你自己的 DEEPSEEK_API_KEY")
        else:
            add(BAD, "缺 secrets.json",
                "copy secrets.example.json secrets.json  然后填 DEEPSEEK_API_KEY")
    else:
        try:
            sec = json.load(open(sec_p, encoding="utf-8"))
            k = (sec.get("DEEPSEEK_API_KEY") or "").strip()
            if k.startswith("sk-") and "填" not in k and len(k) > 20:
                add(OK, "secrets.json 已填 API key", "（长度 %d，不打印内容）" % len(k))
            else:
                add(BAD, "secrets.json 里的 key 还是占位符", "填一个真的 DeepSeek API key")
        except Exception as e:
            add(BAD, "secrets.json 读不了", str(e))

    # corpus/scope.json
    sc = os.path.join(HERE, "corpus", "scope.json")
    sc_ex = os.path.join(HERE, "corpus", "scope.example.json")
    if not os.path.exists(sc):
        if fix and os.path.exists(sc_ex):
            shutil.copyfile(sc_ex, sc)
            add(WARN, "已从示例生成 corpus/scope.json",
                "❗ 打开它，chats 里填你要抓的会话名（python wxsnap.py sessions 可以看）")
        else:
            add(BAD, "缺 corpus/scope.json", "copy corpus\\scope.example.json corpus\\scope.json")
    else:
        try:
            s = json.load(open(sc, encoding="utf-8"))
            chats = [c for c in (s.get("chats") or []) if c]
            if chats:
                add(OK, "scope.json 已填 %d 个会话" % len(chats))
            else:
                add(BAD, "scope.json 的 chats 是空的", "至少填一个会话名")
        except Exception as e:
            add(BAD, "scope.json 读不了", str(e))

    # sender/config.json
    snd = os.path.join(HERE, "sender", "config.json")
    snd_ex = os.path.join(HERE, "sender", "config.example.json")
    if not os.path.exists(snd):
        if fix and os.path.exists(snd_ex):
            shutil.copyfile(snd_ex, snd)
            add(WARN, "已从示例生成 sender/config.json", "（发送腿配置，默认只允许发文件传输助手）")
        else:
            add(WARN, "缺 sender/config.json", "copy sender\\config.example.json sender\\config.json")


def check_ollama():
    """本地模型（做记忆抽取与人设蒸馏用）——不装也能跑，只是那两步会跳过。"""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as r:
            tags = json.loads(r.read().decode("utf-8"))
        names = [m.get("name", "") for m in (tags.get("models") or [])]
        if any(n.startswith("qwen2.5") for n in names):
            add(OK, "Ollama 在跑，且有 qwen2.5", "共 %d 个模型" % len(names))
        else:
            add(WARN, "Ollama 在跑，但没看到 qwen2.5:7b",
                "ollama pull qwen2.5:7b   （记忆抽取/人设蒸馏要用）")
    except Exception:
        add(WARN, "Ollama 没在跑（可选）",
            "记忆抽取与人设蒸馏需要它：装 Ollama + ollama pull qwen2.5:7b。"
            "不装也能跑，只是这两步做不了")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="从示例生成缺失的配置文件")
    args = ap.parse_args()

    print("=" * 72)
    print("🩺 微信 AI 分身 · 环境体检")
    print("=" * 72)
    check_python()
    check_deps()
    check_wechat()
    check_db_root()
    check_config(fix=args.fix)
    check_ollama()

    for lv, title, detail in results:
        print("%s %s" % (lv, title))
        if detail:
            print("     %s" % detail)

    bad = [r for r in results if r[0] == BAD]
    warn = [r for r in results if r[0] == WARN]
    print("-" * 72)
    if bad:
        print("❌ 有 %d 个问题必须先解决（上面标 ❌ 的）" % len(bad))
        print("   补完再跑一次：python doctor.py")
    elif warn:
        print("⚠️  基本可用，但有 %d 处提醒（大多是「还没填/还没跑」）" % len(warn))
    else:
        print("🎉 全部就绪！下一步：")
        print("   1) python wxsnap.py refresh          # 解密微信库（要微信开着）")
        print("   2) python corpus/export.py           # 导出你的聊天语料")
        print("   3) python update.py                  # 蒸馏人设 + 建记忆")
        print("   4) python run.py --once              # 先看它想说什么（只写草稿）")
    print("=" * 72)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
