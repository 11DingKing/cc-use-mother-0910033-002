# 培训补考证书管理

培训补考证书的服务端实现：保存试卷版本、单元成绩、考官、补考资格与证书声明，发证时固定证据组合；成绩撤销、补考次数限制、单元替代与证书暂停全部记录在不可变哈希链上，并提供接口验证证书在指定日期与主题上的有效范围。纯标准库实现，无需安装第三方依赖。

## 架构

- **仅追加事件账本**：所有变更（`ScoreRecorded`、`ScoreRevoked`、`RetakeEligibilityGranted`、`UnitReplaced`、`CertificateIssued`、`CertificateSuspended`、`CertificateResumed`、`CertificateRevoked` 等）只追加、不更新不删除。
- **哈希链**：每条事件的哈希包含前一事件哈希（SHA-256，规范化 JSON 输入），`GET /chain` 可重算全链检测篡改；任何读取都先回放账本，遇到未知事件即 fail-closed。
- **发证固定证据组合**：证书声明逐单元记录 `first_pass / retake_pass / substitution`，连同成绩 ID、试卷版本、考官、考试日期一起哈希（`evidence_hash`），并锚定到链上的 `anchor_seq/anchor_hash`。事后撤销成绩不会改写声明，只在验证时标记证据失效。
- **单元替代**：替代关系带生效日期；生效日前发证仍要求旧单元，旧单元在替代生效日前的通过成绩以 `substitution` 声明计入新单元。
- **并发唯一发证**：所有写操作在 SQLite `BEGIN IMMEDIATE` 事务内完成"重建状态→校验→追加事件"；另有部分唯一索引兜底，同一志愿者同一主题至多一张签发/暂停中的证书，撤销后名额才释放。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/training_certs/`：服务端实现。
  - `events.py`：哈希、规范化编码、日期工具。
  - `store.py`：SQLite 账本 schema 与追加写入。
  - `models.py`：事件回放（reducer）与领域状态。
  - `service.py`：业务规则、事务边界、验证与链自检。
  - `http_api.py` / `__main__.py`：HTTP JSON 接口与启动入口。
- `tools/check_contract.py`：命令行契约摘要检查。
- `tests/`：契约、领域规则、并发唯一性与 HTTP 端到端测试。

## 运行

```bash
PYTHONPATH=src python3 -m training_certs --db training_certs.db --host 127.0.0.1 --port 8000
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/volunteers` | 登记志愿者 |
| POST | `/units` | 登记主题下的考核单元 |
| POST | `/papers` | 登记试卷版本（单元内版本号单调递增，随成绩永久保存） |
| POST | `/retake-eligibility` | 授予补考次数上限（含授权教务员与原因） |
| POST | `/scores` | 记录单元成绩（自动区分首考/补考，记录考官与试卷版本） |
| POST | `/scores/{score_id}/revoke` | 撤销成绩（追加事件，仍占用补考名额） |
| POST | `/unit-replacements` | 定义单元替代（旧单元→新单元、生效日期） |
| POST | `/certificates` | 发证（固定证据组合；并发重复发证返回 409） |
| POST | `/certificates/{cert_no}/suspend` | 暂停证书 |
| POST | `/certificates/{cert_no}/resume` | 恢复证书 |
| POST | `/certificates/{cert_no}/revoke` | 撤销证书（释放发证名额） |
| GET | `/certificates/{cert_no}` | 证书详情（声明、锚点、状态） |
| GET | `/certificates/{cert_no}/verify?date=YYYY-MM-DD[&topic=...]` | 验证指定日期与主题上的有效范围 |
| GET | `/volunteers/{volunteer_id}/history` | 志愿者全部单元成绩历史 |
| GET | `/chain` | 哈希链完整性自检 |

`verify` 返回该日期的生命周期（`not_yet_issued / valid / suspended / expired / revoked`）、每个单元声明的证据状态（`in_scope / suspended / not_valid`）、有效范围 `valid_scope`，以及 `evidence_intact`（成绩被撤销或晚于查询日时为 false）。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
