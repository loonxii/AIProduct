# -*- coding: utf-8 -*-
"""
finetune_lora.py — 在清洗好的数据集上对 Qwen2.5 做 LoRA 微调

依赖(与线上服务分离, 只在有 GPU 的机器上装):
    pip install torch transformers peft trl datasets accelerate bitsandbytes

用法:
    # 只统计数据集, 不训练(无 GPU 也能跑)
    python scripts/finetune_lora.py --dry-run

    # 1.5B 全参 LoRA(消费级显卡 8G 显存可跑)
    python scripts/finetune_lora.py --model Qwen/Qwen2.5-1.5B-Instruct

    # 7B + 4bit 量化
    python scripts/finetune_lora.py --model Qwen/Qwen2.5-7B-Instruct --load-4bit

    # 训练完合并权重成独立模型
    python scripts/finetune_lora.py --merge ./outputs/qwen-lora

数据集格式(由 clean_data.py 生成):
    {"instruction": "...", "input": "商品标题", "output": "描述文案", "meta": {...}}
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TRAIN = os.path.join(ROOT, "data", "finetune", "sft_train.jsonl")
DEFAULT_VAL = os.path.join(ROOT, "data", "finetune", "sft_val.jsonl")

SYSTEM = (
    "你是一名资深电商文案策划, 擅长为淘宝、京东、小红书等平台撰写商品卖点文案。"
    "你的文案具体、有画面感, 不空泛, 严格遵守广告法, 不编造商品参数。"
)


def read_jsonl(path: str, limit: int = 0) -> list[dict]:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if limit and len(out) >= limit:
                break
    return out


def to_chat(sample: dict) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": sample.get("instruction", "") + "\n" + sample.get("input", "")},
        {"role": "assistant", "content": sample.get("output", "")},
    ]


def stats(samples: list[dict]) -> dict:
    from collections import Counter

    lens = [len(s.get("output", "")) for s in samples]
    cats = Counter((s.get("meta") or {}).get("cat", "?") for s in samples)
    styles = Counter((s.get("meta") or {}).get("style", "?") for s in samples)
    return {
        "samples": len(samples),
        "out_len_min": min(lens) if lens else 0,
        "out_len_max": max(lens) if lens else 0,
        "out_len_avg": round(sum(lens) / len(lens), 1) if lens else 0,
        "top_cats": cats.most_common(10),
        "styles": styles.most_common(),
    }


def dry_run(args) -> int:
    tr = read_jsonl(args.train, args.max_samples)
    va = read_jsonl(args.val, 2000)
    print("=" * 60)
    print("数据集体检")
    print("=" * 60)
    print(f"train: {args.train}")
    print(json.dumps(stats(tr), ensure_ascii=False, indent=2))
    print(f"\nval:   {args.val}")
    print(json.dumps(stats(va), ensure_ascii=False, indent=2))
    print("\n样例(前 2 条格式化后):")
    for s in tr[:2]:
        msgs = to_chat(s)
        for m in msgs:
            print(f"  <{m['role']}> {m['content'][:110]}")
        print("  " + "-" * 50)
    print("\n提示: 去掉 --dry-run 并指定 --model 即可真机开训。")
    return 0


def train(args) -> int:
    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        from trl import SFTConfig, SFTTrainer
    except ImportError as e:
        print(f"缺少训练依赖: {e}\n请先执行: pip install torch transformers peft trl datasets accelerate")
        return 2

    os.makedirs(args.output, exist_ok=True)
    print(f"加载模型 {args.model} ...")
    tok = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    quant = None
    if args.load_4bit:
        quant = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )

    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        torch_dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16,
        device_map="auto",
        quantization_config=quant,
        trust_remote_code=True,
    )
    if quant:
        model = prepare_model_for_kbit_training(model)
    model.config.use_cache = False

    lora = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=args.target_modules.split(","),
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    tr_raw = read_jsonl(args.train, args.max_samples)
    va_raw = read_jsonl(args.val, args.max_val)
    random.Random(42).shuffle(tr_raw)

    def build(rows):
        return [
            {
                "messages": to_chat(r),
                "text": tok.apply_chat_template(to_chat(r), tokenize=False, add_generation_prompt=False),
            }
            for r in rows
        ]

    ds_tr = Dataset.from_list(build(tr_raw))
    ds_va = Dataset.from_list(build(va_raw)) if va_raw else None

    cfg = SFTConfig(
        output_dir=args.output,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup_ratio=0.03,
        logging_steps=args.logging_steps,
        save_steps=args.save_steps,
        save_total_limit=3,
        bf16=torch.cuda.is_bf16_supported(),
        gradient_checkpointing=True,
        max_length=args.max_length,
        packing=args.packing,
        eval_strategy="steps" if ds_va else "no",
        eval_steps=args.save_steps if ds_va else None,
        report_to=[],
        seed=42,
    )

    trainer = SFTTrainer(
        model=model,
        args=cfg,
        train_dataset=ds_tr,
        eval_dataset=ds_va,
        processing_class=tok,
    )
    trainer.train(resume_from_checkpoint=args.resume)
    trainer.save_model(args.output)
    tok.save_pretrained(args.output)
    print(f"\nLoRA 权重已保存到: {args.output}")

    # 推理自测
    print("\n推理自测:")
    model.eval()
    for t in args.test_titles:
        msgs = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "你是一名资深电商文案策划。请根据商品标题, 撰写一段有感染力的商品推广文案。\n" + t},
        ]
        ids = tok.apply_chat_template(msgs, tokenize=True, add_generation_prompt=True, return_tensors="pt").to(model.device)
        with torch.no_grad():
            out = model.generate(ids, max_new_tokens=200, temperature=0.9, do_sample=True, top_p=0.9)
        text = tok.decode(out[0][ids.shape[-1]:], skip_special_tokens=True)
        print(f"\n[{t}]\n{text.strip()}")
    return 0


def merge(args) -> int:
    try:
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as e:
        print(f"缺少依赖: {e}")
        return 2
    base = args.merge_base
    print(f"合并 {args.merge} + {base} -> {args.merge_out}")
    tok = AutoTokenizer.from_pretrained(base, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(base, torch_dtype=torch.float16, trust_remote_code=True)
    model = PeftModel.from_pretrained(model, args.merge)
    model = model.merge_and_unload()
    model.save_pretrained(args.merge_out)
    tok.save_pretrained(args.merge_out)
    print("完成。可用 vLLM / Ollama 加载该目录直接部署。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter,
                                 description="商品文案 LoRA 微调")
    ap.add_argument("--train", default=DEFAULT_TRAIN)
    ap.add_argument("--val", default=DEFAULT_VAL)
    ap.add_argument("--model", default="Qwen/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--output", default=os.path.join(ROOT, "outputs", "copy-lora"))
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--batch-size", type=int, default=4)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--max-length", type=int, default=512)
    ap.add_argument("--max-samples", type=int, default=0, help="0=全部")
    ap.add_argument("--max-val", type=int, default=1000)
    ap.add_argument("--lora-r", type=int, default=16)
    ap.add_argument("--lora-alpha", type=int, default=32)
    ap.add_argument("--lora-dropout", type=float, default=0.05)
    ap.add_argument("--target-modules", default="q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj")
    ap.add_argument("--load-4bit", action="store_true")
    ap.add_argument("--packing", action="store_true")
    ap.add_argument("--logging-steps", type=int, default=20)
    ap.add_argument("--save-steps", type=int, default=500)
    ap.add_argument("--resume", default=None)
    ap.add_argument("--test-titles", nargs="*", default=[
        "2024春秋新款男士连帽卫衣 宽松潮流纯棉外套",
        "日系复古直筒牛仔裤男士宽松阔腿裤",
    ])
    ap.add_argument("--dry-run", action="store_true")
    # 合并模式
    ap.add_argument("--merge", default=None, help="LoRA 权重目录, 指定后进入合并模式")
    ap.add_argument("--merge-base", default="Qwen/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--merge-out", default=os.path.join(ROOT, "outputs", "copy-merged"))
    args = ap.parse_args()

    if args.merge:
        return merge(args)
    if args.dry_run:
        return dry_run(args)
    return train(args)


if __name__ == "__main__":
    sys.exit(main())
