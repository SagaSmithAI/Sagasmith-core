# Execution contracts in 0.3

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
