# -*- coding: utf-8 -*-
"""schemas.py — 请求/响应模型"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LLMOptions(BaseModel):
    """前端在选择页选定的模型通道。

    为空(或 provider 为 server)时表示「沿用服务端 .env 里的配置」。
    provider: server(用 .env) | openai(自备 Key) | ollama(本机) | cloud(平台)
    api_key 只由浏览器持有, 随请求临时使用, 不落盘。
    """

    provider: str = Field("server", description="server | openai | ollama | cloud")
    base_url: str = Field("", max_length=300)
    api_key: str = Field("", max_length=300)
    model: str = Field("", max_length=120)


class GenerateRequest(BaseModel):
    title: str = Field(..., min_length=2, max_length=200, description="商品标题")
    platform: str = Field("taobao", description="目标平台 id")
    style: str = Field("auto", description="文案风格 id")
    n: int = Field(3, ge=1, le=6, description="生成条数")
    selling_points: list[str] = Field(default_factory=list, description="卖点关键词")
    audience: str = Field("", max_length=60, description="目标人群")
    price_info: str = Field("", max_length=80, description="价格/活动信息")
    min_len: int | None = Field(None, ge=10, le=400)
    max_len: int | None = Field(None, ge=10, le=500)
    use_references: bool = True
    top_k: int = Field(6, ge=1, le=15)
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    avoid: list[str] = Field(default_factory=list, description="需要避免雷同的已有文案")
    llm: LLMOptions | None = Field(None, description="本次生成使用的模型通道, 不传则用服务端配置")


class ReferenceItem(BaseModel):
    desc: str
    src_title: str = ""
    style: str = ""


class TitleHit(BaseModel):
    title_id: int
    title: str
    cat: str = ""
    score: float = 0
    similarity: float = 0
    n_desc: int = 0
    descs: list[dict[str, Any]] = Field(default_factory=list)


class PrepareResponse(BaseModel):
    messages: list[dict[str, str]]
    references: list[ReferenceItem]
    hits: list[TitleHit]
    params: dict[str, Any]


class FinalizeRequest(BaseModel):
    raw: str = Field("", description="模型原始输出")
    expect: int = Field(3, ge=1, le=6)
    params: dict[str, Any] = Field(default_factory=dict)
    references: list[dict[str, Any]] = Field(default_factory=list)
    hits: list[dict[str, Any]] = Field(default_factory=list)
    engine: str = "cloud"
    model: str = ""
    latency_ms: int = 0
    save: bool = True


class CopyItem(BaseModel):
    id: str = ""
    angle: str = "文案"
    text: str
    char_count: int = 0
    issues: list[dict[str, str]] = Field(default_factory=list)
    grade: str = "ok"


class GenerateResponse(BaseModel):
    id: str = ""
    items: list[CopyItem] = Field(default_factory=list)
    references: list[ReferenceItem] = Field(default_factory=list)
    hits: list[TitleHit] = Field(default_factory=list)
    engine: str = ""
    model: str = ""
    latency_ms: int = 0
    warnings: list[str] = Field(default_factory=list)
    raw: str = ""


class RetrieveRequest(BaseModel):
    title: str = Field(..., min_length=2, max_length=200)
    top_k: int = Field(6, ge=1, le=15)
    per_title: int = Field(4, ge=1, le=8)
    cat: str = ""
