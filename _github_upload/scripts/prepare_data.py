# -*- coding: utf-8 -*-
"""一键跑完「清洗 → 建索引」数据准备流程"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def run(desc, args):
    print(f"\n{'='*64}\n▶ {desc}\n{'='*64}", flush=True)
    t0 = time.time()
    r = subprocess.run([PY] + args, cwd=ROOT)
    print(f"  完成, 耗时 {time.time()-t0:.1f}s, exit={r.returncode}", flush=True)
    if r.returncode != 0:
        print(f"  !! {desc} 失败", flush=True)
        sys.exit(r.returncode)


def main():
    extra = sys.argv[1:]
    run("步骤 1/2 清洗原始数据集", [
        os.path.join("scripts", "clean_data.py"),
        "--out-dir", os.path.join(ROOT, "data"),
    ] + extra)
    run("步骤 2/2 构建检索索引", [
        os.path.join("scripts", "build_index.py"),
        "--db", os.path.join(ROOT, "data", "corpus.db"),
        "--out", os.path.join(ROOT, "data", "index"),
    ])
    print("\n全部完成。启动服务:  docker compose up -d --build")
    print("或本地运行:        uvicorn backend.main:app --port 8000")


if __name__ == "__main__":
    main()
