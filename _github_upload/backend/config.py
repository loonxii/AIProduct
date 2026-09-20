# -*- coding: utf-8 -*-
"""config.py — 运行配置(全部可通过环境变量覆盖，并自动读取项目根目录的 .env)"""
from __future__ import annotations

import os
from pathlib import Path

# 项目根目录 = backend/ 的上一级, 便于直接 `python -m backend.main`。
# 注意: 用 __file__ 定位, 与当前工作目录无关。
BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """极简 .env 加载器: 已存在的真实环境变量优先级更高, 不会被 .env 覆盖。

    不依赖 python-dotenv, 避免为一个几十行的功能引入额外依赖。
    支持 `KEY=VALUE`、`export KEY=VALUE`、`#` 注释、值两侧的引号。
    """
    path = BASE_DIR / ".env"
    if not path.exists():
        return
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if not key:
                    continue
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                # 真实环境变量优先
                if key not in os.environ:
                    os.environ[key] = val
    except Exception:
        # .env 解析失败不应阻止服务启动, 继续用环境变量/默认值
        pass


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v if v is not None and v != "" else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


class Settings:
    # ---- 数据路径 ----
    DATA_DIR = _env("DATA_DIR", str(BASE_DIR / "data"))
    CORPUS_DB = _env("CORPUS_DB", str(Path(DATA_DIR) / "corpus.db"))
    INDEX_DIR = _env("INDEX_DIR", str(Path(DATA_DIR) / "index"))
    APP_DB = _env("APP_DB", str(Path(DATA_DIR) / "app.db"))
    FINETUNE_DIR = _env("FINETUNE_DIR", str(Path(DATA_DIR) / "finetune"))

    # ---- 检索参数 ----
    TOP_K_TITLES = _env_int("TOP_K_TITLES", 6)
    DESCS_PER_TITLE = _env_int("DESCS_PER_TITLE", 4)
    MAX_REFERENCES = _env_int("MAX_REFERENCES", 10)

    # ---- LLM ----
    # 通道: auto | openai | ollama | none
    LLM_PROVIDER = _env("LLM_PROVIDER", "auto")
    LLM_BASE_URL = _env("LLM_BASE_URL", "")
    LLM_API_KEY = _env("LLM_API_KEY", "")
    LLM_MODEL = _env("LLM_MODEL", "")
    LLM_TIMEOUT = _env_int("LLM_TIMEOUT", 120)
    LLM_TEMPERATURE = float(_env("LLM_TEMPERATURE", "1.0"))
    # 注意: 对推理模型(思考型模型), max_tokens 是「思考 + 正文」共享的预算。
    # 实测思考部分可吃掉 900~1500 tokens, 预算太小会把正文截断在 JSON 中间。
    # 因此默认给到 4096。
    LLM_MAX_TOKENS = _env_int("LLM_MAX_TOKENS", 4096)
    # 解析失败时自动重试的次数(重试会放宽 max_tokens)
    LLM_RETRY = _env_int("LLM_RETRY", 1)

    OLLAMA_BASE_URL = _env("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    OLLAMA_MODEL = _env("OLLAMA_MODEL", "qwen2.5:7b")

    # ---- 服务 ----
    HOST = _env("HOST", "0.0.0.0")
    PORT = _env_int("PORT", 8000)
    CORS_ORIGINS = _env("CORS_ORIGINS", "*")
    ENABLE_DOCS = _env_bool("ENABLE_DOCS", True)


settings = Settings()
