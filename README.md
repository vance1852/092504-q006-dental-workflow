# 口腔修复工艺协作基础服务

本项目提供口腔修复训练机构、加工工位、技师与病例资料的统一后台基础能力，负责机构、场所、操作者和领域资料的登记，支持请求幂等、角色权限、SQLite 事务与哈希串联审计。各项资料通过稳定业务键保存，相同请求会返回原回执，不同内容复用编号时返回明确冲突。

在此之上，项目实现**病例工序与材料追溯账本**：保存脱敏处方版本、关键尺寸、工序路线、材料批次与工位责任；交方发起交接后由接收方确认或提出差异，任何工序只能基于已确认的前序版本开始；材料拆分与消耗保持数量守恒；返修不覆盖原成品而建立原因明确的新分支；处方修订只重估尚未开始的路线、不改写既有加工记录；重复交接、并发领料与终态迟到消息均返回稳定结果。

## 目录

- `src/skills_workspace/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - `tracing.py` / `tracing_storage.py` / `tracing_models.py`：追溯账本的服务、表结构与只读视图；
  - `tracing_acceptance.py`：追溯账本的离线端到端验收；
- `tests/`：核心规则、事务边界、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m skills_workspace.acceptance
```

命令会在临时 SQLite 数据库中登记机构、操作者、场所和领域资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m skills_workspace.api --database skills_workspace.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者，支持机构、操作者、场所和领域资料登记，以及审计事件查询。服务重启后，SQLite 中的业务状态和审计链继续保留。

## 病例工序与材料追溯账本

所有写接口都要求 `X-Actor-Id` 与幂等键 `request_id`；重复请求返回原回执（`replayed=true`），同一 `request_id` 携带不同内容返回 `409`。

| 接口 | 说明 |
| --- | --- |
| `POST /cases` | 建病例：脱敏处方（v1）、工序路线、初始分支与在制成品 |
| `POST /cases/{id}/prescription-revisions` | 处方修订：仅重估未开始工序并作废旧版未确认交接 |
| `POST /operations/start` | 工序启动闸门：前序已确认、无未决差异才允许开始 |
| `POST /handoffs` | 交方发起交接（可带 `message_id` 去重迟到/重复消息） |
| `POST /handoffs/{id}/respond` | 接收方 `confirm` 或 `dispute`（附原因与期望/实测值） |
| `POST /discrepancies/{id}/resolve` | 质量人员处理差异 |
| `POST /material-lots` / `POST /materials/split` | 登记批次 / 拆分子批次 |
| `POST /material-consumptions` | 工序领料并条件扣减库存 |
| `POST /products/{id}/finish` | 全部工序完成后完工 |
| `POST /cases/{id}/rework` | 返修：原成品保留，建立带原因的新分支、新产品 |
| `POST /cases/{id}/close` | 无未决差异且有完工成品时关闭病例 |
| `GET /cases/{id}` | 完整账本：处方版本、路线、分支、成品、交接、差异、材料 |
| `GET /products/{id}/trace` | 成品追溯：输入版本、经手人、血缘、剩余材料、未决差异 |
| `GET /material-lots/{id}/balance` | 批次守恒核对（期初 = 剩余 + 拆出 + 消耗） |

关键不变量：

- **启动闸门**：工序只能在前序交接处于 `confirmed/closed`、且无 `open` 差异时开始；开始后前序交接置为 `closed`。
- **数量守恒**：数量用定点十进制（4 位小数）保存，拆分校验父批次余量，领料在锁内做条件扣减，并发超额请求恰好一方成功。
- **返修分支**：返修新建 `case_branches` 与 `products`（父子血缘），默认不改动原成品，仅在显式 `scrap_source` 时标记报废。
- **修订不覆盖历史**：修订只把 `pending` 工序换版、把旧版未确认交接置为 `superseded`；`active/done` 记录保持原样。
- **稳定终态**：重复交接返回 `409`，终态上的迟到消息不产生副作用；携带相同 `message_id` 或 `request_id` 的重放返回同一结果。
- **质量追溯**：`reviewer`/`auditor` 等可读取任一成品的输入版本、各工位经手人、批次余量与未决差异。

追溯账本的离线验收：

```bash
PYTHONPATH=src python3 -m skills_workspace.tracing_acceptance
```
