# -*- coding: utf-8 -*-
"""memory.py —— L1/L2 记忆库（SQLite）

一张表存一类东西，全部可读可查。检索用 FTS5 全文索引（零成本），
不依赖向量库也能用；将来要加语义检索，在同一库里加 embedding 表即可。

表结构：
  episode       L1 会话片段：谁、什么时候、聊了什么、多重要
  fact          L2 长期事实：关于主人的稳定信息（工作/喜好/关系/约定）
  contact       L4 联系人档案（从 persona/contacts 同步进来，便于检索）
  ingest        增量水位：每个会话处理到哪条了（断点续跑）
  episode_fts / fact_fts   FTS5 索引
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "memory.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS episode(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat TEXT NOT NULL,
  start_ts INTEGER, end_ts INTEGER,
  date TEXT,
  topic TEXT,
  summary TEXT,
  importance INTEGER DEFAULT 2,
  msg_count INTEGER,
  n_mine INTEGER,
  model TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ep_chat ON episode(chat, start_ts);

CREATE TABLE IF NOT EXISTS fact(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject TEXT DEFAULT '主人',
  predicate TEXT,
  object TEXT,
  statement TEXT NOT NULL,
  chat TEXT,
  ts INTEGER,
  confidence REAL DEFAULT 0.7,
  episode_id INTEGER,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_fact_pred ON fact(predicate);
CREATE INDEX IF NOT EXISTS idx_fact_chat ON fact(chat, ts);

CREATE TABLE IF NOT EXISTS contact(
  chat TEXT PRIMARY KEY,
  relation TEXT,
  register TEXT,
  notes TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS ingest(
  chat TEXT PRIMARY KEY,
  last_ts INTEGER DEFAULT 0,
  last_local_id INTEGER DEFAULT 0,
  n_episode INTEGER DEFAULT 0,
  updated_at TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS episode_fts USING fts5(
  summary, topic, chat UNINDEXED, content='episode', content_rowid='id');
CREATE VIRTUAL TABLE IF NOT EXISTS fact_fts USING fts5(
  statement, predicate, content='fact', content_rowid='id');
"""


def connect(path: str = DB) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init(path: str = DB) -> sqlite3.Connection:
    con = connect(path)
    con.executescript(SCHEMA)
    con.commit()
    return con


def add_episode(con, chat, start_ts, end_ts, topic, summary, importance,
                msg_count, n_mine, model="qwen2.5:7b") -> int:
    cur = con.execute(
        "INSERT INTO episode(chat,start_ts,end_ts,date,topic,summary,importance,"
        "msg_count,n_mine,model,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (chat, start_ts, end_ts,
         time.strftime("%Y-%m-%d", time.localtime(start_ts or 0)),
         topic, summary, int(importance or 2), msg_count, n_mine, model,
         time.strftime("%Y-%m-%d %H:%M:%S")))
    eid = cur.lastrowid
    con.execute("INSERT INTO episode_fts(rowid,summary,topic,chat) VALUES(?,?,?,?)",
                (eid, summary or "", topic or "", chat))
    con.commit()
    return eid


def _bigrams(s: str) -> set:
    s = "".join(ch for ch in (s or "") if ch.strip())
    return {s[i:i + 2] for i in range(max(len(s) - 1, 0))} or ({s} if s else set())


def similar_fact_exists(con, statement: str, threshold: float = 0.72) -> bool:
    """同一条事实不重复入库（bigram Jaccard 相似度判重）。"""
    a = _bigrams(statement)
    if not a:
        return False
    for (st,) in con.execute("SELECT statement FROM fact ORDER BY id DESC LIMIT 400"):
        b = _bigrams(st)
        if not b:
            continue
        inter = len(a & b)
        if inter / max(len(a | b), 1) >= threshold:
            return True
    return False


def normalize_statement(st: str) -> str:
    """统一主语：模型偶尔写成「我」，一律改成「主人」。"""
    st = (st or "").strip()
    if st.startswith("我") and not st.startswith("我们"):
        st = "主人" + st[1:]
    return st


def add_fact(con, statement, predicate="", obj="", chat="", ts=0,
             confidence=0.7, episode_id=None, subject="主人") -> int:
    cur = con.execute(
        "INSERT INTO fact(subject,predicate,object,statement,chat,ts,confidence,"
        "episode_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (subject, predicate, obj, statement, chat, ts, confidence, episode_id,
         time.strftime("%Y-%m-%d %H:%M:%S")))
    fid = cur.lastrowid
    con.execute("INSERT INTO fact_fts(rowid,statement,predicate) VALUES(?,?,?)",
                (fid, statement, predicate or ""))
    con.commit()
    return fid


def get_ingest(con, chat) -> tuple:
    row = con.execute("SELECT last_ts,last_local_id,n_episode FROM ingest WHERE chat=?",
                      (chat,)).fetchone()
    return tuple(row) if row else (0, 0, 0)


def set_ingest(con, chat, last_ts, last_local_id, n_episode=0):
    con.execute(
        "INSERT INTO ingest(chat,last_ts,last_local_id,n_episode,updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(chat) DO UPDATE SET "
        "last_ts=excluded.last_ts, last_local_id=excluded.last_local_id, "
        "n_episode=excluded.n_episode, updated_at=excluded.updated_at",
        (chat, last_ts, last_local_id, n_episode,
         time.strftime("%Y-%m-%d %H:%M:%S")))
    con.commit()


