# -*- coding: utf-8 -*-
"""
store.py — 应用侧持久化(生成历史 / 收藏)

用独立 SQLite 文件, 与只读的语料库分离, 互不影响。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any

DDL = """
CREATE TABLE IF NOT EXISTS generations(
    id           TEXT PRIMARY KEY,
    created_at   INTEGER NOT NULL,
    title        TEXT NOT NULL,
    platform     TEXT NOT NULL,
    style        TEXT NOT NULL,
    n            INTEGER NOT NULL,
    params_json  TEXT NOT NULL DEFAULT '{}',
    refs_json    TEXT NOT NULL DEFAULT '[]',
    items_json   TEXT NOT NULL DEFAULT '[]',
    engine       TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    latency_ms   INTEGER NOT NULL DEFAULT 0,
    favorite     INTEGER NOT NULL DEFAULT 0,
    owner        TEXT NOT NULL DEFAULT 'local'
);
CREATE INDEX IF NOT EXISTS idx_gen_created ON generations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gen_fav ON generations(favorite, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gen_owner ON generations(owner, created_at DESC);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._con = sqlite3.connect(path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.executescript(DDL)
        self._con.commit()

    # ------------------------------------------------------------------ 写
    def save_generation(
        self,
        *,
        title: str,
        platform: str,
        style: str,
        n: int,
        params: dict[str, Any],
        refs: list[dict[str, Any]],
        items: list[dict[str, Any]],
        engine: str,
        model: str,
        latency_ms: int,
        owner: str = "local",
    ) -> str:
        gid = uuid.uuid4().hex[:16]
        with self._lock:
            self._con.execute(
                "INSERT INTO generations(id,created_at,title,platform,style,n,params_json,"
                "refs_json,items_json,engine,model,latency_ms,owner) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    gid,
                    int(time.time()),
                    title,
                    platform,
                    style,
                    n,
                    json.dumps(params, ensure_ascii=False),
                    json.dumps(refs, ensure_ascii=False),
                    json.dumps(items, ensure_ascii=False),
                    engine,
                    model,
                    latency_ms,
                    owner,
                ),
            )
            self._con.commit()
        return gid

    def set_favorite(self, gid: str, fav: bool) -> bool:
        with self._lock:
            cur = self._con.execute(
                "UPDATE generations SET favorite=? WHERE id=?", (1 if fav else 0, gid)
            )
            self._con.commit()
            return cur.rowcount > 0

    def delete(self, gid: str) -> bool:
        with self._lock:
            cur = self._con.execute("DELETE FROM generations WHERE id=?", (gid,))
            self._con.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------ 读
    def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        favorite_only: bool = False,
        keyword: str = "",
    ) -> dict[str, Any]:
        where = []
        args: list[Any] = []
        if favorite_only:
            where.append("favorite=1")
        if keyword:
            where.append("title LIKE ?")
            args.append(f"%{keyword}%")
        w = ("WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self._con.execute(f"SELECT COUNT(*) c FROM generations {w}", args).fetchone()["c"]
            rows = self._con.execute(
                f"SELECT * FROM generations {w} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                args + [limit, offset],
            ).fetchall()
        return {"total": total, "items": [self._row_to_dict(r, brief=True) for r in rows]}

    def get(self, gid: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._con.execute("SELECT * FROM generations WHERE id=?", (gid,)).fetchone()
        return self._row_to_dict(r) if r else None

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._con.execute("SELECT COUNT(*) c FROM generations").fetchone()["c"]
            fav = self._con.execute("SELECT COUNT(*) c FROM generations WHERE favorite=1").fetchone()["c"]
            by_platform = self._con.execute(
                "SELECT platform, COUNT(*) c FROM generations GROUP BY platform ORDER BY c DESC"
            ).fetchall()
        return {
            "total": total,
            "favorites": fav,
            "by_platform": {r["platform"]: r["c"] for r in by_platform},
        }

    @staticmethod
    def _row_to_dict(r: sqlite3.Row, brief: bool = False) -> dict[str, Any]:
        d = {
            "id": r["id"],
            "created_at": r["created_at"],
            "title": r["title"],
            "platform": r["platform"],
            "style": r["style"],
            "n": r["n"],
            "favorite": bool(r["favorite"]),
            "engine": r["engine"],
            "model": r["model"],
            "latency_ms": r["latency_ms"],
        }
        items = json.loads(r["items_json"] or "[]")
        if brief:
            d["preview"] = items[0]["text"][:48] if items else ""
            d["n_items"] = len(items)
        else:
            d["items"] = items
            d["params"] = json.loads(r["params_json"] or "{}")
            d["refs"] = json.loads(r["refs_json"] or "[]")
        return d
