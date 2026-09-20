# 商品描述文案助手

基于**百万级真实电商商品文案语料**的检索增强（RAG）文案生成服务。

输入一个商品标题，系统先从真实语料里找出同类商品是怎么写的，再让大模型按目标平台的调性产出多条候选文案。

- 生成引擎：**真实文案检索 + 大模型**（三种通道：平台托管 / 自备 API Key / 本地 Ollama）
- 启动方式：`python launcher.py` 一键启动（自动顺延端口、自动开浏览器）
- 形态：原生前端单页 + FastAPI 后端，无构建步骤
- 附带产出：微调数据集（由清洗流程顺带生成，Alpaca 格式）
- 完整流水线：数据清洗 / 索引构建 / LoRA 微调脚本一并在 `scripts/`

> **关于数据**：语料库与索引体积大（数百 MB 级）且可由原始语料重建，未随仓库提供，
> 用 `scripts/` 下的脚本即可生成。轻量元数据文件（类目表、索引元信息）已随仓库提供，
> 用于说明数据格式。详见 [`data/README.md`](data/README.md)。

---

## 一、这套东西解决什么问题

直接让大模型写商品文案，有几个通病：空泛（"品质卓越，值得拥有"）、千篇一律、不接地气。

本项目拆成三步解决：

1. **先检索，再生成。** 语料库里全是真实在售商品的真实文案，检索出最相似的若干同款商品，把它们的文案作为风格参照写进提示词。模型模仿的是真实卖家的用词颗粒度，而不是自己想象。
2. **按平台定制。** 淘宝详情页要结构化卖点，小红书要第一人称种草，抖音口播要短句钩子。每个平台有独立的语气、结构、emoji、字数规则。
3. **合规前置。** 生成后自动扫描《广告法》绝对化用语、无依据的功效承诺、具体价格宣称，逐条给出风险等级。

---

## 二、数据资产

原始语料为双字段 TSV：`商品标题 \t 描述文案`。

**数据集下载**：<https://tianchi.aliyun.com/dataset/9717>（天池 · 淘宝商品描述数据集）

| 指标 | 数值 |
|---|---|
| 原始语料 | 约百万行级 / 数百 MB |
| 唯一商品标题 | 数十万级 |
| 文案均长 | **约 88 字（9–150 字，集中在 50–99）** |
| 类目覆盖 | 16 个类目 |

> 文案均长只有 88 字 → 这是**电商卖点短文**，不是详情页长文。这个长度对生成质量非常友好，也决定了提示词的字数约束区间。

清洗产出：

| 产出 | 说明 |
|---|---|
| `data/corpus.db` | SQLite 语料库：标题表 + 文案表 |
| `data/index/index.npz` | BM25 倒排索引（字符 bigram） |
| `data/index/vocab.txt` | 词表 |
| `data/finetune/sft_train.jsonl` | 微调训练集（Alpaca 格式） |
| `data/finetune/sft_val.jsonl` | 微调验证集（**按标题切分，无标题泄漏**） |

清洗规则要点：

- **去重**：`(标题, 文案)` 归一化后完全相同的丢弃（忽略标点与空白差异）
- **长度过滤**：标题与文案均设上下界，文案要求至少含一定数量的汉字
- **质量过滤**：非中文占比过高、字符种类过少、含违规词的丢弃
- **广告清洗**：剔除 URL、手机号、400/800 电话、微信号、QQ 号
- **每标题条数上限**：原始数据里个别标题有数百条变体，必须截断以保证检索结果多样性
- **类目打标**：16 个类目的关键词启发式分类，用于检索时按类目收窄

---

## 三、检索是怎么做的

**为什么是 BM25 而不是向量检索？**

- 用户输入的就是**商品标题**，查询和文档同为短文本、同域同构，字符级匹配本身就非常强
- 中文电商标题分词边界模糊（"怪味少女"、"bdct自制"），词典分词反而引入误差。**字符 bigram 不依赖词典，召回更稳**
- 零额外依赖、零 API 成本、索引常驻内存，**单机毫秒级响应**
- 检索器是 `Retriever` 抽象，`search()` 接口与实现解耦，后续要换向量检索只需替换实现

**索引结构**（numpy 紧凑存储，比 Python dict 省 5–10 倍内存）：

```
indptr   : int64  [n_terms+1]   词表 → postings 切片位置
indices  : int32  [nnz]         命中的文档下标（升序）
tfreq    : int16  [nnz]         词频
idf      : float32[n_terms]     BM25 概率型 IDF
doc_len  : int32  [n_docs]      文档 token 数
doc_cat  : int16  [n_docs]      类目标签（支持按类目过滤）
```

