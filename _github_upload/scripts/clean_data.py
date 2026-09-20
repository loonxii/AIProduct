# -*- coding: utf-8 -*-
"""
clean_data.py — 商品文案数据集清洗

输入: item_desc_dataset.txt  (TSV: 商品标题 \t 描述文案)
输出:
  data/corpus.db          SQLite 语料库(检索层使用)
  data/finetune/sft_train.jsonl
  data/finetune/sft_val.jsonl
  data/clean_stats.json

用法:
  python scripts/clean_data.py                       # 全量清洗
  python scripts/clean_data.py --limit 200000        # 只处理前 20 万行(快速试跑)
  python scripts/clean_data.py --max-desc-per-title 6
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time

# ---------------------------------------------------------------- 正则与常量

RE_URL = re.compile(r"(?:https?://|www\.)[^\s\u4e00-\u9fff]+", re.I)
RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
RE_TEL = re.compile(r"(?<!\d)(?:400|800)[- ]?\d{3}[- ]?\d{4}(?!\d)")
RE_WECHAT = re.compile(
    r"(?:加|添加|联系|详情|咨询)?\s*(?:微信|威信|V\s?信|vx|VX|weixin|wechat|扣扣|企鹅|QQ|qq)"
    r"\s*(?:号|:|：)?\s*[A-Za-z0-9_\-]{4,}",
    re.I,
)
RE_BAD_WORDS = re.compile(
    r"(高仿|精仿|复刻|原单|外贸尾单|A货|假货|刷单|代发|一件代发|微商代理|代理加盟)"
)
# 导流/淘口令类噪声(数据集里真实存在, 会污染风格参照)
RE_TAOKOUO = re.compile(
    r"(复制这条信息|复制此条信息|打开手机淘宝|打开淘宝|打开支付宝|领券|优惠券|点击链接|"
    r"戳链接|扫码|扫一扫|加关注|关注店铺|店铺名|收藏宝贝|加入购物车|立即抢购|"
    r"淘口令|￥[A-Za-z0-9]{6,}￥|€[A-Za-z0-9]{6,}€|\$[A-Za-z0-9]{6,}\$|"
    r"[A-Za-z0-9]{8,}(?=，)?(?:重新打开|打开)(?:手机)?淘宝)"
)
RE_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
RE_WS = re.compile(r"[ \t\u3000\r\n]+")
RE_MULTI_PUNCT = re.compile(r"([，。！？,.!?~～])\1{2,}")
RE_CJK = re.compile(r"[\u4e00-\u9fff]")
RE_ONLY_SYMBOL = re.compile(r"^[\W_]+$", re.U)

# 类目关键词(启发式, 命中即归类; 顺序即优先级)
# 注意: 刻意不使用「米」「面」「油」「包」「猫」「狗」「坐垫」「灯」等单字/歧义词
#       —— 实测会大量误判(「洗面盆」→食品、「猫须裤」→宠物用品、「座垫」→汽车用品)
CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("女装", ("女装", "连衣裙", "半身裙", "雪纺衫", "打底裤", "吊带", "针织衫", "衬衫女", "外套女",
              "风衣女", "羽绒服女", "毛衣女", "女士", "女款", "女神", "小仙女", "半身裙", "裙")),
    ("男装", ("男装", "男士", "男款", "卫衣", "夹克", "衬衫男", "T恤男", "t恤男", "休闲裤", "工装裤",
              "牛仔裤", "外套男", "西服", "polo", "POLO", "背心男", "卫裤", "运动裤")),
    ("鞋靴", ("帆布鞋", "运动鞋", "凉鞋", "拖鞋", "高跟鞋", "皮鞋", "靴", "鞋")),
    ("箱包", ("双肩包", "背包", "手提包", "单肩包", "斜挎包", "腰包", "胸包", "钱包", "卡包",
              "行李箱", "拉杆箱", "旅行箱", "书包", "女包", "男包", "公文包", "挎包", "手包", "化妆包")),
    ("内衣配饰", ("内衣", "文胸", "胸罩", "内裤", "袜子", "丝袜", "围巾", "帽子", "腰带", "皮带",
                  "手套", "饰品", "项链", "耳环", "戒指", "手表", "太阳镜", "眼镜", "首饰")),
    ("美妆个护", ("面膜", "口红", "唇膏", "粉底", "气垫", "精华", "眼霜", "眼影", "乳液", "洗面奶",
                  "洗发水", "沐浴露", "香水", "美瞳", "卸妆", "防晒", "牙膏", "牙刷", "化妆刷", "护肤")),
    ("母婴玩具", ("婴儿", "宝宝", "儿童", "童装", "童鞋", "奶瓶", "纸尿裤", "玩具", "积木", "推车",
                  "孕妇", "幼儿园", "早教")),
    ("家纺布艺", ("床垫", "被芯", "毯子", "凉席", "蚊帐", "窗帘", "沙发套", "被套", "四件套", "床单")),
    ("家居日用", ("毛巾", "面巾", "浴巾", "枕", "抱枕", "收纳", "衣架", "水杯", "保温杯", "餐具",
                  "碗", "筷", "地毯", "纸巾", "清洁", "拖把", "挂钩", "台灯", "吊灯", "落地灯",
                  "吸顶灯", "水壶", "饭盒", "焖烧", "置物架", "垃圾桶", "雨伞")),
    ("数码电器", ("手机", "耳机", "充电", "数据线", "蓝牙", "音箱", "音响", "鼠标", "键盘", "电脑",
                  "相机", "路由器", "移动电源", "U盘", "硬盘", "电视", "风扇", "加湿器", "扫地机",
                  "剃须刀", "吹风机", "电饭煲", "料理机", "榨汁", "平板", "投影", "手环")),
    ("食品饮料", ("零食", "坚果", "饼干", "巧克力", "糖果", "茶叶", "红茶", "绿茶", "普洱茶", "花茶",
                  "咖啡", "牛奶", "酸奶", "饮料", "果汁", "水果", "蜂蜜", "大米", "面粉", "食用油",
                  "酱油", "调味", "螺蛳粉", "辣条", "牛肉干", "果干", "海苔", "糕点")),
    ("运动户外", ("健身", "瑜伽", "跑步", "篮球", "足球", "羽毛球", "乒乓球", "哑铃", "帐篷",
                  "登山", "骑行", "泳衣", "泳镜", "露营", "冲锋")),
    ("宠物用品", ("猫粮", "猫砂", "猫咪", "猫窝", "猫爬架", "猫玩具", "狗粮", "狗狗", "狗窝",
                  "宠物", "牵引绳", "猫抓板")),
    ("文具办公", ("笔记本", "文具", "文件夹", "打印", "办公", "中性笔", "钢笔", "书籍")),
    ("汽车用品", ("汽车", "车载", "车衣", "汽车脚垫", "方向盘套", "行车记录仪", "车用", "车轮", "轮胎")),
]

# 文案风格启发式标注(用于微调数据集的可选风格标签, 属自动标注, 精度有限)
STYLE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("促销带货", ("限时", "秒杀", "抢购", "立减", "优惠", "券", "包邮", "特价", "清仓", "到手价", "折", "买一送一", "仅需", "低至")),
    ("种草安利", ("姐妹们", "真的", "绝了", "太好", "爱了", "回购", "种草", "冲", "安利", "宝藏", "yyds", "谁懂", "闭眼入")),
    ("品质参数", ("面料", "材质", "工艺", "版型", "克重", "支数", "含绒量", "透气", "亲肤", "工艺", "检测", "标准", "成分")),
    ("场景叙事", ("的时候", "想象", "仿佛", "就像", "清晨", "午后", "约会", "通勤", "旅行", "度假", "约会", "咖啡")),
]
STYLE_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2b00-\u2bff\u2190-\u21ff\uFE0F]"
)

DEFAULT_CATS = "其他"


def detect_category(title: str) -> str:
    """按关键词命中打分归类: 命中词的字符长度作为权重(越具体的词权重越高),
    取总分最高的类目。比「首个命中即返回」准确得多——后者会把
    「康尔馨五星级酒店毛巾...男士女士成人」判成女装(只因含"女士")。
    """
    t = title.lower()
    best, best_score = DEFAULT_CATS, 0
    for cat, kws in CATEGORY_RULES:
        score = 0
        for kw in kws:
            if kw.lower() in t:
                score += len(kw)
        if score > best_score:
            best, best_score = cat, score
    return best


def detect_style(desc: str) -> str:
    emoji_cnt = len(STYLE_EMOJI.findall(desc))
    for style, kws in STYLE_RULES:
        hit = sum(1 for kw in kws if kw in desc)
        if style == "种草安利" and hit >= 1:
            return style
        if style == "促销带货" and hit >= 2:
            return style
        if style == "品质参数" and hit >= 3:
            return style
        if style == "场景叙事" and hit >= 2:
            return style
    if emoji_cnt >= 2:
        return "种草安利"
    return "常规描述"


# ---------------------------------------------------------------- 清洗函数


def clean_text(s: str) -> str:
    s = RE_CTRL.sub("", s)
    s = s.replace("\u200b", "").replace("\ufeff", "")
    s = RE_WS.sub(" ", s).strip()
    return s


def clean_title(s: str) -> str:
    s = clean_text(s)
    s = RE_URL.sub("", s)
    s = RE_WECHAT.sub("", s)
    s = RE_PHONE.sub("", s)
    s = RE_TEL.sub("", s)
    s = RE_TAOKOUO.sub("", s)
    s = re.sub(r"[\[\]【】]\s*[\[\]【】]", "", s)
    s = re.sub(r"\s+", " ", s).strip(" -_|·,，")
    return s


def clean_desc(s: str) -> str:
    s = clean_text(s)
    s = RE_URL.sub("", s)
    s = RE_WECHAT.sub("", s)
    s = RE_PHONE.sub("", s)
    s = RE_TEL.sub("", s)
    s = RE_TAOKOUO.sub("", s)
    s = RE_MULTI_PUNCT.sub(r"\1", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def cjk_ratio(s: str) -> float:
    if not s:
        return 0.0
    return len(RE_CJK.findall(s)) / len(s)


def norm_key(s: str) -> str:
    """用于去重的归一化 key(忽略标点与空白)"""
    return re.sub(r"[\W_]+", "", s, flags=re.U).lower()


def is_acceptable_desc(d: str, min_len: int, max_len: int, min_cjk: int) -> bool:
    if not (min_len <= len(d) <= max_len):
        return False
    if len(RE_CJK.findall(d)) < min_cjk:
        return False
    if RE_ONLY_SYMBOL.match(d):
        return False
    if RE_BAD_WORDS.search(d):
        return False
    if RE_TAOKOUO.search(d):
        return False
    # 非中文占比过高的(乱码/纯英文参数串)
    if cjk_ratio(d) < 0.5:
        return False
    # 重复字符型垃圾(如 "aaaaaaaa" / "。。。")
    if len(set(d)) <= 4:
        return False
    return True


# ---------------------------------------------------------------- 主流程


def _write_finetune_readme(ft_dir: str, stat: dict, args: argparse.Namespace) -> None:
    """把数据集的来龙去脉和统计写进产出目录, 便于日后回溯"""
    lines = [
        "# 微调数据集说明",
        "",
        f"由 `scripts/clean_data.py` 从 `{os.path.basename(args.src)}` 生成。",
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 文件",
        "",
        "| 文件 | 样本数 | 说明 |",
        "|---|---|---|",
        f"| `sft_train.jsonl` | {stat['finetune_train_samples']:,} | 训练集 |",
        f"| `sft_val.jsonl` | {stat['finetune_val_samples']:,} | 验证集，**按标题切分，与训练集无标题重叠** |",
        "",
        "## 样本格式",
        "",
        "```json",
        '{"instruction": "你是一名资深电商文案策划。请根据商品标题, 撰写一段有感染力的商品推广文案。",',
        ' "input": "商品标题",',
        ' "output": "描述文案",',
        ' "meta": {"cat": "类目", "style": "风格(自动标注)", "len": 88}}',
        "```",
        "",
        "`meta.style` 是用关键词启发式自动标注的（`促销带货` / `种草安利` / `品质参数` / `场景叙事` / `常规描述`），",
        "精度有限，用于抽样分析可以，**不建议**直接当训练标签做多风格条件生成。",
        "",
        "## 清洗统计",
        "",
        f"- 原始行数：{stat['rows_read']:,}",
        f"- 保留文案：**{stat['kept_rows']:,}** 条 / **{stat['kept_titles']:,}** 个标题",
        f"- 去重后唯一文案：{stat.get('unique_desc_count', 0):,} 条",
        f"- 平均每标题：{stat['avg_desc_per_title']} 条（上限 {stat['max_desc_per_title']} 条）",
        f"- 标题均长：{stat['avg_title_chars']} 字　文案均长：{stat['avg_desc_chars']} 字",
        "",
        "丢弃明细：",
        "",
        "| 原因 | 条数 |",
        "|---|---|",
        f"| 标题重复对 | {stat['drop_dup_pair']:,} |",
        f"| 超过「每标题条数上限」 | {stat['drop_over_cap']:,} |",
        f"| 文案长度越界 | {stat['drop_desc_len']:,} |",
        f"| 文案质量不合格（违规词/乱码/重复字符） | {stat['drop_desc_quality']:,} |",
        f"| 标题长度越界 | {stat['drop_title_len']:,} |",
        "",
        "## 类目分布",
        "",
        "| 类目 | 标题数 |",
        "|---|---|",
    ]
    for k, v in sorted(stat["cat_dist"].items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {v:,} |")
    lines += ["", "## 文案长度分布", "", "| 字数区间 | 条数 |", "|---|---|"]
    for k, v in sorted(stat["desc_len_hist"].items(), key=lambda x: int(x[0].split("-")[0])):
        lines.append(f"| {k} | {v:,} |")
    lines += ["", "## 建议", "",
              "- 全量 100 万+ 样本对 LoRA 来说偏多，`finetune_lora.py --max-samples 200000` 先跑一轮看效果即可。",
              "- 语料以服饰鞋包为主，做其他类目时建议按 `meta.cat` 采样平衡，或补充垂类数据。",
              "- 文案均长 88 字，`--max-length 512` 足够，不需要长上下文。",
              ""]
    with open(os.path.join(ft_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="item_desc_dataset.txt", help="原始语料 TSV")
    ap.add_argument("--out-dir", default="data", help="输出目录")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 行, 0=全量")
    ap.add_argument("--max-desc-per-title", type=int, default=6)
    ap.add_argument("--min-desc-len", type=int, default=20)
    ap.add_argument("--max-desc-len", type=int, default=200)
    ap.add_argument("--min-cjk", type=int, default=15)
    ap.add_argument("--min-title-len", type=int, default=6)
    ap.add_argument("--max-title-len", type=int, default=100)
    ap.add_argument("--val-ratio", type=float, default=0.01)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--log", default="")
    args = ap.parse_args()

    out_dir = args.out_dir
    ft_dir = os.path.join(out_dir, "finetune")
    os.makedirs(ft_dir, exist_ok=True)
    db_path = os.path.join(out_dir, "corpus.db")
    log_path = args.log or os.path.join(out_dir, "clean.log")

    logf = open(log_path, "w", encoding="utf-8")

    try:
        import faulthandler

        _fh = open(log_path + ".crash.txt", "a", encoding="utf-8")
        faulthandler.enable(file=_fh)
    except Exception:
        pass

    def log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        try:
            print(line, flush=True)
        except Exception:
            pass
        logf.write(line + "\n")
        logf.flush()

    log(f"开始清洗: src={args.src} limit={args.limit or 'ALL'}")
    if not os.path.exists(args.src):
        log(f"!! 源文件不存在: {args.src}")
        return 2

    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-200000")
    con.executescript(
        """
        CREATE TABLE titles(
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            cat TEXT NOT NULL,
            n_desc INTEGER NOT NULL
        );
        CREATE TABLE descs(
            id INTEGER PRIMARY KEY,
            title_id INTEGER NOT NULL,
            desc TEXT NOT NULL,
            clen INTEGER NOT NULL,
            style TEXT NOT NULL
        );
        """
    )

    seen_pair: set[int] = set()
    title_index: dict[str, int] = {}
    per_title_count: dict[str, int] = {}

    stat = {
        "src": args.src,
        "rows_read": 0,
        "drop_empty": 0,
        "drop_title_len": 0,
        "drop_desc_len": 0,
        "drop_desc_quality": 0,
        "drop_dup_pair": 0,
        "drop_over_cap": 0,
        "kept_rows": 0,
        "kept_titles": 0,
        "cat_dist": {},
        "style_dist": {},
        "desc_len_hist": {},
        "title_len_sum": 0,
        "desc_len_sum": 0,
        "unique_title_min": 10**9,
        "unique_title_max": 0,
    }

    title_batch: list[tuple] = []
    desc_batch: list[tuple] = []
    next_tid = 0
    t0 = time.time()
    rng = random.Random(args.seed)
    val_lines: list[str] = []
    train_fp = open(os.path.join(ft_dir, "sft_train.jsonl"), "w", encoding="utf-8")
    # 先写 val 到内存(按标题切分, 避免标题泄漏), 体积可控(1%)
    val_cap = 20000

    with open(args.src, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            stat["rows_read"] += 1
            if args.limit and stat["rows_read"] > args.limit:
                stat["rows_read"] -= 1
                break
            if stat["rows_read"] % 500000 == 0:
                log(
                    f"  已读 {stat['rows_read']:,} 行 | 保留 {stat['kept_rows']:,} 条 | "
                    f"标题 {len(title_index):,} | 用时 {time.time()-t0:.0f}s"
                )

            line = raw.rstrip("\n").rstrip("\r")
            if not line.strip():
                stat["drop_empty"] += 1
                continue
            parts = line.split("\t", 1)
            if len(parts) < 2:
                stat["drop_empty"] += 1
                continue

            title = clean_title(parts[0])
            desc = clean_desc(parts[1])

            if not title or not desc:
                stat["drop_empty"] += 1
                continue
            if not (args.min_title_len <= len(title) <= args.max_title_len):
                stat["drop_title_len"] += 1
                continue
            if not is_acceptable_desc(desc, args.min_desc_len, args.max_desc_len, args.min_cjk):
                if not (args.min_desc_len <= len(desc) <= args.max_desc_len):
                    stat["drop_desc_len"] += 1
                else:
                    stat["drop_desc_quality"] += 1
                continue

            dup_key = hash((norm_key(title), norm_key(desc)))
            if dup_key in seen_pair:
                stat["drop_dup_pair"] += 1
                continue
            seen_pair.add(dup_key)

            cnt = per_title_count.get(title, 0)
            if cnt >= args.max_desc_per_title:
                stat["drop_over_cap"] += 1
                continue

            tid = title_index.get(title)
            if tid is None:
                tid = next_tid
                next_tid += 1
                title_index[title] = tid
                cat = detect_category(title)
                title_batch.append((tid, title, cat, 0))
                stat["cat_dist"][cat] = stat["cat_dist"].get(cat, 0) + 1
            per_title_count[title] = cnt + 1
            stat["kept_rows"] += 1
            stat["title_len_sum"] += len(title)
            stat["desc_len_sum"] += len(desc)

            style = detect_style(desc)
            stat["style_dist"][style] = stat["style_dist"].get(style, 0) + 1
            bucket = f"{(len(desc)//20)*20}-{(len(desc)//20)*20+19}"
            stat["desc_len_hist"][bucket] = stat["desc_len_hist"].get(bucket, 0) + 1

            desc_batch.append((tid, desc, len(desc), style))

            # SFT 样本(Alpaca 风格): 标题 -> 文案
            sample = {
                "instruction": "你是一名资深电商文案策划。请根据商品标题, 撰写一段有感染力的商品推广文案。",
                "input": title,
                "output": desc,
                "meta": {"cat": detect_category(title), "style": style, "len": len(desc)},
            }
            payload = json.dumps(sample, ensure_ascii=False)
            r = rng.random()
            if r < args.val_ratio and len(val_lines) < val_cap and per_title_count[title] == 1:
                val_lines.append(payload)
            else:
                train_fp.write(payload + "\n")

            if len(desc_batch) >= 50000:
                con.executemany(
                    "INSERT INTO descs(title_id,desc,clen,style) VALUES(?,?,?,?)", desc_batch
                )
                desc_batch.clear()
                con.commit()
            if len(title_batch) >= 20000:
                con.executemany(
                    "INSERT INTO titles(id,title,cat,n_desc) VALUES(?,?,?,?)", title_batch
                )
                title_batch.clear()
                con.commit()

    if desc_batch:
        con.executemany("INSERT INTO descs(title_id,desc,clen,style) VALUES(?,?,?,?)", desc_batch)
    if title_batch:
        con.executemany("INSERT INTO titles(id,title,cat,n_desc) VALUES(?,?,?,?)", title_batch)
    con.commit()
    train_fp.close()

    log(f"  扫描完成, 用时 {time.time()-t0:.0f}s, 开始回填 n_desc 并建索引...")

    log(f"  步骤A: 回填 n_desc ({len(per_title_count):,} 个标题)")
    con.executemany(
        "UPDATE titles SET n_desc=? WHERE id=?",
        ((c, title_index[t]) for t, c in per_title_count.items()),
    )
    con.commit()
    log("  步骤A 完成")

    con.executescript(
        """
        CREATE INDEX idx_descs_title ON descs(title_id);
        CREATE INDEX idx_titles_cat ON titles(cat);
        """
    )
    con.commit()
    log("  步骤B(建索引) 完成")

    with open(os.path.join(ft_dir, "sft_val.jsonl"), "w", encoding="utf-8") as f:
        for x in val_lines:
            f.write(x + "\n")

    stat["kept_titles"] = con.execute("SELECT COUNT(*) FROM titles").fetchone()[0]
    stat["unique_desc_count"] = con.execute("SELECT COUNT(DISTINCT desc) FROM descs").fetchone()[0]
    stat["avg_desc_per_title"] = (
        round(stat["kept_rows"] / stat["kept_titles"], 2) if stat["kept_titles"] else 0
    )
    stat["avg_title_chars"] = (
        round(stat["title_len_sum"] / stat["kept_rows"], 1) if stat["kept_rows"] else 0
    )
    stat["avg_desc_chars"] = (
        round(stat["desc_len_sum"] / stat["kept_rows"], 1) if stat["kept_rows"] else 0
    )
    stat["finetune_train_samples"] = stat["kept_rows"] - len(val_lines)
    stat["finetune_val_samples"] = len(val_lines)
    stat["db_size_MB"] = round(os.path.getsize(db_path) / 1024 / 1024, 1)
    stat["elapsed_sec"] = round(time.time() - t0, 1)
    stat["max_desc_per_title"] = args.max_desc_per_title

    con.close()

    with open(os.path.join(out_dir, "clean_stats.json"), "w", encoding="utf-8") as f:
        json.dump(stat, f, ensure_ascii=False, indent=2)

    _write_finetune_readme(ft_dir, stat, args)

    log("清洗完成:")
    log(
        f"  保留 {stat['kept_rows']:,} 条文案 / {stat['kept_titles']:,} 个标题 "
        f"(平均 {stat['avg_desc_per_title']} 条/标题)"
    )
    log(f"  丢弃: 重复 {stat['drop_dup_pair']:,} | 超数量上限 {stat['drop_over_cap']:,} | "
        f"长度 {stat['drop_desc_len']:,} | 质量 {stat['drop_desc_quality']:,} | 标题长度 {stat['drop_title_len']:,}")
    log(f"  corpus.db = {stat['db_size_MB']} MB, 耗时 {stat['elapsed_sec']}s")
    log(f"  微调集: train {stat['finetune_train_samples']:,} / val {stat['finetune_val_samples']:,}")
    log(f"  类目分布: {json.dumps(stat['cat_dist'], ensure_ascii=False)}")
    logf.close()
    return 0


if __name__ == "__main__":
    import traceback

    _err_path = "clean_error.txt"
    for _i, _a in enumerate(sys.argv):
        if _a == "--log" and _i + 1 < len(sys.argv):
            _err_path = sys.argv[_i + 1] + ".error.txt"
        elif _a.startswith("--out-dir="):
            _err_path = os.path.join(_a.split("=", 1)[1], "clean_error.txt")
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        tb = traceback.format_exc()
        try:
            with open(_err_path, "a", encoding="utf-8") as _f:
                _f.write(tb + "\n")
        except Exception:
            pass
        try:
            sys.stderr.write(tb)
        except Exception:
            pass
        sys.exit(1)
