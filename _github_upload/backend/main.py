# -*- coding: utf-8 -*-
"""
main.py — FastAPI 应用入口

路由总览
--------
GET  /api/health            健康检查 + 索引/生成通道状态
GET  /api/channels          可用生成通道清单(供进入页使用)
POST /api/channels/test     连通性探测 + 拉取可用模型列表
GET  /api/platforms         平台风格清单
GET  /api/styles            文案风格清单
GET  /api/categories        语料库类目清单
POST /api/retrieve          只做检索, 返回相似商品及其真实文案
POST /api/prepare           组装提示词 + 检索结果 (供前端直连云端模型)
POST /api/finalize          解析模型输出 + 合规检查 + 落库 (配合 /api/prepare)
POST /api/generate          一步到位: 检索 + 服务端模型 + 解析 + 落库
POST /api/generate/stream   同上, 但以 SSE 推送真实阶段进度(驱动前端进度条)
GET  /api/history           历史记录
POST /api/history/{id}/favorite
DELETE /api/history/{id}
GET  /api/stats             语料库与使用统计
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import prompts as P
from .config import settings
from .llm_proxy import LLMError, chat, probe, provider_status, resolve, stream_chat
from .retriever import Retriever
from .schemas import (
    CopyItem,
    FinalizeRequest,
    GenerateRequest,
    GenerateResponse,
    LLMOptions,
    PrepareResponse,
    ReferenceItem,
    RetrieveRequest,
    TitleHit,
)
from .store import Store

app = FastAPI(
    title="商品描述文案助手",
    description="基于百万级真实商品文案的检索增强生成助手",
    version="1.0.0",
    docs_url="/docs" if settings.ENABLE_DOCS else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.CORS_ORIGINS == "*" else [o.strip() for o in settings.CORS_ORIGINS.split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs(settings.DATA_DIR, exist_ok=True)
retriever = Retriever(settings.INDEX_DIR, settings.CORPUS_DB)
store = Store(settings.APP_DB)


# ---------------------------------------------------------------- 基础接口


@app.get("/api/health")
def health() -> dict[str, Any]:
    st = retriever.stats()
    return {
        "ok": True,
        "retriever": {
            "ready": st["ready"],
            "error": st["error"],
            "titles": st["n_docs"],
            "terms": st["n_terms"],
            "built_at": (st.get("meta") or {}).get("built_at", ""),
        },
        "llm_server_side": provider_status(),
        "store": store.stats(),
        "version": app.version,
    }


@app.get("/api/channels")
def channels() -> dict[str, Any]:
    """进入页需要的信息: 各通道的默认值 + 服务端是否已配好 Key。

    注意: 只回报「有没有配 Key」, 绝不把 Key 本身回传给前端。
    """
    srv = provider_status()
    return {
        "server": {
            "available": bool(srv.get("available")),
            "provider": srv.get("provider", ""),
            "base_url": srv.get("base_url", ""),
            "model": srv.get("model", ""),
            "reason": srv.get("reason", ""),
        },
        "ollama": {
            "base_url": settings.OLLAMA_BASE_URL,
            "model": settings.OLLAMA_MODEL,
            "local_hint": "http://127.0.0.1:11434",
        },
        "cloud": {
            "available": False,
            "reason": "平台云服务未开通（frontend/cloud-config.js 为空），代码路径已预留",
        },
    }


@app.post("/api/channels/test")
async def channels_test(opts: LLMOptions) -> dict[str, Any]:
    """连通性探测, 顺带把可用模型列表捞回来给前端做下拉。"""
    return await probe(opts)


@app.get("/api/platforms")
def platforms() -> dict[str, Any]:
    return {"items": P.pick_platform_list()}


@app.get("/api/styles")
def styles() -> dict[str, Any]:
    return {"items": P.pick_style_list()}


@app.get("/api/categories")
def categories() -> dict[str, Any]:
    return {"items": retriever.cat_names if retriever.ready else []}


# ---------------------------------------------------------------- 检索


@app.post("/api/retrieve")
def retrieve(req: RetrieveRequest) -> dict[str, Any]:
    if not retriever.ready:
        raise HTTPException(503, f"检索索引未就绪: {retriever.error}")
    hits = retriever.search(
        req.title, top_k=req.top_k, per_title=req.per_title, cat=req.cat or None, with_descs=True
    )
    return {"hits": hits, "n": len(hits)}


# ---------------------------------------------------------------- 组装提示词


def _prepare_payload(req: GenerateRequest) -> dict[str, Any]:
    refs: list[dict[str, Any]] = []
    hits: list[dict[str, Any]] = []
    if retriever.ready and req.use_references:
        hits = retriever.search(
            req.title, top_k=req.top_k, per_title=settings.DESCS_PER_TITLE, with_descs=True
        )
        refs = retriever.flat_references(hits, limit=settings.MAX_REFERENCES, query=req.title)

    length_override = None
    if req.min_len and req.max_len:
        length_override = (req.min_len, req.max_len)

    messages = P.build_messages(
        title=req.title,
        platform=req.platform,
        style=req.style,
        n=req.n,
        selling_points=req.selling_points,
        audience=req.audience,
        price_info=req.price_info,
        length_override=length_override,
        references=refs,
        use_references=req.use_references,
        avoid=req.avoid,
    )
    params = {
        "title": req.title,
        "platform": req.platform,
        "platform_name": (P.PLATFORMS.get(req.platform) or P.PLATFORMS["taobao"])["name"],
        "style": req.style,
        "style_name": (P.STYLES.get(req.style) or P.STYLES["auto"])["name"],
        "n": req.n,
        "selling_points": req.selling_points,
        "audience": req.audience,
        "price_info": req.price_info,
        "use_references": req.use_references,
        "len_range": list(length_override) if length_override else (P.PLATFORMS.get(req.platform) or P.PLATFORMS["taobao"])["length"],
    }
    return {"messages": messages, "references": refs, "hits": hits, "params": params}


def _grade(issues: list[dict[str, str]]) -> str:
    if not issues:
        return "ok"
    if any(i["level"] == "high" for i in issues):
        return "high"
    if any(i["level"] == "mid" for i in issues):
        return "mid"
    return "low"


def _finalize(raw: str, expect: int) -> list[dict[str, Any]]:
    items = P.parse_items(raw, expect=expect)
    out: list[dict[str, Any]] = []
    for it in items:
        text = it["text"].strip()
        issues = P.check_compliance(text)
        out.append(
            {
                "angle": (it.get("angle") or "文案").strip(),
                "text": text,
                "char_count": len([c for c in text if c.strip()]),
                "issues": issues,
                "grade": _grade(issues),
            }
        )
    return out


@app.post("/api/prepare", response_model=PrepareResponse)
def prepare(req: GenerateRequest) -> Any:
    return _prepare_payload(req)


@app.post("/api/finalize")
def finalize(req: FinalizeRequest) -> Any:
    items = _finalize(req.raw, req.expect)
    gid = ""
    if req.save and items:
        p = req.params or {}
        gid = store.save_generation(
            title=p.get("title", ""),
            platform=p.get("platform", ""),
            style=p.get("style", ""),
            n=len(items),
            params=p,
            refs=req.references,
            items=items,
            engine=req.engine,
            model=req.model,
            latency_ms=req.latency_ms,
        )
    return {
        "id": gid,
        "items": items,
        "references": req.references,
        "hits": req.hits,
        "engine": req.engine,
        "model": req.model,
        "latency_ms": req.latency_ms,
        "warnings": [] if items else ["未能从模型输出中解析出文案, 请重试或改用更强的模型"],
        "raw": req.raw if not items else "",
    }


# ---------------------------------------------------------------- 一步到位生成


@app.post("/api/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest) -> Any:
    payload = _prepare_payload(req)
    t0 = time.time()

    # 推理模型的 max_tokens 是「思考+正文」共享预算, 思考长度不稳定,
    # 因此解析不出足够条数时自动重试, 并逐次放宽预算。
    max_tokens = settings.LLM_MAX_TOKENS
    attempts = max(1, settings.LLM_RETRY + 1)
    best: tuple[dict[str, Any], list[dict[str, Any]]] | None = None
    last_err: LLMError | None = None
    warnings: list[str] = []

    for i in range(attempts):
        try:
            res = await chat(
                payload["messages"],
                temperature=req.temperature if req.temperature is not None else settings.LLM_TEMPERATURE,
                max_tokens=max_tokens,
                opts=req.llm,
            )
        except LLMError as e:
            last_err = e
            if i == attempts - 1:
                raise HTTPException(e.status, {"message": str(e), "detail": e.detail})
            max_tokens = int(max_tokens * 1.5)
            continue

        items = _finalize(res["content"], req.n)
        if best is None or len(items) > len(best[1]):
            best = (res, items)
        if len(items) >= req.n:
            break
        if res.get("truncated"):
            warnings.append(
                f"模型思考过长导致输出被截断(第 {i+1} 次尝试, max_tokens={max_tokens}), 已自动放宽预算重试"
            )
        max_tokens = int(max_tokens * 1.5)

    if best is None:
        raise HTTPException(
            last_err.status if last_err else 502,
            {"message": str(last_err) if last_err else "模型调用失败", "detail": ""},
        )

    res, items = best
    if 0 < len(items) < req.n:
        warnings.append(f"本次只解析出 {len(items)}/{req.n} 条, 可点「换一条」补充或重试")
    if not items:
        if res.get("truncated"):
            warnings.append("模型输出被截断且无法修复, 请在 .env 调大 LLM_MAX_TOKENS 后重试")
        elif res.get("finish_reason") == "content_filter":
            warnings.append("内容被模型侧风控拦截, 请换一种商品描述说法")
        else:
            warnings.append("未能从模型输出中解析出文案, 请重试")
    if res.get("truncated") and items:
        warnings.append("回复可能不完整(曾触发输出长度上限)")

    latency = int((time.time() - t0) * 1000)
    gid = ""
    if items:
        gid = store.save_generation(
            title=req.title,
            platform=req.platform,
            style=req.style,
            n=len(items),
            params=payload["params"],
            refs=payload["references"],
            items=items,
            engine="server",
            model=res.get("model", ""),
            latency_ms=latency,
        )
    return {
        "id": gid,
        "items": items,
        "references": payload["references"],
        "hits": payload["hits"],
        "engine": "server:" + resolve(req.llm)["provider"],
        "model": res.get("model", ""),
        "latency_ms": latency,
        "warnings": warnings,
        "raw": res["content"] if not items else "",
    }


# ---------------------------------------------------------------- 流式生成(驱动进度条)


def _sse(obj: dict[str, Any]) -> str:
    """SSE 帧。json.dumps 里不转义中文, 便于抓包排查。"""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


@app.post("/api/generate/stream")
async def generate_stream(req: GenerateRequest) -> Any:
    """与 /api/generate 等价, 但把生成过程拆成可观测的真实阶段:

        retrieve  检索同类真实文案      —— 完成时带真实命中数/参照数
        think     模型思考中            —— tick 带真实 reasoning 累计字数
        write     正在撰写文案          —— tick 带真实 content 累计字数
        parse     解析与合规检查
        done      携带与 /api/generate 完全一致的 result

    前端进度条完全由这些真实计数驱动, 不做假进度。
    截断自动重试、合规检查、落库逻辑与非流式版本保持一致。
    """
    if not req.title.strip():
        raise HTTPException(400, "商品标题不能为空")

    async def event_gen():
        t0 = time.time()

        def el() -> int:
            return int((time.time() - t0) * 1000)

        # ---- 阶段 1: 检索 ----
        try:
            yield _sse({"event": "stage", "stage": "retrieve", "label": "检索同类真实文案"})
            payload = _prepare_payload(req)
            yield _sse(
                {
                    "event": "stage_done",
                    "stage": "retrieve",
                    "hits": len(payload["hits"]),
                    "refs": len(payload["references"]),
                    "elapsed_ms": el(),
                }
            )
        except Exception as e:  # noqa: BLE001
            yield _sse({"event": "error", "message": f"检索失败: {e}", "elapsed_ms": el()})
            return

        # ---- 阶段 2: 流式生成(带截断自动重试) ----
        max_tokens = settings.LLM_MAX_TOKENS
        attempts = max(1, settings.LLM_RETRY + 1)
        best_res: dict[str, Any] | None = None
        best_items: list[dict[str, Any]] = []
        warnings: list[str] = []
        failed: LLMError | None = None

        for i in range(attempts):
            if i > 0:
                yield _sse(
                    {
                        "event": "retry",
                        "attempt": i + 1,
                        "max_tokens": max_tokens,
                        "reason": warnings[-1] if warnings else "",
                    }
                )

            phase = ""
            n_reason = 0
            n_content = 0
            res_done: dict[str, Any] | None = None
            last_tick = 0.0

            try:
                async for ev in stream_chat(
                    payload["messages"],
                    temperature=req.temperature if req.temperature is not None else settings.LLM_TEMPERATURE,
                    max_tokens=max_tokens,
                    opts=req.llm,
                ):
                    et = ev.get("type")
                    if et == "reasoning":
                        n_reason += len(ev["text"])
                        if phase != "think":
                            phase = "think"
                            yield _sse({"event": "stage", "stage": "think", "label": "模型思考中"})
                    elif et == "content":
                        n_content += len(ev["text"])
                        if phase != "write":
                            phase = "write"
                            yield _sse({"event": "stage", "stage": "write", "label": "正在撰写文案"})
                    elif et == "done":
                        res_done = ev

                    # 心跳: 限流到 200ms 一次, 避免把 SSE 打成逐字刷屏
                    now = time.time()
                    if now - last_tick >= 0.2:
                        last_tick = now
                        yield _sse(
                            {
                                "event": "tick",
                                "reasoning_chars": n_reason,
                                "content_chars": n_content,
                                "elapsed_ms": el(),
                            }
                        )
            except LLMError as e:
                failed = e
                if i == attempts - 1:
                    break
                max_tokens = int(max_tokens * 1.5)
                continue

            if res_done is None:
                failed = LLMError("模型流式返回中断, 未收到结束标记", 502)
                break

            items = _finalize(res_done["content"], req.n)
            if best_res is None or len(items) > len(best_items):
                best_res, best_items = res_done, items
            if len(items) >= req.n:
                break
            if res_done.get("truncated"):
                warnings.append(
                    f"模型思考过长导致输出被截断(第 {i+1} 次尝试, max_tokens={max_tokens}), 已自动放宽预算重试"
                )
            max_tokens = int(max_tokens * 1.5)

        if best_res is None:
            yield _sse(
                {
                    "event": "error",
                    "message": str(failed) if failed else "模型调用失败",
                    "detail": getattr(failed, "detail", ""),
                    "status": getattr(failed, "status", 502),
                    "elapsed_ms": el(),
                }
            )
            return

        # ---- 阶段 3: 解析 / 合规 / 落库 ----
        yield _sse({"event": "stage", "stage": "parse", "label": "解析与合规检查"})

        res, items = best_res, best_items
        if 0 < len(items) < req.n:
            warnings.append(f"本次只解析出 {len(items)}/{req.n} 条, 可点「换一条」补充或重试")
        if not items:
            if res.get("truncated"):
                warnings.append("模型输出被截断且无法修复, 请在 .env 调大 LLM_MAX_TOKENS 后重试")
            elif res.get("finish_reason") == "content_filter":
                warnings.append("内容被模型侧风控拦截, 请换一种商品描述说法")
            else:
                warnings.append("未能从模型输出中解析出文案, 请重试")
        if res.get("truncated") and items:
            warnings.append("回复可能不完整(曾触发输出长度上限)")

        latency = el()
        gid = ""
        if items:
            gid = store.save_generation(
                title=req.title,
                platform=req.platform,
                style=req.style,
                n=len(items),
                params=payload["params"],
                refs=payload["references"],
                items=items,
                engine="server",
                model=res.get("model", ""),
                latency_ms=latency,
            )

        yield _sse(
            {
                "event": "done",
                "result": {
                    "id": gid,
                    "items": items,
                    "references": payload["references"],
                    "hits": payload["hits"],
                    "engine": "server:" + resolve(req.llm)["provider"],
                    "model": res.get("model", ""),
                    "latency_ms": latency,
                    "warnings": warnings,
                    "raw": res["content"] if not items else "",
                },
                "elapsed_ms": latency,
            }
        )

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # 关掉 nginx 之类反向代理的缓冲, 否则事件会被攒着一起发
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------- 历史 / 收藏


@app.get("/api/history")
def history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    favorite_only: bool = False,
    keyword: str = "",
) -> dict[str, Any]:
    return store.list(limit=limit, offset=offset, favorite_only=favorite_only, keyword=keyword)


@app.get("/api/history/{gid}")
def history_detail(gid: str) -> dict[str, Any]:
    d = store.get(gid)
    if not d:
        raise HTTPException(404, "记录不存在")
    return d


@app.post("/api/history/{gid}/favorite")
def favorite(gid: str, fav: bool = Query(True)) -> dict[str, Any]:
    if not store.set_favorite(gid, fav):
        raise HTTPException(404, "记录不存在")
    return {"ok": True, "id": gid, "favorite": fav}


@app.delete("/api/history/{gid}")
def delete_history(gid: str) -> dict[str, Any]:
    if not store.delete(gid):
        raise HTTPException(404, "记录不存在")
    return {"ok": True}


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    st = retriever.stats()
    return {
        "corpus": {
            "titles": st["n_docs"],
            "terms": st["n_terms"],
            "categories": st["categories"],
            "meta": st.get("meta", {}),
        },
        "usage": store.stats(),
    }


# ---------------------------------------------------------------- 前端静态资源


def _frontend_dir() -> str:
    """前端静态资源目录: <项目根>/frontend。"""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")


FRONTEND_DIR = _frontend_dir()

if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    def index() -> Any:
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

    @app.get("/favicon.ico")
    def favicon() -> Any:
        fav = os.path.join(FRONTEND_DIR, "favicon.ico")
        if os.path.isfile(fav):
            return FileResponse(fav, media_type="image/x-icon")
        return JSONResponse({}, status_code=204)