**打分**：标准 BM25（k1=1.5, b=0.75）＋ 命中词种类覆盖度加权（`×(1 + 0.12·min(hit, 8))`），避免被单个高频 bigram 带偏。检索结果之间再做 **bigram Jaccard > 0.72 去重**，避免返回一堆近乎同款。

**参考文案排序**：偏好 55–110 字（电商卖点文案的黄金区间），过短或过长的降权。

> 一个反直觉的坑：**不能按「标题-文案词汇重合度」过滤语料**。数据集里确实有约 5% 的标题-文案错配脏样本，但大量优秀文案本就与标题用词不同（如「蓝牙音箱」配「声学结构设计」），过滤会误杀。正确做法是在排序时**降权**而不是删除。

---

## 四、快速开始

### 0. 环境要求

- Python 3.11+
- 可选：一个 OpenAI 兼容的大模型 API Key（通义 / Kimi / 智谱 / DeepSeek…），或本机 Ollama

```bash
pip install -r requirements.txt
```

### 1. 准备数据

先用原始语料：**<https://tianchi.aliyun.com/dataset/9717>**（天池 · 淘宝商品描述数据集，需登录后下载）。

服务运行需要两个派生数据：`data/corpus.db`（清洗后语料）与 `data/index/`（检索索引）。
它们体积大且可由原始语料重建，**未随仓库提供**。

原始语料是双字段 TSV（`商品标题 \t 描述文案`），一键跑完「清洗 → 建索引」：

```bash
python scripts/prepare_data.py --src <语料.tsv> --out-dir data
```

也可以分步执行（便于调参）：

```bash
# 清洗：去重 / 长度过滤 / 广告清洗 / 类目打标 → data/corpus.db
python scripts/clean_data.py --src <语料.tsv> --out-dir data

# 建索引：字符 bigram + BM25 → data/index/index.npz + vocab.txt
python scripts/build_index.py --db data/corpus.db --out data/index
```

跑完后 `data/` 应长这样：

```
data/
├── corpus.db           SQLite 语料库：titles 表 + descs 表
├── app.db              历史记录（首次启动服务时自动创建）
├── index/
│   ├── index.npz       BM25 倒排索引（字符 bigram，numpy 紧凑存储）
│   ├── vocab.txt       词表
│   ├── index_meta.json 构建元信息
│   ├── cat_names.json  类目名称表
│   └── cat_dist.json   类目分布
└── finetune/
    ├── sft_train.jsonl LoRA 训练集（由 clean_data.py 顺带产出）
    ├── sft_val.jsonl   LoRA 验证集（按标题切分）
    └── README.md
```

> 索引各数组的含义与 dtype 见 [`data/README.md`](data/README.md)。
> 若只跑服务不训练，可以删掉 `data/finetune/`。

### 2. 配置生成通道

```bash
cp .env.example .env    # 然后填入自己的 LLM_API_KEY
```

三种通道，前端自动探测，无需改代码：

| 通道 | 配置 | 说明 |
|---|---|---|
| **A. 自备 API Key** | `LLM_PROVIDER=openai`<br>`LLM_API_KEY=sk-xxx`<br>`LLM_BASE_URL=...`<br>`LLM_MODEL=...` | 开箱可用，换厂商只改这三行 |
| **B. 本地 Ollama** | `LLM_PROVIDER=ollama`<br>`OLLAMA_MODEL=qwen2.5:7b` | 数据不出内网 |
| **C. 平台免密钥模型** | 需平台托管环境注入配置 | 代码路径已预留 |

### 3. 启动

```bash
python launcher.py
```

启动器会自动完成：定位项目根 → 读 `.env` → 自检 `data/`（缺失给中文提示，不抛栈）→ 端口被占用自动顺延 → 起服务 → 打开浏览器。

命令行参数：`--port 9000`、`--host 127.0.0.1`、`--no-browser`。

**不用启动器也可以：**

