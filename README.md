# seekdb 文档记忆

给 AI Agent 用的资料检索 Skill：把一个文件夹里的 Markdown / TXT 灌进 seekdb，
之后用一句话提问就能拿回带着「哪份文件、第几行」的原文片段。

## 它解决什么

资料一多，Agent 只能靠 `grep` 猜关键词，或者把整篇文档塞进上下文。
前者遇到同义改写就瞎，后者贵且有上限。这个 Skill 把两种索引都建起来：

- **全文索引**（seekdb `FULLTEXT` + ngram 解析器）：精确词、专有名词、编号一次命中；
- **向量索引**（seekdb `VECTOR` 列 + `COSINE_DISTANCE`）：换一种说法也能召回；
- **RRF 融合**：两路排名倒数相加，不比较分数尺度，谁靠前谁说话；
- **出处**：每段都带 `path:line`，可以直接引用，也能回原文核对。

## 安装

```bash
pip install -r scripts/requirements.txt
python scripts/docmem.py init
python scripts/docmem.py ingest ~/notes
python scripts/docmem.py search "上次那个限流方案怎么定的"
```

seekdb 与 embedding 服务怎么起，见 [references/quickstart.md](references/quickstart.md)。

## 实测

用一套 16 篇中文笔记（77 段）做 12 条提问验证，其中 2 条带目录过滤：

```
top1 11/11 · 无答案留空 1 · 平均 79 ms
```

业务问题全部命中第一条，资料里确实没有的「今天午饭吃什么」返回空结果而不是硬凑答案。
验证集和脚本在 `tests/`，接上自己的库就能复跑。

## 边界

- 只索引 `md / markdown / txt`，PDF 请先转文本。
- 重命名文件会被当成新文档，旧的留存需要自己清理。
- 单机单库，没有做多租户隔离；换 embedding 模型必须 `--force` 重建。
- 与资料完全无关的问题仍可能返回语义相近的片段，判断依据是 `dist`（越接近 1 越勉强）；
  `--json` 会把这些字段带出来，回答时据此决定是否说「没找到」。
