# 口腔修复工艺协作基础服务

本项目提供口腔修复训练机构、加工工位、技师与病例资料的统一后台基础能力，负责机构、场所、操作者和领域资料的登记，支持请求幂等、角色权限、SQLite 事务与哈希串联审计。各项资料通过稳定业务键保存，相同请求会返回原回执，不同内容复用编号时返回明确冲突。

在此之上，服务内置**病例工序与材料追溯账本**：从病例接收、设计、加工、试戴到返修，脱敏处方版本、关键尺寸、工序路线、材料批次与工位责任全程入账，质量人员可通过接口追溯任一成品的输入版本、经手人、剩余材料及未决差异。

## 目录

- `src/skills_workspace/`：领域模型、SQLite 存储、权限服务、审计链、追溯账本、HTTP 路由和离线验收；
- `tests/`：核心规则、事务边界、接口路由、账本规则和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 追溯账本规则

- **病例与处方**：病例按场所内脱敏编号登记；处方以不可变版本保存（脱敏内容与关键尺寸分开入列），修订即产生新版本，旧版本置为 `superseded`，相同内容重复登记返回原版本。
- **工序路线**：路线绑定创建时的有效处方版本，由有序工序组成，每个工序记录工位与责任人；路线定义相同（同病例、同处方版本、同工序序列、同返修来源）时重复创建返回原路线。
- **交接门控**：交出方在工序完成后发起交接，接收方（下一工序责任人）确认或提出差异；差异结案后交接回到待确认状态。任何工序只能在前序交接已确认后开始。
- **数量守恒**：材料批次可拆分为子批次、可领用消耗，数量按定点小数精确记账，任意批次满足 `初始 = 剩余 + 拆出 + 消耗`；`GET /materials/trace` 实时校验并返回 `conserved`。
- **返修分支**：返修必须引用同病例的既有成品并填写原因，生成新的分支路线；原成品与既有加工记录只读不改写。
- **处方修订**：修订使尚未开始的路线进入 `reevaluated` 并禁止其开工；在制路线与已完成工序记录不受影响。
- **稳定结果**：所有写接口按 `request_id` 幂等，重放返回原响应体；重复交接、重复拆分按业务键去重返回原结果；并发领料由串行写事务裁决，余量不足者得到确定的冲突；交接确认后再提差异、工序完成后再开工等终态迟到消息返回确定的冲突或原状态。
- **质量追溯**：`admin`、`reviewer`、`auditor` 角色可查询成品追溯（输入版本、经手人、剩余材料、未决差异、返修分支）、病例汇总、批次拆分树与场所差异列表。

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

命令会在临时 SQLite 数据库中登记机构、操作者、场所和领域资料，并执行一条完整账本链路（病例→处方→路线→工序→交接→差异→材料拆分与消耗→成品→返修→处方修订→质量追溯），核对幂等回执、数量守恒与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m skills_workspace.api --database skills_workspace.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者，成功创建返回 `201`，幂等重放返回 `200` 与原始响应体。服务重启后，SQLite 中的业务状态和审计链继续保留。

### 基础登记

- `POST /organizations`、`POST /actors`、`POST /sites`、`POST /domain-records`
- `GET /domain-records?site_id=&category=`、`GET /audit-events?after_sequence=`

### 追溯账本

- `POST /cases`：病例接收（`site_id`、`external_key`、可选 `note`）
- `POST /prescription-versions`：脱敏处方版本（`case_id`、`content`、`dimensions`）
- `POST /routes`：工序路线（`case_id`、`steps`，返修时附 `rework_of_product_id`、`rework_reason`）
- `POST /steps/start`、`POST /steps/complete`：工序开工与完工（完工记录 `output` 产出哈希）
- `POST /handovers`、`POST /handovers/confirm`：发起与确认交接（`version_ref` 为交接的前序版本快照）
- `POST /handovers/discrepancies`、`POST /discrepancies/resolve`：提出与结案交接差异
- `POST /material-batches`、`POST /material-batches/split`、`POST /material-consumptions`：批次登记、拆分与领料消耗
- `GET /products/trace?product_id=`：成品追溯（输入版本、经手人、剩余材料、未决差异、返修分支）
- `GET /cases/trace?case_id=`：病例汇总（处方版本、路线、成品、材料、未决差异）
- `GET /materials/trace?batch_id=`：批次拆分树、消耗记录与守恒校验
- `GET /discrepancies?site_id=&status=`：场所交接差异列表（`open` 或 `resolved`）
