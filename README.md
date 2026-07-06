# 🏗️ SagaSmith Core

[中文](README.md) | [English](README-en.md)

**系统无关的 TTRPG 应用基础** — 数据库、文档、检索和战役运行时，供 `sagasmith-dnd`、`sagasmith-coc` 等系统插件使用。

> *"一砖一瓦，构筑万桌冒险。"*

`sagasmith-core` 不包含任何 D&D 或 Call of Cthulhu 规则。它提供一组可共享的持久化服务，系统插件在此基础上注册规则和 CLI。你只会通过系统插件间接安装它。

---

## 生态

| 仓库 | 定位 |
|------|------|
| 🏗️ **sagasmith-core**（本仓库） | 通用引擎 — DB、文档、RAG、战役运行时 |
| 🎲 [SagaSmith-agent](https://github.com/dajiaohuang/SagaSmith-agent) | 完整 AI DM 运行时 |
| ⚔️ [sagasmith-dnd](https://github.com/dajiaohuang/sagasmith-dnd) | D&D 5e 系统插件 |
| 🕯️ [sagasmith-coc](https://github.com/dajiaohuang/sagasmith-coc) | CoC 7e 系统插件 |
| 📦 [SagaSmith-dnd-skills](https://github.com/dajiaohuang/SagaSmith-dnd-skills) | D&D Agent Skill 定义 |
| 📦 [SagaSmith-coc-skills](https://github.com/dajiaohuang/SagaSmith-coc-skills) | CoC Agent Skill 定义 |
| ✍️ [SagaSmith-module-gen-skills](https://github.com/dajiaohuang/SagaSmith-module-gen-skills) | 独立冒险模组生成器 |

---

## 功能

- 🏛️ **战役** — 身份、设置、可变状态、system_id 多租户
- 👤 **角色** — 可扩展属性面板，通过 namespaced JSON 扩展
- 📜 **规则文档** — 层次化章节、检索块、BGE-M3 Dense 嵌入
- 📖 **模组管理** — PDF/Markdown 导入、结构感知分块、场景索引
- 🧩 **场景进度** — `party` / `group:<id>` / `player:<id>` 作用域式追踪，支持继承
- 💾 **Snapshot 系统** — 不可变 DAG 存档树、审计版本、分支感知记忆
- 🔍 **检索** — ChromaDB HNSW 向量搜索、词法/全文混合降级
- 🗄️ **数据库** — SQLAlchemy ORM、Alembic 迁移、SQLite/PostgreSQL 双后端
- 🔌 **插件系统** — `sagasmith.systems` 入口点，profile 可插拔

---

## 架构

```
系统插件 (sagasmith-dnd / sagasmith-coc)
        │
        ▼
┌─────────────────────────────────┐
│        sagasmith-core           │
│                                 │
│  Campaigns · Characters · Docs  │
│  Modules · Scenes · Chunks      │
│  Retrieval (Vector + Lexical)   │
│  Snapshots (DAG) · Memory       │
│  SQLAlchemy ORM · Alembic       │
│  System Plugin Protocol         │
└─────────────────────────────────┘
        │
        ├── SQLite / PostgreSQL
        └── ChromaDB (optional)
```

---

## 安装

```bash
pip install sagasmith-core
```

系统插件通常会作为依赖自动安装。Core 没有 Agent 平台依赖。

### 可选 extras

| Extra | 用途 |
|-------|------|
| `vector` | ChromaDB 向量存储 |
| `embedding` | sentence-transformers 嵌入 |
| `documents` | PDF 解析 (pypdf) |
| `all` | 全部 extras |

---

## 稳定性契约

- Core 表是系统无关的，通过 `system_id` 分区。
- 系统插件通过 namespaced JSON 数据或独立命名的扩展表扩展记录。
- 向量和嵌入依赖惰性导入，不强制安装。
- 运行时恰好激活一个系统 profile。
- 这是新项目，不承担旧版数据库的兼容性义务。

---

## 场景元数据所有权

解析后的场景同时包含列支持字段（始终存在）和系统 profile 在解析时填充的 `metadata_json` JSON dict。消费者应将其视为 **尽力而为的丰富信息**，而非保证存在的 schema。

| 字段 | 来源 | 始终存在？ |
|------|------|-----------|
| `scene_type` | `ModuleScene.scene_type` 列 | 是 |
| `headings` | `ModuleScene.headings` 列 | 是 |
| `scene_level`, `line_count`, `subsections`, `tags` | 实现了 `scene_boundaries()` 的 profile | 如果 profile 实现了 |
| `visibility` | profile 元数据，默认 `"keeper"` | 有默认值 |
| `clues`, `checks` | CoC profile (`CocModuleProfile`) | 否 |
| `sanity` | 仅 CoC profile | 否 |
| `transitions`, `node_id` | 仅 CoC solo-scenario 解析 | 否 |

系统插件选择写入哪些字段。不填充某个丰富信息的 profile **不是 bug**——调用方必须检查空列表 / `None`，而不是假定该字段对该系统有意义。

---

## 贡献

```bash
pip install -e ".[all,dev]"
pytest --cov
ruff check .
```

---

## 许可证

MIT
