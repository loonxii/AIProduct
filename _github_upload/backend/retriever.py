# -*- coding: utf-8 -*-
"""
retriever.py — 检索层

从数十万级真实商品标题中, 用 BM25(中文 bigram) 找出与输入商品最相似的若干商品,
再把它们在数据集里的真实文案取出, 作为大模型生成时的风格参照。

设计说明:
  - 只索引「标题」: 用户输入的是标题, 标题级检索召回最准, 且索引体积可控(百 MB 级)。
  - 字符 bigram: 中文电商标题分词边界模糊, bigram 不依赖词典, 召回更稳, 且无需额外依赖。
  - 去冗余: 命中结果之间做 bigram Jaccard 去重, 避免返回一堆近乎同款的商品。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from typing import Any

import numpy as np

RE_LATIN = re.compile(r"[a-zA-Z][a-zA-Z0-9\-]{0,19}")
RE_DIGIT = re.compile(r"\d{2,}")


def tokenize(text: str) -> list[str]:
    t = (text or "").lower()
    tokens: list[str] = []
    for seg in re.findall(r"[\u4e00-\u9fff]+", t):
        if len(seg) == 1:
            tokens.append(seg)
        else:
            for i in range(len(seg) - 1):
                tokens.append(seg[i : i + 2])
    for w in RE_LATIN.findall(t):
        tokens.append(w)
    for d in RE_DIGIT.findall(t):
        tokens.append("#" + d)
    return tokens


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


class Retriever:
    """线程安全的 BM25 检索器(索引只读, 加载一次常驻内存)"""

    def __init__(self, index_dir: str, db_path: str):
        self.index_dir = index_dir
        self.db_path = db_path
        self.ready = False
        self.error = ""
        self.meta: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._db_lock = threading.Lock()
        self._con: sqlite3.Connection | None = None

        try:
            self._load()
            self.ready = True
        except Exception as e:  # 索引缺失时后端仍可启动, 只是检索降级
            self.error = f"{type(e).__name__}: {e}"

    # ---------------------------------------------------------------- 加载
    def _load(self) -> None:
        npz = np.load(os.path.join(self.index_dir, "index.npz"))
        self.indptr = npz["indptr"]
        self.indices = npz["indices"]
        self.tfreq = npz["tfreq"].astype(np.float32)
        self.idf = npz["idf"]
        self.doc_len = npz["doc_len"].astype(np.float32)
        self.doc_ids = npz["doc_ids"]
        self.doc_cat = npz["doc_cat"]
        self.doc_nd = npz["doc_nd"]
        params = npz["params"]
        self.k1 = float(params[0])
        self.b = float(params[1])
        self.avgdl = float(params[2]) or 1.0
        self.n_docs = int(params[3])

        with open(os.path.join(self.index_dir, "vocab.txt"), "r", encoding="utf-8") as f:
            self.vocab = {t: i for i, t in enumerate(f.read().split("\n")) if t}

        cat_path = os.path.join(self.index_dir, "cat_names.json")
        self.cat_names = json.load(open(cat_path, encoding="utf-8")) if os.path.exists(cat_path) else []
        meta_path = os.path.join(self.index_dir, "index_meta.json")
        self.meta = json.load(open(meta_path, encoding="utf-8")) if os.path.exists(meta_path) else {}

        self._con = sqlite3.connect(self.db_path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA query_only=ON")

    # ---------------------------------------------------------------- 打分
    def _score(self, query: str, cat: str | None = None) -> np.ndarray:
        q_tokens = tokenize(query)
        if not q_tokens:
            return np.zeros(self.n_docs, dtype=np.float32)

        # 长标题里重复出现的 bigram 只算一次, 但给低频词更高权重
        uniq = {}
        for t in q_tokens:
            uniq[t] = uniq.get(t, 0) + 1

        doc_chunks: list[np.ndarray] = []
        w_chunks: list[np.ndarray] = []
        for term, qf in uniq.items():
            j = self.vocab.get(term)
            if j is None:
                continue
            s, e = int(self.indptr[j]), int(self.indptr[j + 1])
            if e <= s:
                continue
            di = self.indices[s:e]
            tf = self.tfreq[s:e]
            dl = self.doc_len[di]
            denom = tf + self.k1 * (1.0 - self.b + self.b * dl / self.avgdl)
            contrib = (self.idf[j] * tf * (self.k1 + 1.0) / denom).astype(np.float32)
            if qf > 1:
                contrib = contrib * (1.0 + 0.25 * (qf - 1))
            doc_chunks.append(di)
            w_chunks.append(contrib)

        if not doc_chunks:
            return np.zeros(self.n_docs, dtype=np.float32)

        di = np.concatenate(doc_chunks)
        wt = np.concatenate(w_chunks)
        scores = np.bincount(di, weights=wt.astype(np.float64), minlength=self.n_docs).astype(np.float32)

        # 命中词种类越多越相关(覆盖度加权), 避免被单个高频词带偏
        hit = np.bincount(di, minlength=self.n_docs)
        scores *= 1.0 + 0.12 * np.minimum(hit, 8)

        if cat:
            try:
                cid = self.cat_names.index(cat)
                scores = np.where(self.doc_cat == cid, scores, 0.0)
            except ValueError:
                pass
        return scores

    # ---------------------------------------------------------------- 检索
    def search_titles(self, query: str, top_k: int = 8, cat: str | None = None) -> list[dict[str, Any]]:
        if not self.ready:
            return []
        scores = self._score(query, cat)
        if not scores.any():
            return []
        pool = min(max(top_k * 12, 60), self.n_docs)
        cand = np.argpartition(-scores, pool - 1)[:pool]
        cand = cand[np.argsort(-scores[cand])]

        qset = set(tokenize(query))
        picked: list[dict[str, Any]] = []
        picked_sets: list[set[str]] = []
        for di in cand:
            sc = float(scores[di])
            if sc <= 0:
                break
            tid = int(self.doc_ids[di])
            title = self._title_cache_get(tid)
            if title is None:
                continue
            ts = set(tokenize(title))
            # 去重: 与已选标题过于相似则跳过
            if any(_jaccard(ts, ps) > 0.72 for ps in picked_sets):
                continue
            picked_sets.append(ts)
            picked.append(
                {
                    "title_id": tid,
                    "title": title,
                    "cat": self.cat_names[int(self.doc_cat[di])] if self.cat_names else "",
                    "score": round(sc, 4),
                    "similarity": round(min(_jaccard(qset, ts) * 2.2, 1.0), 3),
                    "n_desc": int(self.doc_nd[di]),
                }
            )
            if len(picked) >= top_k:
                break
        return picked

    # ---------------------------------------------------------------- 取文案
    def _title_cache_get(self, tid: int) -> str | None:
        with self._db_lock:
            row = self._con.execute("SELECT title FROM titles WHERE id=?", (tid,)).fetchone()
        return row["title"] if row else None

    def fetch_descs(self, title_ids: list[int], per_title: int = 4) -> dict[int, list[dict[str, Any]]]:
        """按标题批量取真实文案(长度更接近目标区间的优先)"""
        if not title_ids:
            return {}
        out: dict[int, list[dict[str, Any]]] = {t: [] for t in title_ids}
        q = (
            "SELECT title_id, desc, clen, style FROM descs WHERE title_id IN (%s) "
            "ORDER BY title_id, clen"
            % ",".join("?" * len(title_ids))
        )
        with self._db_lock:
            rows = self._con.execute(q, title_ids).fetchall()
        for r in rows:
            lst = out[r["title_id"]]
            if len(lst) < per_title * 3:
                lst.append({"desc": r["desc"], "clen": r["clen"], "style": r["style"]})
        # 每条标题下按(质量分)排序取前 per_title 条
        for tid, lst in out.items():
            lst.sort(key=lambda x: _desc_quality(x["clen"]), reverse=True)
            out[tid] = lst[:per_title]
        return out

    def search(
        self,
        query: str,
        top_k: int = 6,
        per_title: int = 4,
        cat: str | None = None,
        with_descs: bool = True,
    ) -> list[dict[str, Any]]:
        hits = self.search_titles(query, top_k=top_k, cat=cat)
        if not hits or not with_descs:
            return hits
        dmap = self.fetch_descs([h["title_id"] for h in hits], per_title=per_title)
        for h in hits:
            h["descs"] = dmap.get(h["title_id"], [])
        return hits

    def flat_references(
        self, hits: list[dict[str, Any]], limit: int = 10, query: str = ""
    ) -> list[dict[str, Any]]:
        """把命中结果摊平成参考文案列表(供提示词使用)

        排序策略: 先看文案与来源标题是否有词汇关联(数据集里存在少量标题-文案错配的脏样本,
        实测约占 5%, 但**不能**直接按重合度过滤——大量优秀文案本就与标题用词不同,
        如「蓝牙音箱」配「声学结构设计」。所以这里只做**降权**而非删除),
        再看长度是否落在电商卖点文案的黄金区间。
        """
        qset = set(tokenize(query)) if query else set()
        refs: list[dict[str, Any]] = []
        for h in hits:
            tset = set(tokenize(h["title"]))
            for d in h.get("descs", []):
                dset = set(tokenize(d["desc"]))
                linked = bool(dset & tset) or bool(dset & qset)
                refs.append(
                    {
                        "desc": d["desc"],
                        "src_title": h["title"],
                        "style": d.get("style", ""),
                        "_linked": linked,
                        "_q": _desc_quality(len(d["desc"])),
                    }
                )
        refs.sort(key=lambda x: (x["_linked"], x["_q"]), reverse=True)
        for r in refs:
            r.pop("_linked", None)
            r.pop("_q", None)
        return refs[:limit]

    def stats(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "error": self.error,
            "n_docs": self.n_docs if self.ready else 0,
            "n_terms": len(self.vocab) if self.ready else 0,
            "categories": self.cat_names if self.ready else [],
            "meta": self.meta,
        }


def _desc_quality(clen: int) -> float:
    """文案质量启发式打分: 偏好 55-110 字(电商卖点文案的黄金区间)"""
    if clen <= 0:
        return -1.0
    if 55 <= clen <= 110:
        return 1.0
    if 40 <= clen < 55 or 110 < clen <= 140:
        return 0.7
    if 25 <= clen < 40:
        return 0.4
    return 0.15
