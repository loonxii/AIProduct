# -*- coding: utf-8 -*-
"""
prompts.py — 平台风格模板与提示词构建

核心思路:
  从百万级真实商品文案中检索出与当前商品最相似的若干条真实文案,
  作为「风格参照」注入提示词, 让大模型模仿数据集的真实文风, 而不是自由发挥。
"""
from __future__ import annotations

import json
import random
import re
from typing import Any

# ------------------------------------------------------------------ 平台风格
# length: 期望字数区间; emoji: emoji 密度; structure: 结构要求
PLATFORMS: dict[str, dict[str, Any]] = {
    "taobao": {
        "id": "taobao",
        "name": "淘宝/天猫详情页",
        "icon": "袋",
        "desc": "结构化卖点, 突出材质版型与穿着场景, 兼顾转化",
        "length": [70, 140],
        "emoji": "少量(0-1个)",
        "tone": "热情、有购买欲、口语化但不失专业",
        "structure": "先抛一个使用场景或痛点, 再讲 2-3 个具体卖点(材质/版型/细节), 最后给一句搭配或选择建议",
        "extra": "卖点要具体到可感知的细节(如'前身两个小口袋''polo领'), 避免空泛形容词堆砌",
    },
    "jd": {
        "id": "jd",
        "name": "京东商品卖点",
        "icon": "东",
        "desc": "理性、参数感、品质可靠, 强调工艺与保障",
        "length": [70, 140],
        "emoji": "不使用",
        "tone": "理性、克制、可信, 像产品经理在介绍",
        "structure": "先给一句整体定位, 再分点说明材质/工艺/规格/适用场景, 收尾给品质或服务承诺",
        "extra": "多用客观描述与规格词汇, 不使用夸张语气词, 不出现'最''第一'等绝对化表述",
    },
    "xhs": {
        "id": "xhs",
        "name": "小红书笔记",
        "icon": "书",
        "desc": "第一人称种草, 强情绪, emoji 丰富",
        "length": [50, 120],
        "emoji": "丰富(3-6个, 自然嵌入)",
        "tone": "第一人称、闺蜜式分享、情绪饱满",
        "structure": "用一句真实感受开场(如'真的会谢''谁懂啊'), 讲一个具体的使用体验细节, 再给搭配或人群建议, 结尾自然收",
        "extra": "必须有个人体验感, 不能像广告; 可以用'姐妹们''宝子'等称呼, 但不要每句都用",
    },
    "douyin": {
        "id": "douyin",
        "name": "抖音口播",
        "icon": "抖",
        "desc": "短句节奏, 强钩子, 适合读出来",
        "length": [50, 110],
        "emoji": "不使用或少用",
        "tone": "语速快、有节奏、有煽动力",
        "structure": "第一句必须是钩子(制造好奇/痛点/反差), 中间用短句连击讲 2 个卖点, 最后一句行动号召",
        "extra": "句子要短, 适合口播; 不要出现书面语长句; 不要罗列参数",
    },
    "wechat": {
        "id": "wechat",
        "name": "朋友圈软文",
        "icon": "圈",
        "desc": "生活化、亲和、无硬广感",
        "length": [40, 100],
        "emoji": "少量(0-2个)",
        "tone": "像朋友随口推荐, 平和自然",
        "structure": "从生活片段或心情切入, 自然带出商品, 讲一个小细节, 收尾轻描淡写",
        "extra": "绝对不要出现价格、促销、'买它'等硬广词; 不要用感叹号堆砌",
    },
    "pdd": {
        "id": "pdd",
        "name": "拼多多卖点",
        "icon": "拼",
        "desc": "直给性价比, 简单粗暴, 强紧迫感",
        "length": [40, 100],
        "emoji": "少量(0-2个)",
        "tone": "直接、接地气、有实惠感",
        "structure": "先说便宜/划算, 再说质量不将就, 最后催单",
        "extra": "突出实惠和不怕比, 语言要短平快; 不要编造具体折扣数字",
    },
}

