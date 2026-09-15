# Execution contracts in 0.3

## Completed audit boundaries

Applications can use `CommandExecutor` with a required `CommandContext` and
`DecisionBase`. The executor validates the live checkout, replays the exact
business response, checks the decision revision, invokes the handler with an
explicit UoW, and saves its response in that same transaction. Authenticate the
principal before entering this persistence boundary. Never include a freshly
issued authorization nonce in the business payload.
Its required `authorize(work, context)` callback rechecks current access before
every lookup, including replay. `NoOp(response)` records an explicit no-change
outcome without story revisions; a no-op handler that writes is rejected and
rolled back. Importing the contracts module does not import SQLAlchemy or concrete
parsers; storage/application implementations remain separate.

`StateMutationService.replace_in_work`, `ActorLifecycleService.create_in_work`,
and `ContinuityCommitService.commit_in_work` compose without creating nested
transactions. Events, facts, actor knowledge, scene progress and snapshots expose
public `*_in_work` methods. Their failures mark the owning UoW rollback-only.
The two old private continuity callbacks remain validated migration bridges for
the currently published Narrative consumer; they cannot accept an unrelated,
closed or foreign-owner session. New code must use the public UoW methods.

`DecisionBase.capture(database, work, campaign_id)` captures campaign revision,
branch and the non-restorable `timeline_epoch`. Durable continuations retain this
value and call `require_current` before resuming. Every restore/checkout advances
the epoch. An old command cannot replay into a new checkout even if its business
key and payload match. Migration `20260915_35` initializes existing epochs to zero.

Migration `20260915_36` adds missing historical foreign keys and enforces non-null
compressed snapshot fields. It preserves FTS triggers and rejects orphaned
references instead of silently deleting history. Tests compare historical upgrades
with current ORM column names, nullability and foreign-key/delete contracts.

### Import and worker contract

All three ingestion paths hand immutable `PreparedImport` values to their SQL
phase. Parsed structures and vectors are detached from mutable parser/embedder
outputs; invalid counts, dimensions and non-finite vectors fail before writing.
Outbox rows bind document content, model identity and an embedding digest. A
changed entity becomes a permanent failure rather than being indexed under an
old identity. Even a crash on the final permitted attempt reaches a terminal
state after lease expiry. Completion counts include only leases finalized by
that worker.

Run a bounded worker batch from a host scheduler:

```sh
python -m sagasmith_core.vector_worker --system-id dnd --collection rules --profile bge_m3 --limit 100
```

Add `--status` to inspect readiness without writes. The service equivalent is
`VectorIndexJobService.status(system_id=..., collection=...)`; pending or failed
indexing is explicit and lexical retrieval remains available. The worker loads
the optional Chroma adapter only for delivery. Configure `CHROMA_DB_URL` or
`CHROMA_DB_PATH` as for the consumer.

### Extension state

Trusted system plugins declare `StateExtension` adapters and supply them through
`Database(state_extensions=...)`. Each adapter owns an ID, system ID and schema
version, plus capture, validate, migrate, clear and restore callbacks. Snapshot
restore validates and migrates all extension payloads before clearing tables,
clears before rebuilding actors, then restores after actors exist. All work uses
one transaction; callback failure rolls back story state, extension tables and
the epoch. Missing adapters and future schemas fail explicitly. Plugins own their
table DDL migrations; Core never guesses an extension schema from live classes.
Extension snapshots must contain story data only, never real-user permissions.

### Compatibility and validation

The release is tested with D&D transaction/random-receipt integration, CoC
continuity/vertical-slice integration and Narrative settlement/recovery/real-stdio
integration. CoC and Narrative checks deliberately install their current source
packages without dependency resolution: their published dependency bounds still
select Core 0.2. Consumers must deliberately opt into `>=0.3.0,<0.4.0`; these
runtime checks do not claim their current package metadata already permits 0.3.
Local wheel checks exercise the installed artifact, not editable source. Model
downloads and a live Chroma service are not prerequisites for the contract tests.

## 审计改造后的接口

应用使用必填的 `CommandContext`、`DecisionBase` 和 `CommandExecutor`，统一校验当前
分支与时间线、精确重放业务响应、校验决策版本、传递显式工作单元并原子保存收据。
身份认证由调用方先完成；新签发的授权 nonce 不属于业务请求身份。
执行器要求提供 `authorize(work, context)`，包括重放在内，每次读取收据前都重新校验
当前权限。`NoOp(response)` 明确记录无变化结果；若 handler 实际写入则拒绝并回滚，
不会伪造故事版本更新。导入 contracts 不会加载 SQLAlchemy 或具体解析器。

状态、角色创建、连续性提交，以及事件、事实、角色知识、场景进度和快照均提供
`*_in_work` 组合接口，内部失败会阻止工作单元提交。为当前 Narrative 消费者保留的
两个旧私有回调只接受经校验的当前工作单元 Session；新代码应使用公开接口。

`DecisionBase` 记录战役版本、分支和不随读档回退的 `timeline_epoch`；持久化 continuation
恢复前必须校验它。每次读档或切换分支推进 epoch，旧时间线的请求不能重放到新时间线。
迁移 35 初始化 epoch；迁移 36 补齐历史外键和压缩快照非空约束，保留 FTS 触发器，
发现孤立引用时明确要求修复。测试核对历史升级和 ORM 的列、非空与外键删除契约。

三条导入路径通过不可变 `PreparedImport` 向短事务交付数据；错误的向量数量、维数或
非有限值在写入前失败。向量任务绑定正文、模型与向量摘要；实体变化进入永久失败。
最后一次尝试中崩溃的任务也会在租约到期后进入终态，完成数量只统计本 worker 成功
结算的租约。上面的 CLI 每次执行一个有界批次，可由 Host 调度；`--status` 只读返回
索引就绪情况，未完成时可使用词法检索。Chroma 仍是可选适配器。

