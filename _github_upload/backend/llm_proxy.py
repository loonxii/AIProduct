# -*- coding: utf-8 -*-
"""
llm_proxy.py — 服务端大模型调用通道

本模块只负责「自托管」场景(我自己部署、我自己的 Key / 我自己的 Ollama)。
如果应用发布在托管平台上, 前端会优先走平台免密钥通道, 不会经过这里。

支持:
  - openai  : 任意 OpenAI 兼容接口 (通义 / Kimi / 智谱 / DeepSeek / OpenAI / vLLM ...)
  - ollama  : 本机 Ollama
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .config import settings


class LLMError(RuntimeError):
    def __init__(self, msg: str, status: int = 502, detail: str = ""):
        super().__init__(msg)
        self.status = status
        self.detail = detail


def _clean(v: Any) -> str:
    return str(v).strip() if v else ""


def provider_status() -> dict[str, Any]:
    """告诉前端: 服务端是否具备可用的生成通道"""
    prov = settings.LLM_PROVIDER
    has_key = bool(settings.LLM_API_KEY)
    if prov == "none":
        return {"available": False, "provider": "none", "reason": "服务端未启用生成通道(LLM_PROVIDER=none)"}
    if prov in ("openai", "auto") and has_key:
        return {
            "available": True,
            "provider": "openai",
            "base_url": settings.LLM_BASE_URL,
            "model": settings.LLM_MODEL,
        }
    if prov == "ollama":
        return {"available": True, "provider": "ollama", "base_url": settings.OLLAMA_BASE_URL, "model": settings.OLLAMA_MODEL}
    if prov == "auto":
        # 没配 Key 时退到本地 Ollama
        return {
            "available": True,
            "provider": "ollama",
            "base_url": settings.OLLAMA_BASE_URL,
            "model": settings.OLLAMA_MODEL,
            "note": "未配置 LLM_API_KEY, 自动使用本机 Ollama",
        }
    return {"available": False, "provider": prov, "reason": "缺少 LLM_API_KEY 配置"}


def resolve(opts: Any = None) -> dict[str, Any]:
    """把「前端在选择页选的通道」和「服务端 .env」合并成一份生效配置。

    opts 可以是 None、LLMOptions 实例或 dict。
    provider=server/空 -> 完全沿用 .env; openai/ollama 才覆盖。
    """
    o: dict[str, Any] = {}
    if opts is not None:
        if isinstance(opts, dict):
            o = opts
        else:
            o = {k: getattr(opts, k, "") for k in ("provider", "base_url", "api_key", "model")}

    prov = _clean(o.get("provider"))
    if not prov or prov == "server":
        return provider_status()

    if prov == "ollama":
        return {
            "available": True,
            "provider": "ollama",
            "base_url": _clean(o.get("base_url")) or settings.OLLAMA_BASE_URL,
            "model": _clean(o.get("model")) or settings.OLLAMA_MODEL,
        }

    if prov == "openai":
        key = _clean(o.get("api_key"))
        if not key:
            return {"available": False, "provider": "openai", "reason": "未填写 API Key"}
        return {
            "available": True,
            "provider": "openai",
            "base_url": _clean(o.get("base_url")) or settings.LLM_BASE_URL,
            "api_key": key,
            "model": _clean(o.get("model")) or settings.LLM_MODEL,
        }

    return {"available": False, "provider": prov, "reason": f"不支持的通道: {prov}"}


def _key_of(cfg: dict[str, Any]) -> str:
    """前端传来的 Key 优先; 走 .env 通道时 provider_status() 不带 Key, 回退到服务端配置。"""
    return cfg.get("api_key") or settings.LLM_API_KEY


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal"}


def _is_local_host(url: str) -> bool:
    try:
        host = httpx.URL(url).host or ""
    except Exception:  # noqa: BLE001
        return False
    host = host.strip("[]").lower()
    if host in _LOCAL_HOSTS:
        return True
    if host.startswith("127.") or host.startswith("192.168.") or host.startswith("10."):
        return True
    if host.startswith("172."):
        try:
            return 16 <= int(host.split(".")[1]) <= 31
        except (ValueError, IndexError):
            return False
    if host.endswith(".local") or host.endswith(".internal"):
        return True
    return False


def _mk_client(timeout: float, url: str) -> httpx.AsyncClient:
    """构造 httpx 客户端。

    关键: 本机/内网地址一律 trust_env=False 绕过系统代理。
    实测踩过的坑——宿主机设了 HTTP_PROXY 时, 连 http://127.0.0.1:11434 这种本机
    Ollama 请求也会被代理拦走, 返回一个假的 502「upstream connect failed」,
    看起来像 Ollama 挂了, 其实是代理在捣乱。
    """
    return httpx.AsyncClient(timeout=timeout, trust_env=not _is_local_host(url))


async def _call_openai(
    messages: list[dict[str, str]], temperature: float, max_tokens: int, cfg: dict[str, Any]
) -> dict[str, Any]:
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {_key_of(cfg)}",
        "Content-Type": "application/json",
    }
    try:
        async with _mk_client(settings.LLM_TIMEOUT, url) as cli:
            r = await cli.post(url, json=payload, headers=headers)
    except httpx.TimeoutException:
        raise LLMError("模型接口超时, 请稍后重试或换用更小的模型", 504)
    except Exception as e:
        raise LLMError(f"无法连接模型接口: {e}", 502)

    if r.status_code == 401:
        raise LLMError("模型接口鉴权失败(401), 请检查 LLM_API_KEY", 401)
    if r.status_code == 429:
        raise LLMError("模型接口限流(429), 请稍后重试", 429)
    if r.status_code >= 400:
        raise LLMError(f"模型接口返回错误 {r.status_code}", 502, r.text[:500])

    data = r.json()
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
        finish_reason = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError):
        raise LLMError("模型返回结构异常", 502, json.dumps(data, ensure_ascii=False)[:500])
    return {
        "content": content,
        "model": data.get("model", cfg["model"]),
        "usage": data.get("usage", {}),
        "finish_reason": finish_reason,
        "truncated": finish_reason == "length",
    }


async def _call_ollama(
    messages: list[dict[str, str]], temperature: float, max_tokens: int, cfg: dict[str, Any]
) -> dict[str, Any]:
    url = cfg["base_url"].rstrip("/") + "/api/chat"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    try:
        async with _mk_client(settings.LLM_TIMEOUT, url) as cli:
            r = await cli.post(url, json=payload)
    except Exception as e:
        raise LLMError(
            f"无法连接本地 Ollama ({cfg['base_url']}): {e}。"
            "请确认 Ollama 已启动, 且容器内可访问宿主机(Docker 下为 host.docker.internal)",
            502,
        )
    if r.status_code >= 400:
        raise LLMError(f"Ollama 返回错误 {r.status_code}", 502, r.text[:300])
    data = r.json()
    msg = data.get("message") or {}
    content = msg.get("content", "")
    if not content:
        raise LLMError("Ollama 返回内容为空", 502, json.dumps(data, ensure_ascii=False)[:300])
    return {
        "content": content,
        "model": cfg["model"],
        "usage": {},
        "finish_reason": data.get("done_reason") or ("stop" if data.get("done") else None),
        "truncated": False,
    }


async def chat(messages: list[dict[str, str]], temperature: float | None = None, max_tokens: int | None = None) -> dict[str, Any]:
    t = settings.LLM_TEMPERATURE if temperature is None else temperature
    mt = settings.LLM_MAX_TOKENS if max_tokens is None else max_tokens
    st = provider_status()
    if not st["available"]:
        raise LLMError(st.get("reason", "服务端生成通道不可用"), 503)
    if st["provider"] == "openai":
        return await _call_openai(messages, t, mt)
    if st["provider"] == "ollama":
        return await _call_ollama(messages, t, mt)
    raise LLMError(f"不支持的 LLM_PROVIDER: {st['provider']}", 500)


# ---------------------------------------------------------------- 流式调用
#
# 为什么要流式: 推理模型(思考型模型)的耗时大头在「思考」阶段。
# 实测一次 4.6s 的生成里, 思考占了 3.1s(0.65s~3.78s, 1210 字), 正文只占最后 0.8s。
# 非流式调用下前端只能看到「转圈」, 无法给出任何真实进度。
# 流式之后可以拿到两路真实计数, 直接驱动进度条:
#   reasoning_content 的累计字数 → 思考阶段进度
#   content 的累计字数           → 撰写阶段进度


async def _stream_openai(
    messages: list[dict[str, str]], temperature: float, max_tokens: int, cfg: dict[str, Any]
):
    """OpenAI 兼容接口的流式调用。逐块 yield:
       {"type":"reasoning","text":...} | {"type":"content","text":...}
       {"type":"done","content":str,"model":str,"finish_reason":str|None,
        "truncated":bool,"usage":dict}
    """
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    headers = {
        "Authorization": f"Bearer {_key_of(cfg)}",
        "Content-Type": "application/json",
    }

    parts: list[str] = []
    model = cfg["model"]
    finish_reason: str | None = None
    usage: dict[str, Any] = {}

    try:
        async with _mk_client(settings.LLM_TIMEOUT, url) as cli:
            async with cli.stream("POST", url, json=payload, headers=headers) as r:
                if r.status_code >= 400:
                    body = (await r.aread()).decode("utf-8", "replace")
                    if r.status_code == 401:
                        raise LLMError("模型接口鉴权失败(401), 请检查 LLM_API_KEY", 401)
                    if r.status_code == 429:
                        raise LLMError("模型接口限流(429), 请稍后重试", 429)
                    raise LLMError(f"模型接口返回错误 {r.status_code}", 502, body[:500])

                async for raw in r.aiter_lines():
                    line = raw.strip()
                    if not line:
                        continue
                    # SSE 注释行(部分厂商用它做 keep-alive)
                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if not line or line == "[DONE]":
                        continue
                    try:
                        obj = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if obj.get("model"):
                        model = obj["model"]
                    if obj.get("usage"):
                        usage = obj["usage"]
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    ch = choices[0] or {}
                    if ch.get("finish_reason"):
                        finish_reason = ch["finish_reason"]
                    delta = ch.get("delta") or {}
                    rtext = delta.get("reasoning_content")
                    if rtext:
                        yield {"type": "reasoning", "text": rtext}
                    ctext = delta.get("content")
                    if ctext:
                        parts.append(ctext)
                        yield {"type": "content", "text": ctext}
    except LLMError:
        raise
    except httpx.TimeoutException:
        raise LLMError("模型接口超时, 请稍后重试或换用更小的模型", 504)
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"无法连接模型接口: {e}", 502)

    content = "".join(parts)
    if not content.strip():
        raise LLMError("模型返回内容为空", 502)
    yield {
        "type": "done",
        "content": content,
        "model": model,
        "finish_reason": finish_reason,
        "truncated": finish_reason == "length",
        "usage": usage,
    }


async def _stream_ollama(
    messages: list[dict[str, str]], temperature: float, max_tokens: int, cfg: dict[str, Any]
):
    """本机 Ollama 的流式调用(NDJSON, 每行一个 JSON)。"""
    url = cfg["base_url"].rstrip("/") + "/api/chat"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "stream": True,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }

    parts: list[str] = []
    finish_reason: str | None = None

    try:
        async with _mk_client(settings.LLM_TIMEOUT, url) as cli:
            async with cli.stream("POST", url, json=payload) as r:
                if r.status_code >= 400:
                    body = (await r.aread()).decode("utf-8", "replace")
                    raise LLMError(f"Ollama 返回错误 {r.status_code}", 502, body[:300])
                async for raw in r.aiter_lines():
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    msg = obj.get("message") or {}
                    ctext = msg.get("content")
                    if ctext:
                        parts.append(ctext)
                        yield {"type": "content", "text": ctext}
                    if obj.get("done"):
                        finish_reason = obj.get("done_reason") or "stop"
    except LLMError:
        raise
    except httpx.TimeoutException:
        raise LLMError("本地 Ollama 响应超时", 504)
    except Exception as e:  # noqa: BLE001
        raise LLMError(
            f"无法连接本地 Ollama ({cfg['base_url']}): {e}。"
            "请确认 Ollama 已启动, 且容器内可访问宿主机(Docker 下为 host.docker.internal)",
            502,
        )

    content = "".join(parts)
    if not content.strip():
        raise LLMError("Ollama 返回内容为空", 502)
    yield {
        "type": "done",
        "content": content,
        "model": cfg["model"],
        "finish_reason": finish_reason,
        "truncated": False,
        "usage": {},
    }


async def chat(
    messages: list[dict[str, str]],
    temperature: float | None = None,
    max_tokens: int | None = None,
    opts: Any = None,
) -> dict[str, Any]:
    t = settings.LLM_TEMPERATURE if temperature is None else temperature
    mt = settings.LLM_MAX_TOKENS if max_tokens is None else max_tokens
    cfg = resolve(opts)
    if not cfg["available"]:
        raise LLMError(cfg.get("reason", "生成通道不可用"), 503)
    if cfg["provider"] == "openai":
        return await _call_openai(messages, t, mt, cfg)
    if cfg["provider"] == "ollama":
        return await _call_ollama(messages, t, mt, cfg)
    raise LLMError(f"不支持的 LLM_PROVIDER: {cfg['provider']}", 500)


async def stream_chat(
    messages: list[dict[str, str]],
    temperature: float | None = None,
    max_tokens: int | None = None,
    opts: Any = None,
):
    """统一的流式入口, 事件格式与 _stream_openai 一致。

    注意: 这里是异步生成器, 通道可用性校验要等到第一次迭代才执行,
    调用方需要把 `async for` 包在 try/except LLMError 里。
    """
    t = settings.LLM_TEMPERATURE if temperature is None else temperature
    mt = settings.LLM_MAX_TOKENS if max_tokens is None else max_tokens
    cfg = resolve(opts)
    if not cfg["available"]:
        raise LLMError(cfg.get("reason", "生成通道不可用"), 503)
    if cfg["provider"] == "openai":
        gen = _stream_openai(messages, t, mt, cfg)
    elif cfg["provider"] == "ollama":
        gen = _stream_ollama(messages, t, mt, cfg)
    else:
        raise LLMError(f"不支持的 LLM_PROVIDER: {cfg['provider']}", 500)
    async for ev in gen:
        yield ev


# ---------------------------------------------------------------- 通道探测
#
# 进入页要用: 用户选完通道后点「测试并进入」, 这里先确认真的连得上,
# 顺便把可用模型列表捞回来给他做下拉选择, 避免手打模型名打错。


async def probe(opts: Any = None) -> dict[str, Any]:
    cfg = resolve(opts)
    prov = cfg.get("provider", "")
    if not cfg.get("available"):
        return {
            "ok": False,
            "provider": prov,
            "message": cfg.get("reason", "通道不可用"),
            "base_url": cfg.get("base_url", ""),
        }

    t0 = time.time()
    models: list[str] = []

    if prov == "openai":
        url = cfg["base_url"].rstrip("/") + "/models"
    elif prov == "ollama":
        url = cfg["base_url"].rstrip("/") + "/api/tags"
    else:
        return {"ok": False, "provider": prov, "message": f"暂不支持的通道: {prov}"}

    try:
        async with _mk_client(20.0, url) as cli:
            if prov == "openai":
                r = await cli.get(url, headers={"Authorization": f"Bearer {_key_of(cfg)}"})
                if r.status_code in (401, 403):
                    return {"ok": False, "provider": prov, "base_url": cfg["base_url"],
                            "message": "鉴权失败，请检查 API Key"}
                if r.status_code >= 400:
                    # 有些兼容服务没实现 /models, 退化成一次极小的真实调用
                    ping = await _ping_openai(cfg)
                    if ping is None:
                        return {"ok": False, "provider": prov, "base_url": cfg["base_url"],
                                "message": f"接口返回 {r.status_code}", "detail": r.text[:200]}
                    return {"ok": True, "provider": prov, "base_url": cfg["base_url"],
                            "model": cfg["model"], "models": [],
                            "latency_ms": int((time.time() - t0) * 1000),
                            "message": "已连通（该服务未提供模型列表接口，请手动确认模型名）"}
                data = r.json()
                models = [m.get("id") for m in (data.get("data") or []) if m.get("id")]

            else:  # ollama
                r = await cli.get(url)
                if r.status_code >= 400:
                    return {"ok": False, "provider": prov, "base_url": cfg["base_url"],
                            "message": f"Ollama 返回 {r.status_code}", "detail": r.text[:200]}
                data = r.json()
                models = [m.get("name") for m in (data.get("models") or []) if m.get("name")]

    except httpx.TimeoutException:
        return {"ok": False, "provider": prov, "base_url": cfg.get("base_url", ""),
                "message": f"连接超时，请检查地址是否可达：{cfg.get('base_url','')}"}
    except httpx.ConnectError as e:
        return {"ok": False, "provider": prov, "base_url": cfg.get("base_url", ""),
                "message": f"连不上 {cfg.get('base_url','')}，请确认服务已启动且地址端口正确（{e}）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "provider": prov, "base_url": cfg.get("base_url", ""),
                "message": f"连接失败：{e}"}

    latency = int((time.time() - t0) * 1000)
    chosen = cfg["model"]
    if models and chosen and chosen not in models:
        note = f"注意：当前填写的模型「{chosen}」不在可用列表中，仍按填写的名称调用"
    else:
        note = "连接成功"
    return {
        "ok": True,
        "provider": prov,
        "base_url": cfg["base_url"],
        "model": chosen,
        "models": models,
        "latency_ms": latency,
        "message": note,
    }


async def _ping_openai(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """退化的连通性校验: 一次极小的真实调用。成功返回结果, 失败返回 None。"""
    try:
        return await _call_openai(
            [{"role": "user", "content": "ping"}], 0.0, 16, cfg
        )
    except Exception:  # noqa: BLE001
        return None