# ------------------------------------------------------------------ 文案风格
STYLES: dict[str, dict[str, str]] = {
    "auto": {"id": "auto", "name": "自动匹配", "desc": "按平台特性自动选择最合适的语气", "rule": ""},
    "zhongcao": {
        "id": "zhongcao",
        "name": "种草安利",
        "desc": "真实体验感, 有情绪, 像在分享",
        "rule": "以第一人称真实体验切入, 传递'我用过/我想要'的情绪, 让人产生代入感",
    },
    "promo": {
        "id": "promo",
        "name": "促销带货",
        "desc": "突出划算与稀缺, 推动下单",
        "rule": "强调划算、限时、不容错过, 制造紧迫感, 但不得编造具体折扣金额或倒计时",
    },
    "pro": {
        "id": "pro",
        "name": "专业理性",
        "desc": "讲工艺讲参数, 建立信任",
        "rule": "用客观、专业、可验证的表达讲材质工艺与规格, 避免夸张修饰",
    },
    "literary": {
        "id": "literary",
        "name": "文艺质感",
        "desc": "有画面感, 讲究文字节奏",
        "rule": "用有画面感的比喻和克制的修饰, 营造质感与氛围, 不堆砌形容词",
    },
    "humor": {
        "id": "humor",
        "name": "幽默风趣",
        "desc": "轻松诙谐, 有记忆点",
        "rule": "用轻松诙谐的表达制造记忆点, 可以自嘲或玩梗, 但不要低俗",
    },
    "minimal": {
        "id": "minimal",
        "name": "极简高级",
        "desc": "短句留白, 少即是多",
        "rule": "用短句和留白, 只说最关键的信息, 克制用词, 不用感叹号",
    },
}

# ------------------------------------------------------------------ 广告法合规
# 《广告法》第九条绝对化用语高风险词
FORBIDDEN_WORDS = [
    "国家级", "世界级", "最高级", "最佳", "最优", "最好", "最强", "最便宜", "最低价",
    "第一品牌", "全网第一", "全国第一", "销量第一", "排名第一", "顶级", "极致",
    "绝对", "100%", "百分百", "万能", "永久", "独一无二", "独家专利", "史上最",
    "包治", "根除", "特效", "绝无仅有", "空前绝后", "领导品牌", "王牌", "至尊",
    "零风险", "无任何副作用", "无效退款", "全球领先", "唯一", "首个",
]

NUMBER_CLAIM_RE = re.compile(r"\d+\s*%\s*(?:有效|见效|改善|提升|减少|降低)")
PRICE_RE = re.compile(r"(?:仅需|只要|低至|立减|直降)\s*[\d.]+\s*元")


def check_compliance(text: str) -> list[dict[str, str]]:
    """返回合规风险提示列表(不阻断, 仅提醒)"""
    issues: list[dict[str, str]] = []
    for w in FORBIDDEN_WORDS:
        if w in text:
            issues.append({"level": "high", "word": w, "msg": f"「{w}」属绝对化用语, 违反《广告法》第九条"})
    m = NUMBER_CLAIM_RE.search(text)
    if m:
        issues.append({"level": "high", "word": m.group(0), "msg": "功效数据需有依据, 无资质时建议删除"})
    if PRICE_RE.search(text):
        issues.append({"level": "mid", "word": PRICE_RE.search(text).group(0), "msg": "具体价格与折扣需与实际活动一致, 否则构成虚假宣传"})
    if re.search(r"[!！]{3,}", text):
        issues.append({"level": "low", "word": "连续感叹号", "msg": "连续感叹号影响专业感, 建议精简"})
    return issues


# ------------------------------------------------------------------ 提示词构建

SYSTEM_PROMPT = (
    "你是一名资深电商文案策划, 长期为淘宝、京东、小红书、抖音等平台的品牌方撰写商品文案。"
    "你写的文案特点是: 具体、有画面感、不空泛, 每句话都能让读者感知到商品的实际好处。"
    "你严格遵守《中华人民共和国广告法》, 从不使用绝对化用语, 从不编造商品不具备的参数或功效。"
)


def _fmt_references(references: list[dict[str, Any]], max_items: int = 8) -> str:
    if not references:
        return "(无参考样例)"
    lines: list[str] = []
    seen: set[str] = set()
    for r in references:
        d = r.get("desc", "").strip()
        if not d or d in seen:
            continue
        seen.add(d)
        lines.append(f"{len(lines)+1}. {d}")
        if len(lines) >= max_items:
            break
    return "\n".join(lines) if lines else "(无参考样例)"


