# SagaSmith Core

[中文](README.md) · [English](README-en.md) · [官网](https://sagasmithai.github.io) · [平台总览](https://github.com/SagaSmithAI/.github/blob/main/profile/README.md) · [托管服务](https://github.com/SagaSmithAI/SagaSmith-service) · [内容目录](https://github.com/SagaSmithAI/SagaSmith-dnd-content-library)

**AI 原生 TTRPG 平台的系统无关运行时。** `sagasmith-core` 为规则系统、MCP 服务和 UI 提供持久化战役、角色知识、分支时间线、内容导入、规则包与检索能力；它本身不包含 D&D 或 CoC 规则。

> 世界状态应当可验证，时间线应当可分支，每个角色只应知道自己真正知道的事。

## 它解决什么

普通聊天记忆无法充当长期战役数据库：它不知道哪条时间线有效，也无法可靠区分 GM、玩家、PC 和 NPC 的视角。SagaSmith Core 把这些问题建模为显式服务：

- **战役与角色** — system-neutral campaign/character 模型、namespaced sheet、revision 和访问控制。
- **分支与 Snapshot** — 不可变 Snapshot DAG、checkout、lineage、分支连续性和完整性校验。
- **Actor Knowledge** — 按 actor、主体、分支和可见范围维护所知事实，不把角色知识混入全局摘要。
- **事件与长期记忆** — 事件日志、事实身份、分支修订、continuity context 与 recap 数据面。
- **规则包** — core/extension 包、profile 锁定、版本与来源、规则 receipt 和机械 IR。
- **内容导入** — 可恢复 import job、内容寻址的标准化/页面缓存、PDFium 文本提取、选择性 OCR 质量门禁与页码索引。
- **统一内容包** — core rules、addon、module、preset 共用 v2 归档骨架，携带 v3 PC/NPC/怪物卡、标准化来源、内容寻址资产、严格校验及模块 Agent 定稿记录。
- **检索** — 精确与词法检索、SQLite FTS5，以及可选的 ChromaDB + sentence-transformers。
- **插件系统** — 通过 `sagasmith.systems` entry point 注册 D&D、CoC 或新的系统实现。

## 架构位置

```mermaid
flowchart TB
    A[Agent / MCP Host] --> M[System MCP Server]
    M --> R[System Runtime<br/>D&D · CoC · custom]
    R --> C[SagaSmith Core]
    C --> D[(SQLite / PostgreSQL)]
    C --> F[FTS5]
    C -. optional .-> V[ChromaDB / embeddings]
```

Core 不负责主持风格、MCP 工具暴露或具体规则裁决。Agent Skills 负责工作流，系统运行时负责规则，MCP 服务负责能力与存储边界，Core 负责一致的数据语义。

## 当前领域实现

| 领域 | 当前仓库 | 同仓版本化组件 |
|---|---|---|
| D&D 5e | [`sagasmith-dnd`](https://github.com/SagaSmithAI/sagasmith-dnd) | Domain、MCP、Skills、UI、模组生成流程 |
| Call of Cthulhu 7e | [`sagasmith-coc`](https://github.com/SagaSmithAI/sagasmith-coc) | Domain、MCP、Skills、UI、模组生成流程 |
| Narrative | [`sagasmith-narrative`](https://github.com/SagaSmithAI/sagasmith-narrative) | Domain、MCP、Skills、项目生成流程 |

以上三个垂直仓库是当前唯一源码入口。原独立 MCP、Skills、UI 与通用 Module
Generator 仓库已归档，只保留只读历史；新集成不得依赖其分支、发布或文档。

## 2026-08-20 集成基线

当前主线已经用最新 Agent、Service 与三个领域仓库完成真实 Host 回归：Service
签发 `sagasmith.auth-context/v1` principal context，Agent 将其交给会话作用域 MCP，
领域服务仍在调用边界重新校验 campaign、actor、role 与 revision。D&D 与 CoC
参考战役已在同一托管栈中并发运行且未发现回归缺口；D&D 路径额外记录了一个合法
结局。内容目录 runner 会发现当前全部模组并保存机器可读 exclusion，而不会把未执行
路径伪装成已覆盖。

这条证据不改变 Core 的职责：签名身份、动态工具暴露与主持语义仍分别属于
Service/Agent、MCP 和 Agent Skills；Core 只提供系统无关的持久化与事务保证。

## 可分享内容格式

`sagasmith.content-package` v2 是唯一公开交换格式，文件扩展名为
`.sagasmith-pack`。`addon`、`module`、`preset`、`core_rules` 共用同一归档骨架：
校验和锁定的 manifest、结构化内容、actor cards、来源索引，以及
`blobs/sha256/` 中的原始文档、标准化全文和图片。
完整的 archive、证据、角色图与 kind 语义见
[`docs/CONTENT_PACKAGES.md`](docs/CONTENT_PACKAGES.md)。

actor card 使用 `sagasmith.actor-card.v3`，统一表示 PC、NPC 与怪物。每张卡可引用
一张带媒体类型、许可、署名及来源证据的角色图；图片留在卡和内容包中，不复制到
运行时角色或 Snapshot。导入角色始终产生新的本地 identity，且绝不携带 campaign、
revision、权限、ActorKnowledge、随机流或进度。

Core 校验统一归档并通过公开服务重建来源、角色与模组结构；系统插件继续校验
sheet、edition、规则依赖和具体语义，应用/MCP 负责权限与导入根目录。规则包由
`RulePackService` 完成 draft、不可变存储和 campaign activation 生命周期。旧 portable、
release manifest 与 `.sagasmith-module` 不是公开兼容协议。

## 核心领域

| 领域 | 主要服务 | 关键保证 |
|---|---|---|
| Campaign | `CampaignService`, `AccessService` | system_id 分区、principal/role 访问边界 |
| Character | `CharacterService`, `StateMutationService` | revisioned sheet、受控状态写入、actor-card 导入/导出 |
| Knowledge | `ActorKnowledgeService` | actor 视角隔离、分支有效性 |
| Timeline | `SnapshotService`, `BranchService`, `ContinuityService` | DAG 祖先链、checkout、连续性上下文 |
| Content | `ImportJobService`, `ModuleService`, `PdfDocumentConverter` | 可恢复导入、来源、结构、统一 content package |
| Rules | `RuleService`, `RulePackService`, `RuleProfileService`, `RuleReceiptService` | 内容包来源、规则包版本、精确依赖、激活上下文和结算证据 |
| Retrieval | `RuleService`, `VectorStore` | 检索可降级，权威状态不交给向量库 |

## 安装

Python 3.11+：

```bash
pip install sagasmith-core
```

系统插件通常会自动安装 Core。按需启用 extras：

```bash
pip install "sagasmith-core[documents]"  # PDF
pip install "sagasmith-core[documents,ocr]"  # 扫描版或乱码 PDF 恢复
pip install "sagasmith-core[vector]"     # ChromaDB
pip install "sagasmith-core[embedding]"  # sentence-transformers
pip install "sagasmith-core[all]"
```

本地常驻运行时可为每个领域前缀配置持久 embedding cache。例如 D&D 使用：

```bash
export DND5E_EMBEDDING_CACHE_DIR="/absolute/private/user-cache/sagasmith/dnd5e"
```

请选择源码 checkout 之外、仅当前用户可读写的 OS 应用缓存目录。`BgeEmbedder` 会先查进程内
LRU，再查其中的 SQLite cache；cache identity 绑定固定模型 revision、profile、维度、推理
epoch 与原始文本 digest，向量按 float32 保存且带完整性校验。内置 BGE profile 已固定到
Hugging Face commit；所有自定义 profile 也必须提供 40 位 commit SHA 形式的不可变
`model_revision`，避免 SQL、Chroma 或 cache 在移动 ref 后复用旧向量。损坏记录会被当作
miss 并在下一次推理后替换；锁竞争经过一次有界 cache 尝试后降级为普通推理，默认等待
50 ms。POSIX 上新建 cache 目录和数据库会自动收紧为仅当前用户可访问。

默认硬上限为 50,000 项和 256 MiB 逻辑数据，写入时按最早写入顺序淘汰。可使用
`<PREFIX>_EMBEDDING_CACHE_MAX_ENTRIES`、`<PREFIX>_EMBEDDING_CACHE_MAX_BYTES`、
`<PREFIX>_EMBEDDING_CACHE_BUSY_TIMEOUT_MS` 与 `<PREFIX>_EMBEDDING_CACHE_EPOCH` 调整，
并通过 `embedder.persistent_cache_stats()` 查看当前用量。CoC 等运行时使用自己的 prefix，
不得让不同用户或 tenant 共用同一目录。

最小服务构造：

```python
from sagasmith_core import CampaignService, Database, SystemRegistry

db = Database("sqlite:///sagasmith.db")
db.upgrade_schema()
systems = SystemRegistry.discover()
campaigns = CampaignService(db)
```

## 扩展一个新规则系统

系统包通过 entry point 注册：

```toml
[project.entry-points."sagasmith.systems"]
my_system = "my_package.system:get_system"
```

系统实现提供 profile、角色 schema、模块解析与规则引擎；Core 表保持系统无关。需要新的系统字段时，优先使用 namespaced JSON 或系统包自己的明确扩展表，不向通用表塞入某一规则专属列。

## 稳定性与安全边界

- Snapshot、branch 和 revision 是权威连续性；向量命中不是。
- 客观事实使用稳定 `fact_key`，在分支内通过 revision head 演进；修订应携带
  `expected_revision_id`。角色的主观知识继续使用独立的 ActorKnowledge ledger。
- 场景收尾优先使用 `ContinuityCommitService`，在同一事务中写入事件、事实、
  角色认知和可选 Snapshot，避免产生半保存状态。
- Snapshot 在语义上是可独立恢复的全量 checkpoint；`recap` 才是相对父节点的差量摘要。完整性校验同时覆盖 payload、DAG 祖先链以及 fact/event/actor-knowledge bindings。
- checkout 不会静默丢弃工作区：当前分支有未保存变化时，必须先创建 Snapshot。
- 写操作应携带 expected revision 与幂等键，避免 Agent 重试造成重复副作用。
- 玩家读取只允许当前可见分支、场景作用域和角色知识；GM 权限需要显式 principal/role。
- 最终统一 Pack 从不充当存档或权限载体；导入 actor 必须使用新身份，主观知识必须在目标战役中重新获得或合理传递；导入规则 Pack 也不能自动启用。
- 文档解析结果保留来源、页码、质量警告和 parser profile；调用方必须处理缺失的富元数据。
- 持久 embedding cache 只是可重建的性能层，不是权威检索或 campaign 状态；默认不启用，
  配置目录后也不保存原始文本，只保存 model/text digest 和校验过的 float32 向量。但 digest
  可能被低熵文本字典猜测，embedding 本身也是敏感派生数据；目录必须采用私有权限，不得提交到
  Git、备份到公共位置或跨 tenant 共享。
- 这是 Alpha 项目；主线迁移会保留当前已发布 schema 的数据，但不承诺任意旧实验版本或 downgrade 路径。

## 开发

```bash
pip install -e ".[all,dev]"
pytest --cov
ruff check .
```

更多资料：[Architecture](docs/ARCHITECTURE.md) · [Quickstart](docs/QUICKSTART.md) · [Retrieval](docs/RETRIEVAL.md)

## License

Apache-2.0
