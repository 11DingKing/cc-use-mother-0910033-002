# 培训补考证书管理

本项目维护培训补考证书管理的领域约定、角色边界与样例数据，并提供零第三方依赖（仅 Python 标准库）的服务端实现，供后端服务、接口和自动化验证统一使用。覆盖文博中心运营员、志愿者、监护人、场馆负责人，并落实领域契约中的四个关键不变量：

1. **单元成绩历史**：成绩只追加，补考产生新记录（`attempt_no` 递增），旧分数、旧考官、旧试卷版本永不被总分覆盖。
2. **补考资格约束**：第二次起记录成绩必须持有补考配额，配额逐次扣减、用尽即拒。
3. **证书证据组合**：发证瞬间把每条单元声明的依据（首次通过 / 补考通过 / 单元替代）、试卷版本、考官、分数、有效期组合成 `evidence_hash`，整证生成 `manifest_hash`；此后成绩再变化也不影响已发证书。
4. **并发唯一发证**：进程写锁 + `BEGIN IMMEDIATE` 事务 + 证书指纹 `UNIQUE(volunteer_id, fingerprint)` + 可选 `Idempotency-Key`，并发请求只产生一张证书，重复请求返回既有证书（`duplicate: true`）。

此外，成绩撤销、补考授权、单元替代、发证、证书暂停/恢复全部追加到 **SHA-256 不可变事件链**（`prev_hash` 串联，序号、类型、载荷、时间戳全部入哈希），可通过 `GET /api/chain` 重放校验；验证证书时撤销状态以链为准，并复核每条声明的证据哈希。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/training_cert/`：服务端实现。
  - `store.py`：SQLite 表结构与线程安全连接。
  - `events.py`：不可变链追加与重放校验。
  - `hashing.py`：规范 JSON / 证据哈希 / 日期工具。
  - `service.py`：领域规则（成绩、补考、替代、发证、暂停、验证）。
  - `app.py`：HTTP 接口（`http.server`，无第三方依赖）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约、领域服务（15 例）与 HTTP 端到端（含 8/10 线程并发发证）回归测试。

## 运行

```bash
PYTHONPATH=src python3 -m training_cert.app --host 127.0.0.1 --port 8080 --db training_cert.db
# 亦可通过 CERT_HOST / CERT_PORT / CERT_DB 环境变量配置
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/papers` | 登记试卷版本（主题、单元、版本号、及格线、发布日） |
| GET | `/api/papers?topic_code=` | 列出试卷版本 |
| POST | `/api/scores` | 记录单元成绩（只追加；第 2 次起需补考配额） |
| GET | `/api/scores?volunteer_id=&topic_code=&unit_code=` | 单元成绩完整历史 |
| POST | `/api/scores/<id>/revoke` | 撤销成绩（带生效日，不可重复撤销） |
| POST | `/api/retake-eligibility` | 授予补考资格（额外次数，配额累加不覆盖） |
| POST | `/api/substitutions` | 定义单元替代（带生效日，按发证日判定） |
| POST | `/api/certificates` | 发证，固定证据组合（支持 `Idempotency-Key` 头） |
| GET | `/api/certificates/<id>` | 查看证书与固定证据 |
| GET | `/api/certificates/<id>/verify?on_date=&topic_code=` | 验证指定日期、主题上的有效范围 |
| POST | `/api/certificates/<id>/suspend` | 暂停证书（形成链上暂停区间） |
| POST | `/api/certificates/<id>/resume` | 恢复证书 |
| GET | `/api/events` | 浏览不可变链事件 |
| GET | `/api/chain` | 链完整性校验报告 |

验证接口返回 `overall_valid`、逐单元 `valid/reasons`（未生效、过期、暂停期、证据成绩已撤销、证据哈希不匹配等）、`suspension_intervals` 与当日仍有效的 `valid_scope.units`。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

