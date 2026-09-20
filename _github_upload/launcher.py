# -*- coding: utf-8 -*-
"""
launcher.py — 商品描述文案助手 · 一键启动入口

它做这几件事:
  1. 定位「项目根目录」
  2. 把数据目录指到 <项目根>/data, 并读取同目录的 .env
  3. 自检数据是否就绪 (语料库 / 检索索引), 缺失时给出可操作的提示而不是抛栈
  4. 端口被占用时自动顺延找一个空闲端口
  5. 启动 uvicorn, 稍后自动打开浏览器
  6. 出错时打印中文提示并暂停, 让用户看清原因
  7. 缺运行依赖时自动改用项目内 .venv 的解释器重启 (见「解释器自愈」)

只启动业务服务, 不需要训练相关依赖与语料 (torch / transformers / data/finetune 一律不碰)。

用法:
    python launcher.py                      # 默认 8000 端口, 自动开浏览器
    python launcher.py --port 8010          # 指定端口
    python launcher.py --no-browser         # 不开浏览器
    python launcher.py --host 127.0.0.1     # 只监听本机

用哪个解释器都行: 若当前解释器缺 fastapi/uvicorn, 而项目内存在 .venv,
本脚本会自动切到 .venv 重新执行, 无需手动选解释器。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_NAME = "商品描述文案助手"
APP_VERSION = "1.0.0"

# 需要的检索资源(相对 data/), 用于启动自检
REQUIRED_FILES = [
    ("语料库", "corpus.db"),
    ("检索索引", "index/index.npz"),
    ("检索词表", "index/vocab.txt"),
]


# --------------------------------------------------------------------- 路径

def program_root() -> Path:
    """项目根目录: 本文件所在目录。"""
    return Path(__file__).resolve().parent


# ------------------------------------------------------------------- 终端输出

def _enable_utf8_console() -> None:
    """Windows 控制台默认 GBK, 直接 print 中文可能乱码或报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def say(msg: str = "") -> None:
    print(msg, flush=True)


def banner() -> None:
    say("=" * 58)
    say(f"  {APP_NAME}  v{APP_VERSION}")
    say("=" * 58)
    say()


def pause(msg: str = "按回车键退出...") -> None:
    """让窗口不要一闪而过。无控制台时静默跳过。"""
    try:
        input(msg)
    except Exception:
        pass


def fatal(msg: str) -> None:
    """致命错误: 打印中文提示 + 暂停 + 非零退出。"""
    say()
    say("!" * 58)
    say(msg)
    say("!" * 58)
    pause()
    sys.exit(1)


# ---------------------------------------------------------------- 解释器自愈

#: 启动业务服务必需的第三方包 (与 requirements.txt 保持一致)
RUNTIME_DEPS = ("uvicorn", "fastapi")

_REEXEC_FLAG = "_LAUNCHER_VENV_REEXEC"


def missing_runtime_deps() -> list[str]:
    """返回当前解释器缺失的运行依赖名单, 空列表表示齐备。"""
    import importlib.util

    return [m for m in RUNTIME_DEPS if importlib.util.find_spec(m) is None]


def local_venv_python(root: Path) -> Path | None:
    """项目内虚拟环境的解释器: <项目根>/.venv/Scripts/python.exe (兼容 POSIX 布局)。"""
    for rel in (("Scripts", "python.exe"), ("bin", "python")):
        cand = root.joinpath(".venv", *rel)
        if cand.exists():
            return cand
    return None


def reexec_with_venv(root: Path) -> bool:
    """依赖缺失时, 自动改用项目内 .venv 的解释器重新执行本脚本。

    返回 False 表示这次没接管 (调用方继续往下走, 由后续的 fatal 提示收场);
    接管成功则会直接 sys.exit, 不会返回。
    """
    if os.environ.get(_REEXEC_FLAG) == "1":
        return False  # 已经切过一次, 防重入死循环

    venv_py = local_venv_python(root)
    if venv_py is None:
        return False
    try:
        if Path(sys.executable).resolve() == venv_py.resolve():
            return False  # 已经跑在 .venv 里了
    except OSError:
        pass

    say("当前解释器缺少运行依赖, 已自动改用项目内虚拟环境:")
    say(f"  {venv_py}")
    say()

    import subprocess

    env = dict(os.environ)
    env[_REEXEC_FLAG] = "1"
    try:
        proc = subprocess.run(
            [str(venv_py), str(Path(__file__).resolve()), *sys.argv[1:]],
            env=env,
        )
    except OSError as e:
        say(f"自动切换解释器失败: {type(e).__name__}: {e}")
        say()
        return False
    sys.exit(proc.returncode)


# --------------------------------------------------------------------- 数据

def check_data(data_dir: Path) -> list[str]:
    """返回缺失项的中文描述列表, 空列表表示数据齐全。"""
    missing: list[str] = []
    for label, rel in REQUIRED_FILES:
        if not (data_dir / rel).exists():
            missing.append(f"{label}  ({rel})")
    return missing


