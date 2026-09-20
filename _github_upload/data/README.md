# data/ 目录说明

本目录存放**运行期数据**。为保证仓库体积可控，只有轻量元数据文件随仓库提供，
大体积数据文件需自行生成。

## 随仓库提供的文件

| 文件 | 说明 |
|---|---|
| `index/index_meta.json` | 索引构建元信息（标题数、词元数、avgdl、k1/b、构建时间） |
| `index/cat_names.json` | 16 个类目名称表（索引中的 `doc_cat` 按此顺序编码） |
| `index/cat_dist.json` | 各类目标题数分布 |
| `finetune/README.md` | 微调数据集说明与样本格式 |

> 这几个文件是真实运行产物，作用是把数据格式与时序信息说清楚，便于复现。

## 需自行生成的文件

原始语料下载：**<https://tianchi.aliyun.com/dataset/9717>**（天池 · 淘宝商品描述数据集）

| 文件 | 体积量级 | 生成方式 |
|---|---|---|
| `corpus.db` | 数百 MB | `python scripts/clean_data.py --src <语料.tsv> --out-dir data` |
| `index/index.npz` | 数十 MB | `python scripts/build_index.py --db data/corpus.db --out data/index` |
| `index/vocab.txt` | 数 MB | 同上 |
| `finetune/sft_train.jsonl` | 数百 MB | 由 `clean_data.py` 顺带产出 |
| `finetune/sft_val.jsonl` | 数 MB | 由 `clean_data.py` 顺带产出 |
| `app.db` | < 1 MB | 首次启动服务时自动创建（历史记录 / 收藏） |

一键跑完清洗 + 建索引：

```bash
python scripts/prepare_data.py --src <语料.tsv> --out-dir data
```

## 目录结构

```
data/
├── corpus.db              SQLite 语料库：titles 表 + descs 表
├── app.db                 运行期产生的历史记录（自动创建）
├── index/
│   ├── index.npz          BM25 倒排索引（字符 bigram，numpy 紧凑存储）
│   ├── vocab.txt          词表（行号 = 词元 id）
│   ├── index_meta.json    构建元信息
│   ├── cat_names.json     类目名称表
│   └── cat_dist.json      类目分布
└── finetune/
    ├── sft_train.jsonl    LoRA 训练集（Alpaca 格式）
    ├── sft_val.jsonl      LoRA 验证集（按标题切分）
    └── README.md          微调数据集说明
```

## 索引格式细节

`index.npz` 是 numpy 的压缩归档，内含：

| key | shape | dtype | 含义 |
|---|---|---|---|
| `indptr` | `n_terms + 1` | int64 | 倒排表偏移（CSR 风格） |
| `indices` | `nnz` | int32 | 倒排表指向的文档下标 |
| `tfreq` | `nnz` | int16 | 词频 |
| `idf` | `n_terms` | float32 | 逆文档频率 |
| `doc_len` | `n_docs` | int32 | 文档长度（bigram 数） |
| `doc_ids` | `n_docs` | int64 | 文档对应的标题 id（关联 corpus.db） |
| `doc_cat` | `n_docs` | int16 | 类目 id（对应 cat_names.json 下标） |
| `doc_nd` | `n_docs` | int16 | 该标题下的文案条数 |
| `params` | `4` | float64 | `[k1, b, avgdl, n_docs]` |

**注意**：`n_docs` / `avgdl` / `k1` / `b` 都在 `params` 数组里，不在 `.npz` 的独立键中；
类目名称与构建元信息在旁边的 `.json` 文件里，不在归档内。
`backend/retriever.py` 按这个布局读取。

## 为什么这些数据不入库

- 体积：`corpus.db` 数百 MB + `index.npz` 数十 MB + 训练集数百 MB，超出常规仓库容量预期
- 派生性：全部可由原始语料 + 脚本重建，不需要版本管理
- 数据来源：原始语料来自第三方数据集，随仓库分发不合适
