# 快速开始

## 1. seekdb

任何监听 2881 的 seekdb / OceanBase 都可以。默认连接参数：

```
host 127.0.0.1  port 2881  user root@sys  password 空
```

换成别的实例时用环境变量覆盖：

| 变量 | 默认值 |
| --- | --- |
| `DOCMEM_DB_HOST` | `127.0.0.1` |
| `DOCMEM_DB_PORT` | `2881` |
| `DOCMEM_DB_USER` | `root@sys` |
| `DOCMEM_DB_PASSWORD` | 空 |
| `DOCMEM_DB_NAME` | `docmem` |

## 2. Embedding 服务

默认走 Ollama 的 `/api/embed`，模型 `quentinz/bge-small-zh-v1.5`（512 维）：

```bash
ollama pull quentinz/bge-small-zh-v1.5
ollama serve
```

也可以换成任何 OpenAI 兼容的 embeddings 接口，只要把地址指向 `/v1/embeddings`，
脚本会按 `{"input": [...]}` 发送、从 `data[].embedding` 取结果：

```bash
export DOCMEM_EMBED_URL=https://your-endpoint/v1/embeddings
export DOCMEM_EMBED_MODEL=bge-m3
export DOCMEM_EMBED_DIM=1024
```

**维度必须和模型真实输出一致**。改完维度要在新库里重新 `ingest`，
因为建表时 `VECTOR(n)` 已经定死。

## 3. 建库与灌数据

```bash
export DOCMEM_EMBED_DIM=512
python scripts/docmem.py init
python scripts/docmem.py ingest ~/notes
```

`ingest` 按文件内容哈希增量：没改过的文件直接跳过，改过的会删旧片段重灌。
改了切块参数想全量重建时加 `--force`。

## 4. 检索

```bash
python scripts/docmem.py search "上个季度复盘里提到的转化率" --top 3
python scripts/docmem.py search "限流" --dir 后端 --json
```

`--json` 输出 `{question, hits[], candidates, latency_ms}`，`hits` 里每条都有
`path / line_no / heading / body / score`。

## 常见问题

**建完表马上用新连接查不到表**
seekdb 的 DDL 对新连接不是立刻可见，等一两秒或重试一次即可。

**中文整句搜索几乎不命中**
全文索引必须用 ngram 解析器（脚本建表时已带 `WITH PARSER ngram`）。
自己建表时漏掉它，中文就只能按空格切词，基本搜不出东西。

**结果里出现明显不相关的文档**
先看 `vec_rank`：如果它只靠向量进来、`dist` 又接近 1，说明是硬凑的邻居。
脚本默认丢掉「全文没命中且 `dist > 0.55`」的候选，语料差别大时可以调 `VEC_MAX_DIST`。

**向量维度报错**
`DOCMEM_EMBED_DIM` 和模型输出不一致，或者用了旧库。换模型要建新库重灌。

**查询变慢**
`POOL` 是每路召回条数，默认 30。语料上万段时先看 `stats` 里的平均耗时再调。