def data_help(data_dir: Path, missing: list[str]) -> str:
    lines = [
        "数据目录不完整, 无法启动。",
        "",
        f"数据目录: {data_dir}",
        "缺少以下文件:",
    ]
    lines += [f"  · {m}" for m in missing]
    lines += [
        "",
        "解决办法(任选其一):",
        "  1. 把完整的 data/ 目录放到本项目根目录下 (与 launcher.py 同级);",
        "  2. 按 README「快速开始 · 准备数据」重新生成 data/;",
        "  3. 若数据放在别处, 设置环境变量 DATA_DIR 指向它后再启动。",
        "",
        "关于体积: 运行所需的必需数据约 451 MB;",
        "         data/finetune/ (微调语料, 约 633 MB) 只有自己训练模型时才需要, 可以不拷。",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------- 端口

def is_port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    """探测端口是否已有服务在监听。

    用「尝试连接」而不是「尝试绑定」:
      Windows 上给探测 socket 设 SO_REUSEADDR 的话, 即使端口已被 0.0.0.0 占用,
      bind(127.0.0.1) 也能成功 -> 探测出假阴性, 真正启动时才报 10048。
      connect_ex 只有在真的有人监听时才返回 0, 没有这个坑。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def pick_port(preferred: int, host: str = "127.0.0.1", tries: int = 30) -> int:
    for i in range(tries):
        port = preferred + i
        if not is_port_in_use(port, host):
            return port
    fatal(
        f"从 {preferred} 开始的 {tries} 个端口都被占用了, 无法启动服务。\n"
        "请关闭占用端口的程序, 或用 --port 指定其它端口后重试。"
    )
    return preferred  # 不会走到


# --------------------------------------------------------------------- 启动

def open_browser_later(url: str, delay: float = 1.2) -> None:
    def _go() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_go, daemon=True).start()


def run(host: str, port: int, open_browser: bool) -> None:
    url = f"http://127.0.0.1:{port}/"
    say("正在启动服务...")
    say(f"  监听地址 : http://{host}:{port}")
    say(f"  访问地址 : {url}")
    say()
    say("首次加载检索索引需要几秒, 请稍候。")
    say("关闭本窗口即可停止服务 (或按 Ctrl+C)。")
    say()

    if open_browser:
        open_browser_later(url)

    try:
        import uvicorn

        from backend.main import app
    except Exception as e:  # 依赖缺失 / 代码损坏
        venv_py = local_venv_python(program_root())
        fatal(
            "加载服务程序失败: 运行依赖不完整。\n\n"
            f"错误信息: {type(e).__name__}: {e}\n\n"
            f"当前解释器: {sys.executable}\n\n"
            "解决办法(任选其一):\n"
            f"  1. 用项目内虚拟环境启动:\n     {venv_py or '<项目根>/.venv/Scripts/python.exe'} launcher.py\n"
            "  2. 给当前解释器装齐依赖: pip install -r requirements.txt\n"
            "  3. 确认已在项目根目录, 且 backend/ 、frontend/ 目录完整。"
        )
        return

    try:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="info",
            access_log=False,
        )
    except KeyboardInterrupt:
        say()
        say("服务已停止。")
    except OSError as e:
        fatal(
            f"启动服务失败: 端口 {port} 可能被占用。\n\n"
            f"错误信息: {e}\n\n"
            "请稍后重试, 或用 --port 指定其它端口。"
        )
    except Exception as e:
        fatal(
            "服务运行过程中出现异常。\n\n"
            f"错误信息: {type(e).__name__}: {e}\n\n"
            "请把本窗口的完整输出反馈给技术支持。"
        )


# --------------------------------------------------------------------- 主流程

def main() -> None:
    _enable_utf8_console()

    ap = argparse.ArgumentParser(description=APP_NAME, add_help=True)
    ap.add_argument("--host", default=None, help="监听地址 (默认 0.0.0.0)")
    ap.add_argument("--port", type=int, default=None, help="监听端口 (默认 8000, 占用则顺延)")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = ap.parse_args()

    root = program_root()

    # 解释器自愈: 缺运行依赖且项目内有 .venv → 换解释器重启, 不让用户卡在启动失败
    if missing_runtime_deps():
        reexec_with_venv(root)

    # 数据目录: 默认 <程序根>/data; 已有环境变量则尊重用户的设置
    os.environ.setdefault("DATA_DIR", str(root / "data"))
    data_dir = Path(os.environ["DATA_DIR"]).resolve()

    banner()
    say(f"程序目录 : {root}")
    say(f"数据目录 : {data_dir}")
    say(f"配置文件 : {root / '.env'}" + ("" if (root / ".env").exists() else "  (未找到, 将使用内置默认值)"))
    say()

    missing = check_data(data_dir)
    if missing:
        fatal(data_help(data_dir, missing))

    # 端口 / 地址: 命令行 > .env(由 config 读取)
    env_host = os.environ.get("HOST", "").strip()
    env_port = os.environ.get("PORT", "").strip()
    host = args.host or env_host or "0.0.0.0"
    try:
        preferred = args.port or int(env_port or 8000)
    except ValueError:
        preferred = 8000

    port = pick_port(preferred, "127.0.0.1")
    if port != preferred:
        say(f"提示: 端口 {preferred} 已被占用, 已自动改用 {port}。")
        say()

    run(host, port, open_browser=not args.no_browser)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # 兜底, 保证任何情况下都有中文提示
        import traceback

        traceback.print_exc()
        fatal(f"启动时发生未预期的错误:\n\n{type(exc).__name__}: {exc}")
