# -*- coding: utf-8 -*-
"""persona.py —— 人设包加载与例句检索（P2）

三层材料：
  L3  style_rules.md      风格硬规则（统计生成，不许违背）
  L4  contacts/<名>.md    对这位联系人的专属档案
  L3  examples.jsonl      真实「对方说X → 主人回Y」例句，按相关性检索

检索用**字符 bigram 重合度**（不依赖向量库，够用且零成本）。中文短句场景下
bigram 重合比关键词更稳。
"""
from __future__ import annotations

import json
import os
import random
from functools import lru_cache

HERE = os.path.dirname(os.path.abspath(__file__))


def _bigrams(s: str) -> set:
    s = "".join(ch for ch in (s or "") if ch.strip())
    return {s[i:i + 2] for i in range(max(len(s) - 1, 0))} or ({s} if s else set())


class Persona:
    def __init__(self, cfg: dict, root: str = HERE):
        self.cfg = cfg
        p = cfg.get("persona") or {}
        self.mode = p.get("mode", "imitate")
        d = os.path.join(root, p.get("dir", "corpus/persona"))
        self.dir = d
        self.style_rules = self._read(os.path.join(d, p.get("style_rules", "style_rules.md")))
        self.identity = self._read(os.path.join(d, "identity.md"))
        self.contacts_dir = os.path.join(d, p.get("contacts_dir", "contacts"))
        self.max_lines = int(p.get("max_lines", 3))
        self.k = int(p.get("examples_per_reply", 4))
        self.examples = self._load_examples(os.path.join(d, p.get("examples", "examples.jsonl")))

    @staticmethod
    def _read(path):
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    @staticmethod
    def _load_examples(path):
        out = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        out.append(json.loads(line))
        except OSError:
            pass
        return out

    def contact_profile(self, chat: str) -> str:
        if not chat:
            return ""
        path = os.path.join(self.contacts_dir, chat.replace("/", "_") + ".md")
        return self._read(path)

    def pick_examples(self, chat: str, recent: list, k: int | None = None) -> list:
        """同联系人优先 + 与最近对话相关性排序。"""
        k = k or self.k
        ctx = " ".join((m.get("text") or "") for m in (recent or [])[-4:])
        cb = _bigrams(ctx)
        scored = []
        for e in self.examples:
            inc = " ".join(x or "" for x in (e.get("incoming") or []))
            score = len(cb & _bigrams(inc))
            if e.get("chat") == chat:
                score += 3           # 同一个人的说话方式最贴近
            scored.append((score, e))
        scored.sort(key=lambda x: -x[0])
        top = [e for s, e in scored[:k * 4] if s > 0] or [e for _, e in scored[:k * 3]]
        random.seed(hash(chat + ctx) & 0xFFFF)
        random.shuffle(top)
        return top[:k]

    def available(self) -> bool:
        return bool(self.style_rules and self.examples)
