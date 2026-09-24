# -*- coding: utf-8 -*-
"""guard.py —— 「保镖」：所有安全闸门的**唯一实现处**

集中管理：

* **总开关**：自动对话是否开启（`state/auto.json`）
* **白名单**：只有名单内的会话才自动回复（`state/whitelist.json`）
* **急停**：`STOP` 文件存在即全部停手
* **速率闸门**：两次发送之间强制间隔 + 滑动窗口 + 冷却（`state/rate.json`）
* **脱敏**：发送前扫 key / 密码 / 验证码
* **审计**：每条动作落 `audit.jsonl`

设计原则：**默认全关**。开关不开、白名单为空时，任何自动回复都不会发生。
"""
from __future__ import annotations

import json
import os
import random
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _paths(cfg=None):
    cfg = cfg or load_config()
    p = cfg.get("paths") or {}
    state_dir = os.path.join(HERE, p.get("state_dir", "state"))
    return {
        "state_dir": state_dir,
        "auto": os.path.join(state_dir, "auto.json"),
        "whitelist": os.path.join(state_dir, "whitelist.json"),
        "rate": os.path.join(state_dir, "rate.json"),
        "stop": os.path.join(HERE, p.get("stop", "STOP")),
        "audit": os.path.join(HERE, p.get("audit", "audit.jsonl")),
    }


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 急停
def stop_present(cfg=None):
    return os.path.exists(_paths(cfg)["stop"])


# ---------------------------------------------------------------- 总开关
def auto_state(cfg=None):
    """返回 {'enabled': bool, 'since': str, 'by': str, 'quiet': bool}

    `quiet` = 「安静模式」：按过 ESC 急停留下的状态，自动回复关掉 + 采集也暂停，
    但**服务进程还活着**（随时可以恢复，不用重启计划任务）。
    """
    p = _paths(cfg)
    d = _read_json(p["auto"], None)
    if d is None:
        # 首次：以 config.auto.enabled 为默认（出厂 false）
        cfg = cfg or load_config()
        enabled = bool((cfg.get("auto") or {}).get("enabled", False))
        return {"enabled": enabled, "since": None, "by": "config-default",
                "quiet": False}
    return {"enabled": bool(d.get("enabled")), "since": d.get("since"),
            "by": d.get("by", "?"), "quiet": bool(d.get("quiet", False))}


def set_auto(enabled, by="cli", cfg=None, quiet=False):
    """显式开关命令。**默认会解除「安静模式」**（按下 ESC 后想恢复就再敲一次开关）。"""
    p = _paths(cfg)
    data = {"enabled": bool(enabled), "since": now_str(), "by": by,
            "quiet": bool(quiet)}
    _write_json(p["auto"], data)
    return data


def set_quiet(on, by="esc", cfg=None):
    """ESC 急停：关掉自动回复 **并且** 暂停采集，但服务进程继续活着。

    为什么要连采集一起停：`harvest.skip_when_auto` 开着时，只关自动回复
    **反而会把采集打开** —— 而采集同样要点鼠标。急停当然要两个都停。
    """
    p = _paths(cfg)
    cur = _read_json(p["auto"], None) or {}
    data = {"enabled": False if on else bool(cur.get("enabled")),
            "since": now_str(), "by": by, "quiet": bool(on)}
    _write_json(p["auto"], data)
    return data


def quiet_state(cfg=None):
    return auto_state(cfg)["quiet"]


def auto_enabled(cfg=None):
    return auto_state(cfg)["enabled"]


# ---------------------------------------------------------------- 白名单
def load_whitelist(cfg=None):
    """返回 [{'name':..,'note':..,'added_at':..}, ...]

    以 `state/whitelist.json` 为准；文件不存在时用 `config.whitelist` 做种子。
    """
    cfg = cfg or load_config()
    p = _paths(cfg)
    d = _read_json(p["whitelist"], None)
    if d is None:
        return [{"name": n, "note": "", "added_at": None, "source": "config"}
                for n in (cfg.get("whitelist") or [])]
    return d if isinstance(d, list) else []


def save_whitelist(entries, cfg=None):
    _write_json(_paths(cfg)["whitelist"], entries)
    return entries