插件通过 `StateExtension` 声明扩展表的捕获、校验、版本迁移、清理和恢复流程，再传入
`Database(state_extensions=...)`。先校验全部扩展数据，再清理、恢复角色和扩展表；任何
回调失败都会回滚故事状态、扩展表和 epoch。缺失插件和未来版本明确拒绝；插件负责
自己的表结构迁移，快照只能包含故事数据，不能恢复真实用户权限。

本地验证覆盖 D&D 事务与随机收据、CoC 连续性与垂直流程，以及 Narrative 结算、恢复和
实际 stdio。CoC/Narrative 当前声明仍限制 Core 0.2，集成测试以不解析依赖的方式安装
其当前源包；实际升级必须显式调整到 `>=0.3.0,<0.4.0`。安装包测试使用 wheel，
不把源码测试冒充发布包验证，也不把契约测试冒充真实模型或 Chroma 服务验收。

## Application ownership

An application owns a `Database.unit_of_work()` around one business command.
Existing `transaction()` services join that unit. A nested failure marks the
unit rollback-only, even if a caller catches the exception. Use
`Database.savepoint(work)` only for an explicitly recoverable local operation.
Closed units and inherited units in another thread or asyncio Task are rejected.
The compatibility session API remains available; it does not weaken these rules.

Use `StateMutationService.commit()` for runtime document replacements. Supply
the campaign decision revision, each character decision revision, an operation,
and an exact idempotency response. `replace()` remains a low-level assembly API;
it rejects an idempotency promise without an audited operation before writing.

`IdempotencyIdentity` makes campaign, branch, timeline epoch, principal and
operation explicit. New receipt rows persist branch ownership. Legacy rows with
no provable branch are not returned by branch receipt recovery. Authorization
nonce refresh is independent of the business idempotency identity.

## Restore and migration

Story snapshots restore sheets and continuity. They do not restore operational
user grants. Campaign and character revisions increase across restore and branch
checkout; they never reuse the historical token. Historical grants remain in the
immutable archive as evidence, not as instructions to authorize a user.

The historical model-based migrations now reference frozen models from the exact
commits which introduced those migrations. The snapshot storage migration verifies
the old JSON checksum before losslessly encoding its payload. It does not relabel
an old payload as a newer story schema. Unsupported story schemas remain historical
data for explicit migration. Do not stamp an old database to head.

Execution-contract migration `20260915_34` adds receipt ownership, vector leases,
and operational nonce claims. Back up a database before upgrading it. Restore a
backup with its matching runtime; dropping execution ownership by downgrade is
intentionally unsupported.

## Retrieval and external work

Rule retrieval obtains bounded exact/FTS/vector candidates before loading result
documents. FTS source constraints apply before Top-K. Vector filtering has a
bounded refill budget. The explicit Python fallback reads at most 1,000 chunks;
use indexed retrieval for larger corpora. Results remain source-bound.

Rule and module ingestion prepare embeddings before entering their write
transaction. Embedding under an already active unit is rejected. SQL content and
outbox jobs commit together. Queries never flush the outbox. Schedule
`VectorIndexJobService.flush()` independently; it claims jobs, performs external
upserts outside SQL transactions, and finalizes only its own lease. Failed jobs
back off; invalid entities and exhausted retries enter `permanent_failure`.
Lease expiry permits recovery after a worker crash. Upsert IDs remain stable.

Hosted adapters can use `DatabaseNonceStore` with `AuthContextNonceGuard` to share
replay protection across processes and restarts. These records are operational and
are never part of story snapshots.

## Public extension boundary

Prefer `sagasmith_core.contracts` and service-specific modules. Root exports remain
lazy compatibility aliases, so importing Core does not load document/embedding
adapters. System definitions declare protocol, implementation and sheet versions,
capabilities and an optional sheet migration. Parser factories use protocols.
Discovery is idempotent for an identical origin/definition; conflicting origins
cannot silently capture a system ID.

## 中文说明

应用通过 `Database.unit_of_work()` 持有一次业务命令。内部事务共享工作单元；
内部失败即使被捕获，也会阻止整体提交。只有明确允许局部失败的操作才使用
`savepoint(work)`。工作单元不能跨线程、跨 asyncio Task 或关闭后复用。

运行时使用 `StateMutationService.commit()`，显式提供战役和角色决策版本、
操作名称和精确幂等收据。底层 `replace()` 保留，但拒绝没有审计操作的幂等承诺。
结构化幂等身份包含战役、分支、时间线、主体和操作；无明确归属的旧收据不会
通过分支恢复接口返回。授权 nonce 与业务幂等键独立。

读档恢复故事与角色状态，保留当前真实用户授权，并推进战役和角色版本。
旧快照中的授权只作为历史证据。迁移引用原始提交的冻结模型；旧快照 JSON
校验后无损压缩，不会冒充新版故事 schema。升级前备份，恢复时匹配原运行时；
禁止通过降级丢弃执行归属，也不要直接 stamp head。

检索先取有界候选，再读取正文；FTS 在截断前过滤来源，向量检索有补充召回预算。
Python 降级最多扫描 1,000 个片段，大库应启用索引。导入在事务前计算 embedding，
内容与 outbox 同步提交。查询不投递索引；独立 worker 使用租约、退避、永久失败
和稳定 upsert ID。`DatabaseNonceStore` 可跨进程和重启保留防重放记录，不随读档回退。

新调用方使用 `contracts` 和服务专用模块。根导出保留惰性兼容。插件声明协议、
实现、角色 schema、能力与迁移；同来源重复发现幂等，不同来源争用 ID 明确失败。
