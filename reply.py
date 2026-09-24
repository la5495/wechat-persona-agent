# -*- coding: utf-8 -*-
"""reply.py —— 回复引擎：云端 DeepSeek（OpenAI 兼容接口）

只用标准库（urllib），不引入任何新依赖。
密钥只从 secrets.json / 环境变量读，**绝不打印、绝不写日志**。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_persona(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _api_key(cfg: dict, root: str) -> str:
    r = cfg.get("reply") or {}
    env = r.get("api_key_env") or "DEEPSEEK_API_KEY"
    if os.environ.get(env):
        return os.environ[env]
    path = os.path.join(root, r.get("secrets_file") or "secrets.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get(env) or ""
    except Exception:
        return ""


def _norm(s: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", s or "")


# 这些是主人的口头禅，跟对方撞词很正常，不算复读
_CATCH = {"行", "来", "OK", "ok", "嗯", "哦", "哦哦", "6", "66", "666", "神了",
          "？", "啥", "没", "好的", "可以", "哈哈", "草", "靠"}


def is_echo(line: str, incoming_texts: list) -> bool:
    """这行是不是在复读对方的话（2026-09-24 实测：对方打错字「合意味」，
    机器人跟着回「何意味」，像复读机）。

    判据：去掉标点后 **字符重合 ≥60% 且长度相差 ≤1**，并且不是口头禅。
    长度相近是关键 —— 免得把「行啊」回「行不行啊」这种正常简短回应误判。
    """
    l = _norm(line)
    if len(l) < 2 or l in _CATCH:
        return False
    ls = set(l)
    for t in (incoming_texts or []):
        s = _norm(t)
        if len(s) < 2:
            continue
        same = sum(1 for ch in ls if ch in s)
        if same / max(len(ls), 1) >= 0.6 and abs(len(l) - len(s)) <= 1:
            return True
    return False


_NUM_RE = re.compile(r"\d{2,}|[一二三四五六七八九十百千万]{2,}")
_NAME_CACHE: dict = {}

# 常见词/口头禅：长得像人名或数字但**绝不能**当编造证据
_STOP_WORDS = {
    "明天", "今天", "昨天", "后天", "现在", "刚才", "以后", "最近", "一起", "千万",
    "什么", "怎么", "这样", "那样", "这个", "那个", "自己", "东西", "事情", "时候",
    "可以", "应该", "知道", "觉得", "没事", "好吧", "哈哈", "谢谢", "拜拜", "晚安",
}
_NUM_OK = {"66", "666", "6666", "233", "520", "888", "999", "11", "22", "00"}


def known_names(root: str = HERE, refresh: float = 300.0) -> set:
    """已知人名库：所有微信会话显示名 + 联系人档案名（带 5 分钟缓存）。

    用途：检测回复里**没根据的人名**（编造细节）。
    """
    now = time.time()
    if _NAME_CACHE.get("names") and now - _NAME_CACHE.get("at", 0) < refresh:
        return _NAME_CACHE["names"]
    names = set()

    def _clean(x: str) -> str:
        x = re.sub(r"\s*\d{5,}\s*$", "", (x or "").strip())   # 去尾部手机号
        x = x.strip("。.、,，!！?？ 　@")                       # 去首尾符号
        return x

    d = os.path.join(root, "corpus", "persona", "contacts")
    try:
        for fn in os.listdir(d):
            if fn.endswith(".md"):
                nm = _clean(fn[:-3])
                if _ok_name(nm):
                    names.add(nm)
    except OSError:
        pass
    try:
        import sys as _sys
        if root not in _sys.path:
            _sys.path.insert(0, root)
        import wxsnap
        for _u, disp in (wxsnap.load_contacts() or {}).items():
            nm = _clean(disp)
            if _ok_name(nm):
                names.add(nm)
    except Exception:
        pass
    _NAME_CACHE.update({"at": now, "names": names})
    return names


def _ok_name(nm: str) -> bool:
    """只收「2-4 个纯汉字、且不是常见词」的名字。

    微信联系人里什么妖魔鬼怪都有（实测 2503 个里混着 `#b`、`$$`、`-8℃`、
    甚至有人就叫人名似的「明天」）—— 不滤掉会疯狂误报编造。
    """
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", nm or "")) and nm not in _STOP_WORDS


def invented_tokens(line: str, allowed: str, names: set, chat: str) -> list:
    """找出这行里**没根据的具体信息**（人名/数字）。

    allowed = 最近上下文 + 检索到的记忆 + 联系人档案（这些地方出现过的就算有根据）。
    没出现在 allowed 里的人名、两位数以上的数字 → 疑似编造。
    """
    hits = []
    for nm in names:
        if nm and nm != chat and nm in line and nm not in allowed:
            hits.append("人名:" + nm)
    for m in _NUM_RE.finditer(line or ""):
        tok = m.group(0)
        if tok in _NUM_OK or len(set(tok)) == 1:      # 666/888/口头禅式数字
            continue
        if tok in allowed:
            continue
        hits.append("数字:" + tok)
    return hits


def adjust_burst(lines: list, max_line_chars: int = 22, max_lines: int = 3) -> list:
    """连发条数后处理：把过长的单条拆成两条，并压到最多 3 条。

    依据真实分布：主人一轮中位 9 字、90% ≤29 字，1-3 条占 82%。
    只在**有明确切分点**（标点/空格）时才拆，避免切出半句话。
    """
    out = []
    for ln in lines:
        ln = (ln or "").strip()
        if not ln:
            continue
        if len(ln) > max_line_chars and len(out) < max_lines - 1:
            cut = None
            mid = len(ln) // 2
            for sep in ("，", ",", " ", "。", "！", "？"):
                idx = ln.find(sep, max(1, mid - 6))
                if 0 < idx < len(ln) - 1:
                    cut = idx + (0 if sep in "，, " else 0)
                    break
            if cut:
                a, b = ln[:cut].strip("，, "), ln[cut:].lstrip("，, ")
                if len(a) >= 2 and len(b) >= 2:
                    out.extend([a, b])
                    continue
        out.append(ln)
    if len(out) > max_lines:                      # 太多条就并尾部
        out = out[:max_lines - 1] + [" ".join(out[max_lines - 1:])]
    return out


def generate_burst(cfg: dict, chat: str, history: list, root: str = HERE) -> tuple:
    """按人设包生成「连发的 1-3 条」。返回 (lines, info)。失败返回 ([], 原因)。

    与 generate() 的区别：这里模仿主人本人的风格，并输出多条（对应微信连发）。
    """
    import persona as persona_mod   # 延迟导入，避免循环
    r = cfg.get("reply") or {}
    key = _api_key(cfg, root)
    if not key:
        return [], "没有 API key"

    pack = persona_mod.Persona(cfg, root)
    if not pack.available():
        return [], "人设包不完整（先跑 corpus/build_persona.py）"

    prof = pack.contact_profile(chat)
    ex = pack.pick_examples(chat, history)
    n_ctx = int(r.get("context_messages", 10))
    recent = "\n".join("%s：%s" % (m.get("who") or "对方", (m.get("text") or "").strip())
                       for m in history[-n_ctx:])
    ctx_recent = " ".join((m.get("text") or "") for m in history[-4:])

    # ---- 动态提示：对方发了几条 → 按真实统计该回几条/多少字 ----
    n_in = 0
    for m in reversed(history):
        if (m.get("who") or "对方") == "我":
            break
        n_in += 1
    hint = ""
    try:
        with open(os.path.join(root, "corpus", "pairs_stats.json"), encoding="utf-8") as f:
            rm = (json.load(f) or {}).get("response_map") or {}
        v = rm.get(str(min(n_in, 4)))
        if v:
            hint = ("\n【本次建议（真实统计）】对方发了 %d 条 → 你**平均回 %.1f 条**"
                    "（常见 1-3 条）、合计约 %.0f 字。照着这个来，别只回两三个字。\n"
                    % (n_in, v.get("lines_avg", 2.0), v.get("chars_avg", 17)))
    except Exception:
        hint = ""
    ex_txt = "\n".join("对方：%s\n主人：%s" % (
        " / ".join(x or "" for x in (e.get("incoming") or [])), e.get("reply") or "")
        for e in ex)

    pcfg = cfg.get("persona") or {}
    ident_answer = pcfg.get("identity_answer") or "本人"
    never = pcfg.get("never_say") or ["AI", "机器人", "助手", "人设", "转告", "主人"]
    redline = (
        "\n【🚫 红线一：你**就是%s本人**，永远第一人称】\n"
        "1. 别人问「你是谁／是不是本人」→ 就答「%s」（自然点，如 `%s啊`）。\n"
        "2. 别人追问「你是不是机器人/AI」→ **别承认、别解释**，当玩笑带过"
        "（`你想多了`／`我像吗`／`滚`）。\n"
        "3. **这些词一个都不许出现**：%s。\n"
        "4. 别人说「他」「你主人」时，那说的就是你本人 —— 用第一人称接，"
        "**禁止**「我转告他」「我跟他说」这种第三方视角。\n"
        "5. ⚠️ **不要复述对方的原词**（尤其错别字、口头语），要用自己的话说。\n"
        "\n【🚫 红线二：绝不替主人做任何约定】\n"
        "1. **不许答应、不许安排、不许定时间地点**。禁止出现这类话："
        "「明天一起玩」「晚上八点」「到时候见」「没问题」「几点」「在哪见」「约好了」"
        "「我请你」「我帮你弄」「包在我身上」。\n"
        "2. 对方提出约定/邀请时，**不要确认、不要承诺**。\n"
        "3. 绝不替主人决定**时间、地点、金钱、行程、是否赴约**——这些只有主人本人能定。\n"
        "4. ⚠️ **但也绝不用空洞话敷衍**！只回「行」「看情况」「再说吧」这类没内容的词，"
        "一眼就是机器人。不接约定时改用这几种**有内容**的方式：\n"
        "   · 说自己的情况（在做啥、忙不忙、累不累）\n"
        "   · 换个话题聊别的（游戏、作业、刚才说的事）\n"
        "   · 反问对方**不含时间地点**的事\n"
        "   · 实在没得说就不回（输出一个空行）\n"
        "5. 拿不准就少说，但**要有实际内容**，不要全是语气词。\n"
    )

    # 检索长期记忆（L1 片段 + L2 事实）：本地库，零 API 费
    mem_txt = ""
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.join(root, "corpus"))
        import memory as memory_mod
        con = memory_mod.connect()
        ctx = " ".join((m.get("text") or "") for m in history[-4:])
        mem = memory_mod.retrieve(con, chat, ctx)
        mem_txt = memory_mod.format_memory(mem)
        con.close()
    except Exception:
        mem_txt = ""

    sys_prompt = (
        "你要**以主人的身份**在微信上给「%s」写回复，说话方式模仿他。\n"
        "%s\n"
        "【主人身份（硬事实）】\n%s\n\n"
        "【风格硬性规则】\n%s\n\n"
        "【对这位联系人的专属档案】\n%s\n\n"
        "%s\n"
        "【真实例句（只学说话方式，不要照抄内容）】\n%s\n"
        "%s\n"
        "【输出格式】只输出主人会发出的内容：每条一行，1-%d 行（对应连发 1-%d 条）。"
        "行内不要有换行符。不要加引号、不要解释、不要编号、不要写「回复：」。"
        "若判断这次不该回，只输出一个空行。"
    ) % (chat, redline, pack.identity or "（未配置）", pack.style_rules, prof or "（无）",
         (mem_txt + "\n") if mem_txt else "",
         ex_txt or "（无）", hint,
         pack.max_lines, pack.max_lines)

    payload_base = {
        "model": r.get("model", "deepseek-chat"),
        "temperature": float(r.get("temperature", 0.7)),
        "max_tokens": int(r.get("max_tokens", 800)),
        "stream": False,
    }
    user_msg = "【最近对话（时间正序）】\n" + (recent or "（无）")

    try:
        sys.path.insert(0, root)
        from guard import Guard
        _guard = Guard(cfg, root)
    except Exception:
        _guard = None

    def _call(extra: str = "") -> str:
        p = dict(payload_base)
        p["messages"] = [
            {"role": "system", "content": sys_prompt + (("\n" + extra) if extra else "")},
            {"role": "user", "content": user_msg},
        ]
        req = urllib.request.Request(
            r.get("endpoint", "https://api.deepseek.com/v1/chat/completions"),
            data=json.dumps(p, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + key},
            method="POST")
        with urllib.request.urlopen(req, timeout=float(r.get("timeout_sec", 60))) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data

    def _parse(text: str) -> list:
        out = []
        for ln in (text or "").splitlines():
            ln = ln.strip().strip('"').strip("“”").strip()
            ln = re.sub(r"^[-*\d.、)）\s]+", "", ln)
            if ln:
                out.append(ln)
        return out[: pack.max_lines]

    def _violations(lines: list) -> list:
        if not _guard:
            return []
        bad = []
        for ln in lines:
            bad += _guard.commitment_hits(ln, ctx_recent)
            bad += _guard.persona_leak_hits(ln)      # 人设泄露（小名/助手/AI 视角）
        return bad

    try:
        data = _call()
    except urllib.error.HTTPError as e:
        return [], "HTTP %s" % e.code
    except Exception as e:
        return [], "请求失败：%s" % type(e).__name__

    try:
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return [], "响应格式异常"
    lines = _parse(text)
    usage = data.get("usage") or {}
    info = {"model": payload_base["model"], "tokens": usage.get("total_tokens"),
            "examples": len(ex), "profile": bool(prof)}

    # ---- 编造细节自检：人名/数字必须在上下文或记忆里出现过 ----
    allowed = ctx_recent + " " + recent + " " + mem_txt + " " + prof
    names = known_names(root)
    invented = []
    for ln in lines:
        invented += invented_tokens(ln, allowed, names, chat)
    if invented:
        info["invented"] = invented
        try:
            data2 = _call("⚠️ 你上一次的回答里出现了没根据的具体信息（%s）。"
                          "上下文里没有的人和数字**一律不许编**，"
                          "不确定就用模糊说法或干脆不提。请重写。" % "、".join(invented[:4]))
            lines2 = _parse((data2["choices"][0]["message"]["content"] or ""))
            inv2 = []
            for ln in lines2:
                inv2 += invented_tokens(ln, allowed, names, chat)
            if lines2 and not inv2:
                lines, info["invented_fixed"] = lines2, invented
            else:
                # 重写也没干净 → 保留原样并记录（宁可有瑕疵，也别越改越差）
                info["invented_kept"] = invented
        except Exception:
            pass

    # ---- 连发条数后处理（过长单条拆两条、压到 ≤3 条）----
    lines = adjust_burst(lines, max_lines=pack.max_lines)

    # ---- 双重红线：确定性过滤（约定 + 人设泄露，主人 2026-09-24 要求）----
    bad = _violations(lines)
    # ---- 复读检测：像复读机一样重复对方的话 → 也要重写 ----
    inc_texts = [m.get("text") or "" for m in history[-3:]
                 if (m.get("who") or "对方") != "我"]
    echoes = [ln for ln in lines if is_echo(ln, inc_texts)]
    if echoes and not bad:
        info["echo_blocked"] = echoes
        try:
            data2 = _call("⚠️ 你上一次的回答「%s」跟对方的话几乎一样，像复读机。"
                          "请换一种说法，用自己的话回应。" % " / ".join(echoes))
            lines2 = _parse((data2["choices"][0]["message"]["content"] or ""))
            lines2 = [l for l in lines2 if not is_echo(l, inc_texts)]
        except Exception:
            lines2 = []
        if lines2:
            info["retried_echo"] = True
            lines, info["echo_blocked"] = lines2, echoes
            bad = _violations(lines)
        else:
            lines = [l for l in lines if l not in echoes] or lines
            bad = _violations(lines)
    if bad:
        keep = [l for l in lines if not _bad_line(l, _guard, ctx_recent)]
        info["blocked"] = bad
        if keep:
            return keep, info
        # 全被拦下 → 带警告重写一次
        try:
            data2 = _call("⚠️ 你上一次的回答「%s」违规了。记住：\n"
                          "① 你就是%s本人，永远第一人称，不许出现 %s 这些词；"
                          "不许复述对方原词；\n"
                          "② 不许替主人做任何约定、不许答应帮忙或借钱、不许定时间地点。\n"
                          "请重写，含糊但不空洞。"
                          % (" / ".join(bad), ident_answer,
                             "「" + "」「".join(str(x) for x in never[:6]) + "」"))
            lines2 = _parse((data2["choices"][0]["message"]["content"] or ""))
        except Exception:
            lines2 = []
        keep2 = [l for l in lines2 if not _bad_line(l, _guard, ctx_recent)]
        if keep2:
            info["retried"] = True
            return keep2, info
        info["blocked_by"] = "redline"
        return [], info
    return lines, info


def _bad_line(line: str, guard_obj, context: str = "") -> bool:
    """该行是否违规（约定 或 人设泄露）。"""
    try:
        if not guard_obj:
            return False
        return bool(guard_obj.commitment_hits(line, context)
                    or guard_obj.persona_leak_hits(line))
    except Exception:
        return False


def _commitment_of(line: str, guard_obj, context: str = "") -> bool:
    try:
        return bool(guard_obj and guard_obj.commitment_hits(line, context))
    except Exception:
        return False


def build_messages(cfg: dict, persona: str, history: list) -> list:
    """history: [{'who': '对方'|'我', 'text': str}, ...]（时间正序）"""
    r = cfg.get("reply") or {}
    n = int(r.get("context_messages", 10))
    lines = []
    for m in history[-n:]:
        who = m.get("who") or "对方"
        lines.append("%s：%s" % (who, (m.get("text") or "").strip()))
    body = "\n".join(lines) if lines else "（暂无上下文）"
    user = (
        "下面是微信上的一段聊天记录（时间正序）：\n"
        "----------------------------------------\n"
        f"{body}\n"
        "----------------------------------------\n"
        "请写出**你要发给对方的下一句**。只输出那句话本身。"
    )
    return [
        {"role": "system", "content": persona or "你是一个中文聊天助手，回复简短自然。"},
        {"role": "user", "content": user},
    ]


def generate(cfg: dict, history: list, root: str = HERE) -> tuple:
    """返回 (text, info)。失败返回 ('', 原因)。"""
    r = cfg.get("reply") or {}
    persona = _load_persona(os.path.join(root, r.get("persona_file") or "persona.md"))
    key = _api_key(cfg, root)
    if not key:
        return "", "没有 API key（secrets.json 里缺 %s）" % (r.get("api_key_env") or "")

    payload = {
        "model": r.get("model", "deepseek-chat"),
        "messages": build_messages(cfg, persona, history),
        "temperature": float(r.get("temperature", 0.7)),
        "max_tokens": int(r.get("max_tokens", 800)),
        "stream": False,
    }
    req = urllib.request.Request(
        r.get("endpoint", "https://api.deepseek.com/v1/chat/completions"),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=float(r.get("timeout_sec", 60))) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 注意：不要把响应体写进日志（可能含敏感信息）
        return "", "HTTP %s" % e.code
    except Exception as e:
        return "", "请求失败：%s" % type(e).__name__

    try:
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return "", "响应格式异常"
    text = text.strip().strip('"').strip("“”")
    # 只取第一行（人设要求只输出一句话）
    if "\n" in text:
        text = text.splitlines()[0].strip()
    limit = int(r.get("max_reply_chars", 120))
    if len(text) > limit:
        text = text[:limit]
    usage = data.get("usage") or {}
    return text, {"model": payload["model"], "tokens": usage.get("total_tokens")}
