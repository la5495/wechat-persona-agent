# -*- coding: utf-8 -*-
"""guard.py —— 保镖：白名单 / 急停 / 速率闸门 / 脱敏 / 审计

设计原则：**默认拒绝**。任何一道门不通过就什么都不做。
"""
from __future__ import annotations

import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- 读写小工具
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def append_jsonl(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


class Guard:
    def __init__(self, cfg, root=HERE):
        self.cfg = cfg
        self.root = root
        p = cfg.get("paths") or {}
        self.path_wl = os.path.join(root, p.get("whitelist", "state/whitelist.json"))
        self.path_auto = os.path.join(root, p.get("auto", "state/auto.json"))
        self.path_seen = os.path.join(root, p.get("seen", "state/seen.json"))
        self.path_rate = os.path.join(root, p.get("rate", "state/rate.json"))
        self.path_stop = os.path.join(root, p.get("stop", "STOP"))
        self.path_audit = os.path.join(root, p.get("audit", "audit.jsonl"))
        self.path_drafts = os.path.join(root, p.get("drafts", "drafts.jsonl"))

    # ------------------------------------------------------------ 急停
    def stopped(self) -> bool:
        return os.path.exists(self.path_stop)

    # ------------------------------------------------------------ 开关
    def auto_enabled(self) -> bool:
        return bool((load_json(self.path_auto, {}) or {}).get("enabled", False))

    def paused(self) -> bool:
        """被「微信消息遥控」暂停中（主人发「停止」触发）。"""
        st = load_json(self.path_auto, {}) or {}
        return bool(st.get("paused"))

    def pause_reason(self) -> str:
        return (load_json(self.path_auto, {}) or {}).get("reason", "")

    def set_auto(self, enabled: bool, by: str = "cli", paused: bool = False,
                 reason: str = ""):
        save_json(self.path_auto, {
            "enabled": bool(enabled),
            "paused": bool(paused),
            "reason": reason,
            "since": time.strftime("%Y-%m-%d %H:%M:%S"),
            "by": by,
        })
        return self.auto_enabled()

    # ------------------------------------------------------------ 微信遥控
    def control_word(self, text: str):
        """判断这条消息是不是遥控指令 → 'stop' / 'resume' / None

        ⚠️ 必须**精确匹配**（或「词+少量后缀」），不能用子串匹配 ——
        否则我们自己回的确认语（「已恢复自动对话」含"恢复"）会被当成新指令，
        造成自我循环（2026-09-24 实测踩到）。
        """
        c = self.cfg.get("control") or {}
        if not c or not text:
            return None
        t = (text or "").strip().lower().replace(" ", "")
        # 明确排除我们自己的确认语
        for key in ("confirm_stop", "confirm_resume"):
            v = (c.get(key) or "").strip().lower().replace(" ", "")
            if v and t == v:
                return None
        for tag, key in (("stop", "stop_words"), ("resume", "resume_words")):
            for w in (c.get(key) or []):
                wl = w.lower().replace(" ", "")
                if not wl:
                    continue
                if t == wl:
                    return tag
                # 允许「停止一下」「开始吧」这类短后缀，但拒绝长句子
                if t.startswith(wl) and len(t) <= len(wl) + 3:
                    return tag
        return None

    def control_path(self) -> str:
        p = (self.cfg.get("paths") or {}).get("control", "state/control.json")
        return os.path.join(self.root, p)

    def control_watermark(self) -> str:
        return (load_json(self.control_path(), {}) or {}).get("last_key", "")

    def set_control_watermark(self, key: str, extra: dict | None = None):
        rec = {"last_key": key, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        if extra:
            rec.update(extra)
        save_json(self.control_path(), rec)

    # ------------------------------------------------------------ 白名单
    def whitelist(self) -> list:
        wl = load_json(self.path_wl, [])
        if isinstance(wl, dict):
            wl = wl.get("names") or []
        out = []
        for e in wl:
            if isinstance(e, str):
                out.append({"name": e})
            elif isinstance(e, dict) and e.get("name"):
                out.append(e)
        return out

    def whitelist_names(self) -> list:
        return [e["name"] for e in self.whitelist()]

    def in_whitelist(self, name: str) -> bool:
        if not name:
            return False
        for n in self.whitelist_names():
            if n == name or n in name or name in n:
                return True
        return False

    # ------------------------------------------------------------ 约定红线
    # 主人 2026-09-24 明确要求：**不许替主人做任何约定**（"明天一起玩"这种）。
    # 这是真实世界风险（朋友会当真），所以用确定性规则兜底，不靠模型自觉。
    #
    # 分三类：
    #   HARD  绝对禁止（承诺/定死）
    #   SOFT  时间+活动要素（等于把约定说具体了）
    #   ALLOW 安全含糊（真人也会这么说，不算约定）
    COMMIT_HARD = [
        r"说好|说定了|就这么定|约好|约起|走起|不见不散",
        r"包在我身上|放心交给我|我来安排|我搞定|我安排",
        r"我请|我帮|我给你|我送你|我借你|我来弄|我去弄",
        r"没问题|一定到|肯定到|保证到",
    ]
    COMMIT_SOFT = [
        r"(明天|后天|大后天|今天|今晚|晚上|早上|中午|下午|上午|放假|周末|"
        r"周[一二三四五六日天]|星期[一二三四五六日天])[^。！？\n]{0,10}"
        r"(一起|一块|见|去|来|打|玩|吃|走|排|开黑|弄|搞|约)",
        r"(一起|一块)[^。！？\n]{0,8}(打|玩|吃|去|见|走|排|开黑|约)",
        r"几点|到时候|等你|等我|在哪|哪里见|地址|别迟到",
        r"\d{1,2}\s*点(半)?[^。！？\n]{0,4}(见|到|来|走|开始|打|玩|出发)",
    ]
    COMMIT_ALLOW = [
        r"到时候(看|再说|说吧?)",
        r"看情况|再说吧?|不一定|随缘|看吧|难说",
    ]

    # 对方在求助/借钱/要东西时，任何"答应"都算承诺（结合上下文才判得准）
    REQUEST_HINTS = (r"帮我|帮忙|帮个|借我|借钱|借点|带个|带点|带我|送我|给我|"
                     r"麻烦你|能不能帮|替我|代我|垫一下|先给我")
    REQUEST_REPLY_BLOCK = [
        r"^(行|好|好的|可以|成|嗯嗯|没问题)",
        r"多少|借|转|红包|给你|我出|垫",
        r"等下(给你|帮你|带|拿|弄|转|发)",
    ]

    def commitment_hits(self, text: str, context: str = "") -> list:
        """命中就说明这句话替主人做了约定/承诺 → 不许发。
        context: 对方最近说的话（用于判断是不是在求助/借钱）。"""
        if not text:
            return []
        # 安全含糊优先：真人也会这么说，只要没踩硬禁止就放行
        for pat in self.COMMIT_ALLOW:
            try:
                if re.search(pat, text):
                    if not any(re.search(h, text) for h in self.COMMIT_HARD):
                        return []
            except re.error:
                continue
        hits = []
        for pat in self.COMMIT_HARD + self.COMMIT_SOFT:
            try:
                m = re.search(pat, text)
                if m:
                    hits.append(m.group(0)[:20])
            except re.error:
                continue
        # 上下文判定：对方在求助/借钱 → 连"行"都算答应
        if context and re.search(self.REQUEST_HINTS, context):
            for pat in self.REQUEST_REPLY_BLOCK:
                try:
                    m = re.search(pat, text)
                    if m:
                        hits.append("求助场景:" + m.group(0)[:16])
                except re.error:
                    continue
        return hits

    # ------------------------------------------------------------ 人设泄露红线
    # 主人 2026-09-24 要求：被问就答「配置里的真名」，**不许说小名**，
    # 也不许露出助手/AI 视角（实测被好友抓到过「我转告他」这种第三人称泄露）。
    #
    # ⚠️ 只在「自称是 AI / 替人转达」的语境才拦 ——
    #    「我在学 AI」「打 AI」这类**正常话题**必须放行（主人真的在学 AI 应用开发）。
    PERSONA_LEAK = [
        # 自称是 AI / 助手 / 程序
        r"我(其实|就)?是[^。！？\n]{0,6}(AI|ai|人工智能|机器人|助手|程序|模型)",
        r"作为[^。！？\n]{0,4}(AI|ai|人工智能|助手|机器人|模型)",
        r"(AI|ai)助手|智能助手|语言模型",
        # 替人转达 / 第三方视角
        r"转告|代他|替他(回|说|转达|看着)|帮他(回|转达)",
        r"我(问|跟|和)他(说|讲|一声)",
        # 人设 / 小名 / 第三方称呼
        r"人设|小名|昵称",
        r"主人",
    ]

    @property
    def NICKNAME(self) -> list:
        """小名清单（从配置读，别写死在代码里）——主人明确说「很尴尬」。"""
        p = self.cfg.get("persona") or {}
        ns = p.get("nicknames")
        if isinstance(ns, list) and ns:
            return [str(x) for x in ns if x]
        return []

    def persona_leak_hits(self, text: str) -> list:
        """命中说明这句话暴露了「不是本人」→ 不许发。"""
        if not text:
            return []
        hits = []
        for w in self.NICKNAME:
            if w in text:
                hits.append("小名:" + w)
        for pat in self.PERSONA_LEAK:
            try:
                m = re.search(pat, text)
                if m:
                    hits.append(m.group(0)[:16])
            except re.error:
                continue
        return hits

    # ------------------------------------------------------------ 脱敏
    def redact_hits(self, text: str) -> list:
        r = self.cfg.get("redaction") or {}
        if not r.get("enabled", True) or not text:
            return []
        hits = []
        for pat in (r.get("patterns") or []):
            try:
                if re.search(pat, text):
                    hits.append(pat)
            except re.error:
                continue
        return hits

    # ------------------------------------------------------------ 速率闸门
    def rate_check(self) -> tuple:
        r = self.cfg.get("rate") or {}
        st = load_json(self.path_rate, {}) or {}
        now = time.time()
        writes = [t for t in (st.get("writes") or []) if now - t < float(r.get("window_sec", 120))]
        last = float(st.get("last") or 0)
        gap = float(r.get("min_gap_sec", 2.5))
        if last and now - last < gap:
            return False, "距上次发送 %.1fs < %.1fs" % (now - last, gap)
        if len(writes) >= int(r.get("window_max_writes", 6)):
            return False, "%ds 内已发 %d 次（上限 %d）" % (
                int(r.get("window_sec", 120)), len(writes), int(r.get("window_max_writes", 6)))
        return True, "ok"

    def rate_mark(self):
        r = self.cfg.get("rate") or {}
        st = load_json(self.path_rate, {}) or {}
        now = time.time()
        writes = [t for t in (st.get("writes") or []) if now - t < float(r.get("window_sec", 120))]
        writes.append(now)
        st["writes"] = writes
        st["last"] = now
        save_json(self.path_rate, st)

    # ------------------------------------------------------------ 水位线
    def seen(self) -> dict:
        return load_json(self.path_seen, {}) or {}

    def is_seen(self, chat: str, key: str) -> bool:
        return (self.seen().get(chat) or {}).get("key") == key

    def mark_seen(self, chat: str, key: str, extra: dict | None = None):
        s = self.seen()
        rec = {"key": key, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        if extra:
            rec.update(extra)
        s[chat] = rec
        save_json(self.path_seen, s)

    # ------------------------------------------------------------ 审计
    def audit(self, rec: dict):
        rec = dict(rec)
        rec.setdefault("time", time.strftime("%Y-%m-%d %H:%M:%S"))
        append_jsonl(self.path_audit, rec)

    def draft(self, rec: dict):
        rec = dict(rec)
        rec.setdefault("time", time.strftime("%Y-%m-%d %H:%M:%S"))
        append_jsonl(self.path_drafts, rec)
