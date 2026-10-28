#!/usr/bin/env python3
"""seekdb 文档记忆：切块入库 + 全文/向量混合检索。

    python docmem.py init
    python docmem.py ingest <目录> [--force]
    python docmem.py search "<问题>" [--top 5] [--dir 子目录] [--json]
    python docmem.py stats
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

import pymysql
from pymysql.cursors import DictCursor

DB = dict(
    host=os.getenv("DOCMEM_DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DOCMEM_DB_PORT", "2881")),
    user=os.getenv("DOCMEM_DB_USER", "root@sys"),
    password=os.getenv("DOCMEM_DB_PASSWORD", ""),
    charset="utf8mb4",
    cursorclass=DictCursor,
    autocommit=True,
    connect_timeout=10,
)
DB_NAME = os.getenv("DOCMEM_DB_NAME", "docmem")

EMBED_URL = os.getenv("DOCMEM_EMBED_URL", "http://127.0.0.1:11434/api/embed")
EMBED_MODEL = os.getenv("DOCMEM_EMBED_MODEL", "quentinz/bge-small-zh-v1.5")
EMBED_DIM = int(os.getenv("DOCMEM_EMBED_DIM", "512"))
EMBED_BATCH = 16

CHUNK_MAX = 480
POOL = 30
RRF_K = 60
TITLE_W = 0.01
VEC_MAX_DIST = 0.55

SUFFIXES = {".md", ".markdown", ".txt"}
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
FRONT_KEYS = ("title", "tags")

# 口语疑问词对全文召回只有噪声，先剥掉再交给 MATCH AGAINST。
FILLERS = (
    "上次", "上回", "之前", "那个", "这篇", "那篇", "请问", "帮我", "帮我看", "找一下", "查一下",
    "是什么", "什么", "怎么样", "怎么", "怎样", "如何", "为什么", "多少", "哪些", "哪个", "是不是",
    "还记得", "记得", "说了", "讲的", "写的", "提到",
)

DDL = [
    "CREATE DATABASE IF NOT EXISTS `{db}` DEFAULT CHARACTER SET utf8mb4",
    """
    CREATE TABLE IF NOT EXISTS `{db}`.docs (
      id INT NOT NULL AUTO_INCREMENT,
      path VARCHAR(512) NOT NULL,
      dir VARCHAR(255) NOT NULL,
      title VARCHAR(200) NOT NULL,
      tags VARCHAR(200) NOT NULL DEFAULT '',
      mtime DATETIME NOT NULL,
      sha CHAR(40) NOT NULL,
      PRIMARY KEY (id),
      UNIQUE KEY uk_path (path),
      KEY idx_dir (dir)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS `{db}`.chunks (
      id INT NOT NULL AUTO_INCREMENT,
      doc_id INT NOT NULL,
      ord INT NOT NULL,
      heading VARCHAR(200) NOT NULL DEFAULT '',
      line_no INT NOT NULL,
      body TEXT NOT NULL,
      ft TEXT NOT NULL,
      emb VECTOR({dim}) NOT NULL,
      PRIMARY KEY (id),
      KEY idx_doc (doc_id),
      FULLTEXT INDEX ft_idx (ft) WITH PARSER ngram
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS `{db}`.query_log (
      id BIGINT NOT NULL AUTO_INCREMENT,
      ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      question VARCHAR(500) NOT NULL,
      hits INT NOT NULL,
      top_score DOUBLE DEFAULT NULL,
      latency_ms INT NOT NULL,
      PRIMARY KEY (id)
    )
    """,
]


@contextmanager
def connect(with_db: bool = True) -> Iterator[DictCursor]:
    kwargs = dict(DB)
    if with_db:
        kwargs["database"] = DB_NAME
    conn = pymysql.connect(**kwargs)
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def embed(texts: list[str]) -> list[list[float]]:
    """调 embedding 服务；/api/embed 走 Ollama 协议，其余按 OpenAI 兼容协议。"""
    payload = json.dumps({"model": EMBED_MODEL, "input": texts}, ensure_ascii=False).encode()
    req = urllib.request.Request(EMBED_URL, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode())

    if EMBED_URL.rstrip("/").endswith("/api/embed"):
        vectors = data.get("embeddings") or []
    else:
        vectors = [item["embedding"] for item in data.get("data") or []]

    if len(vectors) != len(texts):
        raise RuntimeError(f"embedding 数量不匹配：返回 {len(vectors)}，请求 {len(texts)}")
    for vec in vectors:
        if len(vec) != EMBED_DIM:
            raise RuntimeError(f"向量维度是 {len(vec)}，和 DOCMEM_EMBED_DIM={EMBED_DIM} 不一致")
    return vectors


def vec_literal(vec: Iterable[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


def parse_front_matter(text: str) -> tuple[dict[str, str], str, int]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text, 1
    meta: dict[str, str] = {}
    for idx in range(1, len(lines)):
        line = lines[idx]
        if line.strip() == "---":
            return meta, "\n".join(lines[idx + 1:]), idx + 2
        if ":" in line:
            key, value = line.split(":", 1)
            if key.strip() in FRONT_KEYS:
                meta[key.strip()] = value.strip()
    return {}, text, 1


def chunk_markdown(body: str, first_line: int = 1) -> list[tuple[str, int, str]]:
    """按标题分段，超长再按段落切开，返回 (标题路径, 起始行号, 正文)。"""
    chunks: list[tuple[str, int, str]] = []
    path: list[str] = []
    buf: list[str] = []
    start: int | None = None
    size = 0

    def flush() -> None:
        nonlocal buf, start, size
        text = "\n".join(buf).strip()
        if text:
            chunks.append((" / ".join(path), start or first_line, text))
        buf, start, size = [], None, 0

    for offset, raw in enumerate(body.splitlines()):
        line_no = first_line + offset
        line = raw.rstrip()
        match = HEADING_RE.match(line)
        if match:
            flush()
            level, title = len(match.group(1)), match.group(2).strip()
            path = path[: level - 1] + [title]
            continue
        if not line.strip():
            if size > CHUNK_MAX:
                flush()
            elif buf:
                buf.append("")
            continue
        if start is None:
            start = line_no
        buf.append(line)
        size += len(line)
        if size > CHUNK_MAX:
            flush()
    flush()
    return chunks


def index_text(heading: str, body: str) -> str:
    # 只带叶子标题：整条路径含文档名，会让同一文档的每块都命中文档名。
    leaf = heading.split(" / ")[-1] if heading else ""
    return f"{leaf}\n{body}" if leaf else body


def iter_docs(root: Path) -> list[Path]:
    return [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in SUFFIXES]


def cmd_init(_: argparse.Namespace) -> int:
    with connect(with_db=False) as cur:
        for stmt in DDL:
            cur.execute(stmt.format(db=DB_NAME, dim=EMBED_DIM))
    print(f"已就绪：库 {DB_NAME}（向量维度 {EMBED_DIM}）")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 2

    docs, skipped = 0, 0
    chunks_total = 0
    for path in iter_docs(root):
        raw = path.read_text(encoding="utf-8", errors="ignore")
        sha = hashlib.sha1(raw.encode()).hexdigest()
        rel = path.relative_to(root).as_posix()

        with connect() as cur:
            cur.execute("SELECT id, sha FROM docs WHERE path = %s", (rel,))
            old = cur.fetchone()
        if old and old["sha"] == sha and not args.force:
            skipped += 1
            continue

        meta, body, first_line = parse_front_matter(raw)
        parts = chunk_markdown(body, first_line)
        if not parts:
            skipped += 1
            continue

        doc = (
            rel,
            path.parent.relative_to(root).as_posix() if path.parent != root else ".",
            meta.get("title") or path.stem,
            meta.get("tags", ""),
            datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            sha,
        )

        with connect() as cur:
            if old:
                doc_id = old["id"]
                cur.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
                cur.execute(
                    "UPDATE docs SET dir=%s, title=%s, tags=%s, mtime=%s, sha=%s WHERE id=%s",
                    doc[1:] + (doc_id,),
                )
            else:
                cur.execute(
                    "INSERT INTO docs (path, dir, title, tags, mtime, sha) VALUES (%s,%s,%s,%s,%s,%s)", doc
                )
                doc_id = cur.lastrowid

            for base in range(0, len(parts), EMBED_BATCH):
                batch = parts[base: base + EMBED_BATCH]
                vectors = embed([index_text(h, b) for h, _, b in batch])
                cur.executemany(
                    "INSERT INTO chunks (doc_id, ord, heading, line_no, body, ft, emb) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    [
                        (doc_id, base + i, heading, line_no, body, index_text(heading, body), vec_literal(vec))
                        for i, ((heading, line_no, body), vec) in enumerate(zip(batch, vectors))
                    ],
                )
        docs += 1
        chunks_total += len(parts)
        print(f"+ {rel}  {len(parts)} 段")

    print(f"完成：更新 {docs} 篇 / {chunks_total} 段，跳过 {skipped} 篇未变化")
    return 0


def clean_query(question: str) -> str:
    text = question
    for word in FILLERS:
        text = text.replace(word, " ")
    text = " ".join(text.split())
    return text if len(text) >= 2 else question


def bigrams(text: str) -> set[str]:
    chars = [c for c in text if c.strip()]
    return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)} or set(chars)


def cmd_search(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    question = args.question
    ft_query = clean_query(question)
    where, params = "", []
    if args.dir:
        where = " WHERE d.dir LIKE %s"
        params = [f"{args.dir.rstrip('/')}%"]

    with connect() as cur:
        cur.execute(
            "SELECT c.id, MATCH(c.ft) AGAINST (%s IN NATURAL LANGUAGE MODE) AS score "
            "FROM chunks c JOIN docs d ON d.id = c.doc_id"
            + (where + " AND " if where else " WHERE ")
            + "MATCH(c.ft) AGAINST (%s IN NATURAL LANGUAGE MODE) "
            "ORDER BY score DESC LIMIT %s",
            [ft_query, *params, ft_query, POOL],
        )
        fts = cur.fetchall()

        cur.execute(
            f"SELECT c.id, COSINE_DISTANCE(c.emb, %s) AS dist FROM chunks c JOIN docs d ON d.id = c.doc_id"
            + where + " ORDER BY dist ASC LIMIT %s",
            [vec_literal(embed([question])[0]), *params, POOL],
        )
        vecs = cur.fetchall()

        fused: dict[int, dict] = {}
        for rank, row in enumerate(fts, 1):
            item = fused.setdefault(row["id"], {"id": row["id"], "rrf": 0.0, "ft_rank": None, "vec_rank": None})
            item["rrf"] += 1.0 / (RRF_K + rank)
            item["ft_rank"] = rank
        for rank, row in enumerate(vecs, 1):
            item = fused.setdefault(row["id"], {"id": row["id"], "rrf": 0.0, "ft_rank": None, "vec_rank": None})
            item["rrf"] += 1.0 / (RRF_K + rank)
            item["vec_rank"] = rank
            item["dist"] = float(row["dist"])

        ranked = sorted(fused.values(), key=lambda x: x["rrf"], reverse=True)[: max(args.top * 3, 15)]
        detail: dict[int, dict] = {}
        if ranked:
            ids = [item["id"] for item in ranked]
            cur.execute(
                "SELECT c.id, c.heading, c.line_no, c.body, d.path, d.title "
                f"FROM chunks c JOIN docs d ON d.id = c.doc_id WHERE c.id IN ({','.join(['%s'] * len(ids))})",
                ids,
            )
            detail = {row["id"]: row for row in cur.fetchall()}

    grams = bigrams(ft_query)
    hits = []
    for item in ranked:
        row = detail.get(item["id"])
        if not row:
            continue
        # 全文没命中、向量又离得远的，基本是硬凑的邻居。
        if item["ft_rank"] is None and item.get("dist", 1.0) > VEC_MAX_DIST:
            continue
        coverage = len(grams & bigrams(row["title"])) / max(len(grams), 1)
        hits.append(
            {
                "score": round(item["rrf"] + TITLE_W * coverage, 5),
                "title_cov": round(coverage, 3),
                "ft_rank": item["ft_rank"],
                "vec_rank": item["vec_rank"],
                "dist": round(item.get("dist", 1.0), 4),
                "path": row["path"],
                "title": row["title"],
                "heading": row["heading"],
                "line_no": row["line_no"],
                "body": row["body"],
            }
        )
    hits.sort(key=lambda h: h["score"], reverse=True)
    hits = hits[: args.top]

    elapsed = int((time.perf_counter() - started) * 1000)
    with connect() as cur:
        cur.execute(
            "INSERT INTO query_log (question, hits, top_score, latency_ms) VALUES (%s,%s,%s,%s)",
            (question[:500], len(hits), hits[0]["score"] if hits else None, elapsed),
        )

    result = {
        "question": question,
        "hits": hits,
        "candidates": {"fulltext": len(fts), "vector": len(vecs)},
        "latency_ms": elapsed,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if not hits:
        print(f"没有找到相关片段（全文 {len(fts)} 条、向量 {len(vecs)} 条候选都没过门槛）。换个说法再试。")
        return 0
    print(f"命中 {len(hits)} 段 · {elapsed} ms")
    for rank, hit in enumerate(hits, 1):
        where = f"## {hit['heading']}" if hit["heading"] else ""
        print(f"\n{rank}. {hit['path']}:{hit['line_no']}  {where}")
        print("   " + hit["body"].replace("\n", "\n   ")[:400])
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    with connect() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM docs")
        docs = int(cur.fetchone()["n"])
        cur.execute("SELECT COUNT(*) AS n FROM chunks")
        chunks = int(cur.fetchone()["n"])
        cur.execute("SELECT COUNT(*) AS n, ROUND(AVG(latency_ms), 1) AS avg_ms FROM query_log")
        log = cur.fetchone()
        cur.execute("SELECT ts, question, hits, latency_ms FROM query_log ORDER BY id DESC LIMIT 8")
        recent = [{**row, "ts": row["ts"].strftime("%Y-%m-%d %H:%M")} for row in cur.fetchall()]
    print(f"文档 {docs} 篇 / 片段 {chunks} 段 / 查询 {int(log['n'])} 次，平均 {float(log['avg_ms'] or 0)} ms")
    for row in recent:
        print(f"  {row['ts']}  {row['hits']} 段  {row['latency_ms']} ms  {row['question']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="docmem", description="seekdb 文档记忆")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建库建表").set_defaults(func=cmd_init)

    ingest = sub.add_parser("ingest", help="把目录灌进库")
    ingest.add_argument("path")
    ingest.add_argument("--force", action="store_true", help="忽略内容哈希，强制重建")
    ingest.set_defaults(func=cmd_ingest)

    search = sub.add_parser("search", help="混合检索")
    search.add_argument("question")
    search.add_argument("--top", type=int, default=5)
    search.add_argument("--dir", default=None, help="只在某个子目录里找")
    search.add_argument("--json", action="store_true")
    search.set_defaults(func=cmd_search)

    sub.add_parser("stats", help="看看库和查询台账").set_defaults(func=cmd_stats)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
