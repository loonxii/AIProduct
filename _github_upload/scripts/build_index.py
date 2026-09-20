# -*- coding: utf-8 -*-
"""
build_index.py — 为商品标题构建检索索引

算法: 中文字符 bigram + 拉丁词元 的倒排索引 + BM25 打分
产出: data/index/index.npz + data/index/vocab.txt + data/index/index_meta.json

用法:
  python scripts/build_index.py --db data/corpus.db --out data/index
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time

import numpy as np

RE_CJK = re.compile(r"[\u4e00-\u9fff]")
RE_LATIN = re.compile(r"[a-zA-Z][a-zA-Z0-9\-]{0,19}")
RE_DIGIT = re.compile(r"\d{2,}")
RE_NOISE = re.compile(r"[\W_]+", re.U)

# 商品标题中的高频无意义词(弱化但不删除)
STOP_TOKENS = {
    "新款", "包邮", "正品", "专柜", "同款", "爆款", "热卖", "特价", "促销",
    "男", "女", "的", "款", "装", "式",
}


def tokenize(text: str) -> list[str]:
    """中文 bigram + 拉丁/数字词元"""
    t = text.lower()
    tokens: list[str] = []

    # 1) 中文连续段 -> bigram (长度为1时退化为 unigram)
    for seg in re.findall(r"[\u4e00-\u9fff]+", t):
        if len(seg) == 1:
            tokens.append(seg)
        else:
            for i in range(len(seg) - 1):
                tokens.append(seg[i : i + 2])

    # 2) 拉丁词元
    for w in RE_LATIN.findall(t):
        tokens.append(w)

    # 3) 数字词元(尺码/型号)
    for d in RE_DIGIT.findall(t):
        tokens.append("#" + d)

    return tokens


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join("data", "corpus.db"), help="清洗后的语料库")
    ap.add_argument("--out", default=os.path.join("data", "index"), help="索引输出目录")
    ap.add_argument("--k1", type=float, default=1.5)
    ap.add_argument("--b", type=float, default=0.75)
    ap.add_argument("--limit", type=int, default=0, help="只索引前 N 个标题, 0=全量")
    ap.add_argument("--log", default="")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    log_path = args.log or os.path.join(args.out, "build_index.log")
    logf = open(log_path, "w", encoding="utf-8")
    t0 = time.time()

    def log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        try:
            print(line, flush=True)
        except Exception:
            pass
        logf.write(line + "\n")
        logf.flush()

    if not os.path.exists(args.db):
        log(f"!! 找不到语料库 {args.db}, 请先运行 clean_data.py")
        return 2

    log(f"读取标题: {args.db}")
    con = sqlite3.connect(args.db)
    sql = "SELECT id, title, cat, n_desc FROM titles ORDER BY id"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    rows = con.execute(sql).fetchall()
    n_docs = len(rows)
    log(f"  标题数 {n_docs:,}")

    cat_names = sorted({r[2] for r in rows})
    cat_id = {c: i for i, c in enumerate(cat_names)}

    # ---------- 构建倒排 ----------
    postings: dict[str, list[tuple[int, int]]] = {}
    doc_len = np.zeros(n_docs, dtype=np.int32)
    doc_ids = np.zeros(n_docs, dtype=np.int64)
    doc_cat = np.zeros(n_docs, dtype=np.int16)
    doc_nd = np.zeros(n_docs, dtype=np.int16)

    for i, (tid, title, cat, nd) in enumerate(rows):
        doc_ids[i] = tid
        doc_cat[i] = cat_id.get(cat, 0)
        doc_nd[i] = min(int(nd or 0), 32000)
        toks = tokenize(title)
        doc_len[i] = len(toks)
        tf: dict[str, int] = {}
        for tk in toks:
            tf[tk] = tf.get(tk, 0) + 1
        for tk, c in tf.items():
            postings.setdefault(tk, []).append((i, c))
        if (i + 1) % 100000 == 0:
            log(f"  已分词 {i+1:,}/{n_docs:,} 词表 {len(postings):,} 用时 {time.time()-t0:.0f}s")

    log(f"  分词完成: 词表 {len(postings):,}, 用时 {time.time()-t0:.0f}s")

    # ---------- 压缩为 CSR 结构 ----------
    terms = sorted(postings.keys())
    n_terms = len(terms)
    nnz = sum(len(v) for v in postings.values())
    log(f"  非零项 {nnz:,}, 开始压缩 (估算显存 {nnz*(4+2)/1024/1024:.0f} MB)")

    indptr = np.zeros(n_terms + 1, dtype=np.int64)
    indices = np.empty(nnz, dtype=np.int32)
    tfreq = np.empty(nnz, dtype=np.int16)

    pos = 0
    df = np.empty(n_terms, dtype=np.int32)
    for j, tk in enumerate(terms):
        lst = postings[tk]
        lst.sort(key=lambda x: x[0])
        k = len(lst)
        df[j] = k
        indptr[j] = pos
        for m, (di, c) in enumerate(lst):
            indices[pos + m] = di
            tfreq[pos + m] = c
        pos += k
    indptr[n_terms] = pos
    del postings

    # idf (BM25 概率型)
    N = n_docs
    df_f = df.astype(np.float64)
    idf = np.log(1.0 + (N - df_f + 0.5) / (df_f + 0.5)).astype(np.float32)

    avgdl = float(doc_len.mean()) if n_docs else 0.0

    np.savez_compressed(
        os.path.join(args.out, "index.npz"),
        indptr=indptr,
        indices=indices,
        tfreq=tfreq,
        idf=idf,
        doc_len=doc_len,
        doc_ids=doc_ids,
        doc_cat=doc_cat,
        doc_nd=doc_nd,
        params=np.array([args.k1, args.b, avgdl, float(N)], dtype=np.float64),
    )
    with open(os.path.join(args.out, "vocab.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(terms))
    with open(os.path.join(args.out, "cat_names.json"), "w", encoding="utf-8") as f:
        json.dump(cat_names, f, ensure_ascii=False)
    meta = {
        "n_docs": n_docs,
        "n_terms": n_terms,
        "nnz": int(nnz),
        "avgdl": round(avgdl, 3),
        "k1": args.k1,
        "b": args.b,
        "n_cats": len(cat_names),
        "tokenizer": "cjk-bigram+latin+digit",
        "index_file": "index.npz",
        "vocab_file": "vocab.txt",
        "db": os.path.basename(args.db),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 1),
        "size_MB": round(os.path.getsize(os.path.join(args.out, "index.npz")) / 1024 / 1024, 1),
    }
    with open(os.path.join(args.out, "index_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # 类目分布
    cat_dist = {}
    for _, _, cat, _ in rows:
        cat_dist[cat] = cat_dist.get(cat, 0) + 1
    with open(os.path.join(args.out, "cat_dist.json"), "w", encoding="utf-8") as f:
        json.dump(cat_dist, f, ensure_ascii=False, indent=2)

    log(f"完成: index.npz {meta['size_MB']} MB, 词表 {n_terms:,}, 平均标题长度 {avgdl:.1f} token, 耗时 {meta['elapsed_sec']}s")
    logf.close()
    return 0


if __name__ == "__main__":
    import traceback

    _err = "build_index_error.txt"
    for _i, _a in enumerate(sys.argv):
        if _a == "--out" and _i + 1 < len(sys.argv):
            _err = os.path.join(sys.argv[_i + 1], "build_index_error.txt")
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        tb = traceback.format_exc()
        try:
            with open(_err, "a", encoding="utf-8") as _f:
                _f.write(tb + "\n")
        except Exception:
            pass
        sys.exit(1)