```bash
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

### 4. 进入页：选择模型通道

打开 <http://localhost:8000> 后会先停在**通道选择页**，三选一：本地 Ollama / 模型 API / 平台云服务。点「测试并进入」会真的连一次模型服务，把可用模型列表拉回来填进下拉框。

**Key 的存放规则**：自己填的 Key 只存在本机浏览器 localStorage，每次生成随请求发给本地后端临时使用，**不写入服务端文件**，服务端接口也绝不回传 Key。

---

## 五、目录结构

```
.
├── launcher.py                  一键启动入口
├── requirements.txt
├── .env.example
├── backend/
│   ├── main.py                  FastAPI 入口 + 全部路由
│   ├── config.py                环境变量配置（读项目根 .env）
│   ├── retriever.py             BM25 检索器（字符 bigram）
│   ├── prompts.py               平台风格模板 / 提示词构建 / 输出解析 / 合规检查
│   ├── llm_proxy.py             模型调用（OpenAI 兼容 + Ollama，含流式）
│   ├── store.py                 历史记录 / 收藏（SQLite）
│   └── schemas.py               Pydantic 请求响应模型
├── frontend/
│   ├── index.html               单页界面（含通道选择进入页）
│   ├── styles.css
│   ├── favicon.ico
│   ├── app.js                   界面逻辑 + 进度条
│   ├── gate.js                  进入页：模型通道选择
│   ├── llm.js                   生成通道抽象
│   └── cloud-config.js          云服务公开配置
├── scripts/
│   ├── prepare_data.py          一键跑完「清洗 → 建索引」
│   ├── clean_data.py            数据清洗（顺带产出 finetune/*.jsonl）
│   ├── build_index.py           BM25 索引构建
│   └── finetune_lora.py         LoRA 微调（可选）
└── data/                        运行期数据
    ├── README.md                目录说明 + 索引格式文档
    ├── index/
    │   ├── cat_names.json       类目名称表 ★
    │   ├── index_meta.json      索引构建元信息 ★
    │   └── cat_dist.json        类目分布 ★
    ├── corpus.db                清洗后语料（需自行生成）
    └── index/index.npz          检索索引（需自行生成）
```

> ★ 标记的轻量元数据文件随仓库提供；大体积数据文件未提供，
> 需按「快速开始 · 准备数据」用 `scripts/` 下的脚本自行生成。
> 索引内部格式（各数组含义与 dtype）见 `data/README.md`。

---

## 六、平台风格模板

| id | 平台 | 字数 | 结构 |
|---|---|---|---|
| `taobao` | 淘宝/天猫详情页 | 70–140 | 场景痛点 → 2-3 个具体卖点 → 搭配建议 |
| `jd` | 京东商品卖点 | 70–140 | 整体定位 → 材质/工艺/规格 → 品质承诺 |
| `xhs` | 小红书笔记 | 50–120 | 真实感受开场 → 使用体验细节 → 人群建议 |
| `douyin` | 抖音口播 | 50–110 | 钩子 → 短句连击卖点 → 行动号召 |
| `wechat` | 朋友圈软文 | 40–100 | 生活片段切入 → 自然带出商品 → 轻描淡写收尾 |
| `pdd` | 拼多多卖点 | 40–100 | 划算 → 质量不将就 → 催单 |

文案风格可叠加：`自动匹配 / 种草安利 / 促销带货 / 专业理性 / 文艺质感 / 幽默风趣 / 极简高级`。

---

## 七、API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 索引状态 + 生成通道状态 |
| GET | `/api/channels` | 各通道默认值 + 服务端是否已配 Key（**不回传 Key 本身**） |
| POST | `/api/channels/test` | 通道连通性探测，并返回可用模型列表供下拉 |
| GET | `/api/platforms` `/api/styles` | 平台与风格清单 |
| GET | `/api/categories` | 语料库类目清单 |
| POST | `/api/retrieve` | 只检索：输入标题，返回相似商品及其真实文案 |
| POST | `/api/prepare` | 组装提示词 + 检索结果（供前端直连云端模型） |
| POST | `/api/finalize` | 解析模型输出 + 合规检查 + 落库 |
| POST | `/api/generate` | 一步到位：检索 → 服务端模型 → 解析 → 落库 |
| POST | `/api/generate/stream` | 同上，SSE 推送真实阶段进度（驱动前端进度条） |
| GET | `/api/history` | 历史记录（支持 `favorite_only` / `keyword`） |
| POST | `/api/history/{id}/favorite` | 收藏 / 取消收藏 |
| DELETE | `/api/history/{id}` | 删除记录 |
| GET | `/api/stats` | 语料库与使用统计 |

交互式文档：<http://localhost:8000/docs>

**为什么 `/api/prepare` 和 `/api/finalize` 要拆开？** 因为平台免密钥模型必须由浏览器直连（带 `publishableKey`，服务端做 Origin 校验）。拆开后：检索与提示词组装留在后端，模型调用放浏览器，解析与合规检查再回后端统一处理。前端会自动选路，不需要人工配置。

**进度条是怎么算出来的？** 不是假进度。推理模型的耗时大头在思考阶段，`/api/generate/stream` 把过程拆成四个真实阶段：

| 事件 | 含义 | 进度依据 |
|---|---|---|
| `stage: retrieve` / `stage_done` | 检索完成 | 真实命中数 / 参照条数 |
| `stage: think` + `tick` | 模型思考中 | `reasoning_content` 真实累计字数 |
| `stage: write` + `tick` | 正在撰写文案 | `content` 真实累计字数 |
| `stage: parse` | 解析与合规检查 | |
| `done` / `error` | 结果 / 失败原因 | 与 `/api/generate` 返回结构一致 |

前端把这些计数折算成进度（检索 10% + 生成 80% + 解析 10%），用 rAF 平滑插值，目标值只增不减。流式通道不可用时自动回退到 `/api/generate`。

示例：

```bash
curl -X POST http://localhost:8000/api/generate \
  -H "Content-Type: application/json" \
  -d '{
    "title": "2024春秋新款男士连帽卫衣 宽松潮流纯棉情侣外套",
    "platform": "xhs",
    "style": "zhongcao",
    "n": 3,
    "selling_points": ["纯棉亲肤", "宽松显瘦", "情侣款"]
  }'
```

---

## 八、微调（可选）

数据清洗流程会顺带产出标准 SFT 格式的微调集，并且**按标题做训练/验证切分**
（避免同标题的变体同时出现在两边造成指标虚高）。

输出文件：

| 文件 | 说明 |
|---|---|
| `data/finetune/sft_train.jsonl` | 训练集，Alpaca 格式（`instruction` / `input` / `output`） |
| `data/finetune/sft_val.jsonl` | 验证集，按标题切分，无标题泄漏 |

微调脚本为 `scripts/finetune_lora.py`。训练依赖不在 `requirements.txt` 里（只有运行期依赖），
需单独装：

```bash
pip install torch transformers peft trl datasets accelerate

# 先体检数据，不需要 GPU
python scripts/finetune_lora.py --dry-run

# 1.5B LoRA（8G 显存可跑）
python scripts/finetune_lora.py --model Qwen/Qwen2.5-1.5B-Instruct

# 7B + 4bit 量化
python scripts/finetune_lora.py --model Qwen/Qwen2.5-7B-Instruct --load-4bit

# 合并权重成独立模型
python scripts/finetune_lora.py --merge ./outputs/copy-lora
```

微调后的模型可以直接接进服务：起个 vLLM 暴露 OpenAI 兼容接口，
把 `.env` 的 `LLM_BASE_URL` 指过去即可，**服务端代码无需改动**——
这正是「检索层与生成层解耦」的收益。

---

## 九、常见问题

**Q：`/api/health` 里 `retriever.ready` 是 false？**
A：索引没构建或路径不对。检查 `data/index/` 下是否有 `index.npz` 与 `vocab.txt`。
索引缺失时服务仍能启动，只是没有风格参照。

**Q：提示"未检测到可用的生成通道"？**
A：三条路都断了。检查 `.env` 里 `LLM_API_KEY` 是否填了、`LLM_BASE_URL` 是否可达；Ollama 是否在跑（本机默认 `http://127.0.0.1:11434`）。

**Q：检索结果不相关？**
A：标题太短或太泛（如只写"卫衣"）。标题越具体——品类 + 材质 + 版型 + 人群——召回越准。也可以在请求里传 `cat` 按类目收窄。

**Q：界面提示"模型思考过长导致输出被截断"？**
A：你用的模型带推理能力，它的 `max_tokens` 是**思考 + 正文共享**的预算。把 `.env` 里的 `LLM_MAX_TOKENS` 从 4096 调到 8192，或换一个非推理模型。程序已内置自动放宽重试，但预算给太小仍会失败。

**Q：生成结果只有 1-2 条，不是我要的 3 条？**
A：同上，通常是截断导致。界面会给出黄色警告条说明原因。也可以点某条卡片右上角的「↻ 换一条」单独补齐。

**Q：文案里有绝对化用语？**
A：界面会自动标红提示（`grade: high`）。这些词来自模型，提示词里已明确禁止，但小模型偶尔仍会输出，所以做了后置检查而没有依赖提示词。

**Q：启动时报 `ModuleNotFoundError: No module named 'uvicorn'`？**
A：跑 `launcher.py` 的解释器缺运行依赖。启动器会自动改用项目内 `.venv` 兜底；若项目内没有 `.venv`，按提示执行 `pip install -r requirements.txt`。

**Q：想省磁盘空间？**
A：删掉 `data/finetune/`（只有自己训练模型时才用得到），功能完全不受影响。

---

## 十、技术选型说明

| 决策 | 选择 | 理由 |
|---|---|---|
| 检索 | 字符 bigram BM25 | 中文短标题不依赖分词、零额外依赖、毫秒级 |
| 索引存储 | numpy CSR-like | 比 Python dict 省 5–10 倍内存，加载快 |
| 生成通道 | 三层抽象自动降级 | 平台托管 / 自备 Key / 本地离线，同一套前端代码 |
| 历史存储 | 独立 SQLite | 与只读语料库分离，互不影响，零运维 |
| 前端 | 原生 JS 单页 | 无构建步骤，改完刷新即生效，部署简单 |
| 合规 | 后置正则扫描 | 不依赖模型自觉，风险可见可追溯 |
| 本机出站请求 | 内网地址 `trust_env=False` | 绕过系统代理，避免本机 Ollama 被代理拦成假 502 |