def build_user_prompt(
    title: str,
    platform: str,
    style: str,
    n: int,
    selling_points: list[str] | None = None,
    audience: str = "",
    price_info: str = "",
    length_override: tuple[int, int] | None = None,
    references: list[dict[str, Any]] | None = None,
    use_references: bool = True,
    avoid: list[str] | None = None,
) -> str:
    p = PLATFORMS.get(platform) or PLATFORMS["taobao"]
    s = STYLES.get(style) or STYLES["auto"]
    lo, hi = length_override or p["length"]

    parts: list[str] = []
    parts.append("## 商品信息")
    parts.append(f"- 商品标题: {title}")
    if selling_points:
        sp = "、".join(x.strip() for x in selling_points if x.strip())
        if sp:
            parts.append(f"- 必须体现的卖点: {sp}")
    if audience:
        parts.append(f"- 目标人群: {audience}")
    if price_info:
        parts.append(f"- 价格/活动信息(可提及, 不得改动数字): {price_info}")

    parts.append("")
    parts.append("## 写作要求")
    parts.append(f"- 发布平台: {p['name']}（{p['desc']}）")
    parts.append(f"- 语气: {p['tone']}")
    parts.append(f"- 结构: {p['structure']}")
    parts.append(f"- emoji: {p['emoji']}")
    parts.append(f"- 平台特别要求: {p['extra']}")
    if s["rule"]:
        parts.append(f"- 文案风格: {s['name']} —— {s['rule']}")
    parts.append(f"- 每条字数: {lo}-{hi} 个汉字")
    parts.append(f"- 生成 {n} 条, 每条切入角度必须不同(例如: 材质细节 / 使用场景 / 人群共鸣 / 搭配建议 / 情绪价值)")

    if use_references and references:
        parts.append("")
        parts.append("## 真实文案风格参照")
        parts.append("以下是同品类真实在售商品的文案, 请**只借鉴其用词习惯、句式节奏和卖点颗粒度**, 不要照抄词句, 也不要引入其中与本商品无关的信息:")
        parts.append(_fmt_references(references))

    if avoid:
        parts.append("")
        parts.append("## 避免重复")
        parts.append("不要与以下已有文案雷同: " + " | ".join(avoid[:3]))

    parts.append("")
    parts.append("## 硬性约束")
    parts.append("1. 不得出现《广告法》绝对化用语(最、第一、国家级、100%、绝对、顶级等)")
    parts.append("2. 不得编造商品不具备的材质、成分、功效、认证或折扣数字")
    parts.append("3. 不要出现店铺名、联系方式、链接、'点击购买'等导流信息")
    parts.append("4. 只输出文案本身, 不要写'以下是为您生成的文案'这类说明性文字")

    parts.append("")
    parts.append("## 输出格式")
    parts.append("严格输出 JSON, 不要包裹代码块, 不要有任何多余文字:")
    parts.append('{"items":[{"angle":"切入角度(4-8字)","text":"文案正文"}]}')
    parts.append(f"items 数组长度必须为 {n}。")

    return "\n".join(parts)


def build_messages(**kwargs) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(**kwargs)},
    ]


# ------------------------------------------------------------------ 结果解析