def add_whitelist(name, note="", cfg=None):
    name = (name or "").strip()
    if not name:
        return False, "会话名不能为空"
    cfg = cfg or load_config()
    if name in set(cfg.get("blacklist") or []):
        return False, "%r 在黑名单里，不能加白名单" % name
    entries = load_whitelist(cfg)
    for e in entries:
        if e.get("name") == name:
            return False, "%r 已经在白名单里了（加入于 %s）" % (name, e.get("added_at"))
    entries.append({"name": name, "note": note, "added_at": now_str(), "source": "cli"})
    save_whitelist(entries, cfg)
    return True, "已加入白名单：%s（共 %d 个）" % (name, len(entries))


def remove_whitelist(name, cfg=None):
    name = (name or "").strip()
    cfg = cfg or load_config()
    entries = load_whitelist(cfg)
    kept = [e for e in entries if e.get("name") != name]
    if len(kept) == len(entries):
        return False, "%r 不在白名单里" % name
    save_whitelist(kept, cfg)
    return True, "已移出白名单：%s（剩 %d 个）" % (name, len(kept))


def is_whitelisted(name, cfg=None):
    if not name:
        return False
    entries = load_whitelist(cfg)
    for e in entries:
        n = e.get("name") or ""
        if n == name or (n and n in name) or (name and name in n):
            return True
    return False


# ---------------------------------------------------------------- 速率闸门
class RateGate:
    """两次"对外写动作"之间的强制间隔 + 滑动窗口 + 冷却。跨进程持久化。"""

    def __init__(self, cfg=None, path=None):
        self.cfg = cfg or load_config()
        self.rate = self.cfg.get("rate") or {}
        self.path = path or _paths(self.cfg)["rate"]

    def _load(self):
        d = _read_json(self.path, {"writes": []})
        w = [float(x) for x in (d.get("writes") or []) if isinstance(x, (int, float))]
        return w

    def _save(self, writes):
        cutoff = time.time() - 24 * 3600
        _write_json(self.path, {"writes": [w for w in writes if w > cutoff]})

    def seconds_to_wait(self):
        now = time.time()
        writes = self._load()
        win = float(self.rate.get("window_sec", 120))
        wmax = int(self.rate.get("window_max_writes", 6))
        in_win = [w for w in writes if now - w < win]
        if len(in_win) >= wmax:
            return random.uniform(float(self.rate.get("cooldown_min_sec", 30)),
                                  float(self.rate.get("cooldown_max_sec", 75))), \
                "窗口内已写 %d 次（上限 %d）→ 冷却" % (len(in_win), wmax)
        if writes:
            gap = now - max(writes)
            need = random.uniform(float(self.rate.get("min_gap_sec", 2.5)),
                                  float(self.rate.get("max_gap_sec", 6.0)))
            if gap < need:
                return need - gap, "距上次 %.1fs，需间隔 %.1fs" % (gap, need)
        return 0.0, "可以发"

    def wait(self, log=print):
        while True:
            secs, why = self.seconds_to_wait()
            if secs <= 0:
                return why
            log("  ⏳ 速率闸门：%s → 等 %.1fs" % (why, secs))
            time.sleep(secs)

    def record(self):
        writes = self._load()
        writes.append(time.time())
        self._save(writes)


# ---------------------------------------------------------------- 脱敏
def scan_sensitive(text, patterns):
    hits = []
    for p in patterns or []:
        try:
            m = re.search(p, text or "", re.IGNORECASE)
            if m:
                hits.append({"pattern": p, "match": m.group(0)[:40]})
        except re.error:
            continue
    return hits


def redaction_check(cfg, text):
    """返回 (ok, hits)。ok=False 表示必须中止。"""
    red = cfg.get("redaction") or {}
    if not red.get("enabled", True):
        return True, []
    hits = scan_sensitive(text, red.get("patterns"))
    if hits and red.get("block_on_hit", True):
        return False, hits
    return True, hits


# ---------------------------------------------------------------- 审计
def audit(cfg, record):
    path = _paths(cfg)["audit"]
    rec = dict(record)
    rec.setdefault("time", now_str())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def gate_status(cfg=None, whitelist_only=True):
    """给人和程序看的一页纸状态。"""
    cfg = cfg or load_config()
    st = auto_state(cfg)
    wl = load_whitelist(cfg)
    return {
        "auto_enabled": st["enabled"],
        "auto_since": st["since"],
        "auto_by": st["by"],
        "quiet": st.get("quiet", False),
        "stop_present": stop_present(cfg),
        "mode": cfg.get("mode"),
        "whitelist_count": len(wl),
        "whitelist": [e.get("name") for e in wl] if whitelist_only else wl,
        "blacklist": cfg.get("blacklist") or [],
    }