def already_done_range(con, chat, start_ts) -> bool:
    """这段是否已经总结过（防重复）。"""
    row = con.execute(
        "SELECT 1 FROM episode WHERE chat=? AND start_ts=? LIMIT 1",
        (chat, start_ts)).fetchone()
    return bool(row)


_TRANSIENT_WORDS = ("今天", "明天", "后天", "今晚", "这周", "本周", "下周", "这个月",
                    "待会", "一会儿", "马上", "现在", "正在", "刚刚")


def is_transient(statement: str) -> bool:
    """这句话是不是「只在那几天有效」的临时信息。

    「主人明天要交作业」过一周就没意义了 —— 这类事实检索时要快速衰减。
    """
    s = statement or ""
    return any(w in s for w in _TRANSIENT_WORDS)


def retrieve(con, chat: str, context: str, k_ep: int = 3, k_fact: int = 6,
             facts_total_limit: int = 2000) -> dict:
    """按当前对话上下文检索相关记忆。

    打分 = 字符 bigram 重合 + 同会话加分 + 时间新鲜度加分。
    数据量不大（几百条），直接全表打分比 FTS5 中文分词更稳。

    ⚠️ 临时事实（含「明天/这周」这类词）衰减快得多 —— 否则模型会拿
    一周前的「明天要交作业」当成现在的事。
    """
    ctx = _bigrams(context)
    import time as _t
    now = _t.time()

    facts = []
    for r in con.execute(
            "SELECT id,statement,predicate,object,chat,ts,confidence FROM fact "
            "ORDER BY id DESC LIMIT ?", (facts_total_limit,)):
        fid, st, pred, obj, fchat, ts, conf = r
        score = len(ctx & _bigrams(st)) * 1.0
        if fchat == chat:
            score += 1.5
        if ts:
            age_days = (now - ts) / 86400.0
            score += max(0.0, 2.0 - age_days / 30.0)
            if is_transient(st):
                # 临时事实：3 天后基本作废，7 天后彻底压掉
                score *= 0.15 if age_days > 7 else (0.5 if age_days > 3 else 1.0)
        score *= (conf or 0.7)
        facts.append((score, {"id": fid, "statement": st, "predicate": pred,
                              "object": obj, "chat": fchat, "ts": ts}))
    facts.sort(key=lambda x: -x[0])

    eps = []
    for r in con.execute(
            "SELECT id,chat,date,topic,summary,importance,start_ts FROM episode "
            "ORDER BY start_ts DESC LIMIT 1200"):
        eid, echat, date, topic, summary, imp, sts = r
        score = len(ctx & _bigrams((summary or "") + (topic or ""))) * 1.0
        if echat == chat:
            score += 1.2
        score += (imp or 2) * 0.25
        if sts:
            age_days = (now - sts) / 86400.0
            score += max(0.0, 1.5 - age_days / 30.0)
        eps.append((score, {"id": eid, "chat": echat, "date": date, "topic": topic,
                            "summary": summary, "importance": imp}))
    eps.sort(key=lambda x: -x[0])

    return {"facts": [f for _, f in facts[:k_fact]],
            "episodes": [e for _, e in eps[:k_ep]]}


def format_memory(mem: dict) -> str:
    """把检索结果排成给云端模型看的文本块。"""
    out = []
    if mem.get("facts"):
        out.append("【关于主人的已知信息】")
        for f in mem["facts"]:
            out.append("- %s" % f["statement"])
    if mem.get("episodes"):
        out.append("【相关的过往片段】")
        for e in mem["episodes"]:
            out.append("- [%s %s] %s" % (e.get("date"), e.get("topic") or "", e.get("summary")))
    return "\n".join(out)


def sync_contacts(con, contacts_dir: str):
    """把 persona/contacts/*.md 同步进库（便于检索）。"""
    import glob
    n = 0
    for md in sorted(glob.glob(os.path.join(contacts_dir, "*.md"))):
        name = os.path.basename(md)[:-3]
        try:
            body = open(md, encoding="utf-8").read()
        except OSError:
            continue
        # 从硬事实头里抠一句"关系与场景"
        relation = ""
        m = body.find("## 关系与场景")
        if m >= 0:
            relation = body[m + len("## 关系与场景"):].strip().split("\n")[0].strip()
        con.execute(
            "INSERT INTO contact(chat,relation,register,notes,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(chat) DO UPDATE SET "
            "relation=excluded.relation, notes=excluded.notes, "
            "updated_at=excluded.updated_at",
            (name, relation, "", body[:4000],
             time.strftime("%Y-%m-%d %H:%M:%S")))
        n += 1
    con.commit()
    return n


def search(con, q, limit=8, kind="episode"):
    """FTS5 关键词检索。中文短查询会退化，配合 Python 侧 bigram 兜底。"""
    tbl = "episode_fts" if kind == "episode" else "fact_fts"
    src = "episode" if kind == "episode" else "fact"
    try:
        rows = con.execute(
            f"SELECT s.* FROM {tbl} f JOIN {src} s ON s.id=f.rowid "
            f"WHERE {tbl} MATCH ? ORDER BY rank LIMIT ?", (q, limit)).fetchall()
        cols = [d[0] for d in con.execute(f"SELECT * FROM {src} LIMIT 1").description]
        return [dict(zip(cols, r)) for r in rows]
    except sqlite3.OperationalError:
        return []


def stats(con) -> dict:
    out = {}
    for t in ("episode", "fact", "contact", "ingest"):
        try:
            out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            out[t] = 0
    return out


if __name__ == "__main__":
    con = init()
    print(json.dumps(stats(con), ensure_ascii=False, indent=2))
    print("库文件：%s" % DB)