_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")
# 完整的 "text":"..." (含转义处理)
_RE_TEXT_CLOSED = re.compile(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"')
# 被截断的尾部 "text":"...<EOF>
_RE_TEXT_OPEN = re.compile(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)$', re.S)
# 判断一段文本是不是裸露的 JSON 片段 —— 这种绝对不能当文案返回给用户
_RE_JSON_FRAGMENT = re.compile(
    r"""\{\s*['"]items['"]|['"](?:angle|text|content|copy)['"]\s*:|^\s*[{}\[\],'"]+\s*$"""
)


def _unescape_json_string(s: str) -> str:
    try:
        return json.loads('"' + s + '"')
    except Exception:
        return s.replace("\\n", " ").replace('\\"', '"').replace("\\\\", "\\")


def _looks_like_json_fragment(s: str) -> bool:
    return bool(_RE_JSON_FRAGMENT.search(s))


def _try_load(text: str) -> Any:
    """依次尝试标准 JSON 与 Python 字面量(容忍单引号脏 JSON)"""
    import ast

    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        v = ast.literal_eval(text)
        if isinstance(v, (dict, list)):
            return v
    except Exception:
        pass
    return None


def _normalize_items(raw_items: list, expect: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for it in raw_items:
        if isinstance(it, dict):
            t = str(it.get("text") or it.get("content") or it.get("copy") or "").strip()
            a = str(it.get("angle") or it.get("title") or "").strip()
            if t and not _looks_like_json_fragment(t):
                out.append({"angle": a or "文案", "text": t})
        elif isinstance(it, str) and it.strip() and not _looks_like_json_fragment(it):
            out.append({"angle": "文案", "text": it.strip()})
    return out[: max(expect, 1)] if out else []


def _salvage_from_text(text: str, expect: int) -> list[dict[str, str]]:
    """JSON 整体解析失败时, 用正则把 items 抢救出来。

    先抓完整闭合的 "text":"...", 一条都抓不到再尝试抓被截断的尾部
    (模型思考过长导致输出被截断时会出现)。
    """
    angles = [m.group(1) for m in re.finditer(r'"angle"\s*:\s*"((?:[^"\\]|\\.)*)"', text)]
    texts = [_unescape_json_string(m.group(1)) for m in _RE_TEXT_CLOSED.finditer(text)]
    if not texts:
        m = _RE_TEXT_OPEN.search(text)
        if m and len(m.group(1)) >= 12:
            texts = [_unescape_json_string(m.group(1))]
    out = []
    for i, t in enumerate(texts):
        t = t.strip()
        if len(t) >= 8 and re.search(r"[\u4e00-\u9fff]", t) and not _looks_like_json_fragment(t):
            out.append({"angle": angles[i] if i < len(angles) else "文案", "text": t})
    return out[: max(expect, 1)]


def parse_items(raw: str, expect: int = 3) -> list[dict[str, str]]:
    """把模型输出解析为 [{angle, text}]，容忍常见的非严格 JSON 与输出截断。

    重要: 任何情况下都不能把裸露的 JSON 片段当成文案返回 —— 那会让用户
    看到 `{"items":[{"angle":"材质细节","text":"...` 这种东西。
    """
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    data = None
    try:
        data = json.loads(text)
    except Exception:
        m = _JSON_BLOCK.search(text)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if data is None:
        # 容忍单引号之类的脏 JSON
        data = _try_load(text)
        if data is None:
            m = _JSON_BLOCK.search(text)
            if m:
                data = _try_load(m.group(0))

    items: list[dict[str, str]] = []
    if isinstance(data, dict):
        arr = data.get("items") or data.get("data") or data.get("result") or []
        if isinstance(arr, list):
            items = _normalize_items(arr, expect)
    elif isinstance(data, list):
        items = _normalize_items(data, expect)
    if items:
        return items

    # JSON 解析失败 → 正则抢救
    items = _salvage_from_text(text, expect)
    if items:
        return items

    # 最后兜底: 按行切分(必须过滤掉 JSON 片段)
    cleaned = re.sub(r"```[a-zA-Z]*", "", text)
    cand: list[dict[str, str]] = []
    for line in cleaned.split("\n"):
        line = line.strip().strip('",')
        line = re.sub(r"^\s*[-*\d]+[.、)]\s*", "", line)
        line = re.sub(r'^"(?:text|angle|content)"\s*:\s*', "", line).strip(' "')
        if len(line) >= 12 and re.search(r"[\u4e00-\u9fff]", line) and not _looks_like_json_fragment(line):
            cand.append({"angle": "文案", "text": line})
    return cand[: max(expect, 1)]


def pick_platform_list() -> list[dict[str, Any]]:
    return [{k: v[k] for k in ("id", "name", "icon", "desc", "length")} for v in PLATFORMS.values()]


def pick_style_list() -> list[dict[str, Any]]:
    return [{k: v[k] for k in ("id", "name", "desc")} for v in STYLES.values()]
