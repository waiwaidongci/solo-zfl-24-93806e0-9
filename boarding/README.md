# 赛鸽寄养与代训管理服务

仅依赖 Python 3 标准库（`http.server` + `sqlite3`），数据落盘 `data/boarding.db`，重启不丢。

## 运行

```bash
python3 server.py          # 默认端口 3025，可用 PORT / BOARDING_DB 环境变量覆盖
```

## 端到端验证（53 项断言）

```bash
python3 e2e_test.py        # 自动起停服务，覆盖全流程/并发/重启/参数/权限
```

## 角色与令牌（种子数据）

| 角色 | 账号 | 令牌 |
|---|---|---|
| 管理员 | admin | `admin-token` |
| 委托人 | owner1 | `owner1-token` |
| 委托人 | owner2 | `owner2-token` |

请求头：`Authorization: Bearer <token>`

## 接口一览

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| POST | /api/pigeons | owner | 建档（足环号唯一） |
| GET | /api/pigeons | any | 委托人看自己的，管理员看全部 |
| POST | /api/orders | owner | 下单：选寄养期 planDays + 代训课 courseCodes |
| GET | /api/orders | any | 列表，可按 ?status= 过滤 |
| GET | /api/orders/{id} | 本人/admin | 详情+账单（天数×日价+课程+医疗，自动汇总） |
| POST | /api/orders/{id}/check-in | admin | 入舍分棚（容量校验，并发不超卖） |
| POST | /api/orders/{id}/feedings | admin | 每日喂养（每单每天一条） |
| POST | /api/orders/{id}/trainings | admin | 训练登记（隔离期拒绝） |
| POST | /api/orders/{id}/health-events | admin | 健康异常，可转隔离观察 |
| POST | /api/orders/{id}/quarantine/release | admin | 解除隔离 |
| POST | /api/orders/{id}/medical-items | admin | 额外医疗项（计入账单） |
| POST | /api/orders/{id}/payments | owner | 缴费（idempotencyKey 幂等） |
| POST | /api/orders/{id}/check-out | admin | 结算离舍（未结清拒绝，释放棚位） |
| GET | /api/orders/{id}/audit | 本人/admin | 审计流水 |
| GET | /api/courses · /api/lofts | any | 课程价目 / 棚位占用 |

## 关键规则与实现

- **同鸽不同单**：`boarding_orders(pigeon_id)` 部分唯一索引（仅进行中状态），并发下单数据库层兜底，10 并发只有 1 单成功。
- **隔离不训练**：`QUARANTINE` 状态下训练接口返回 409 `quarantine_no_training`。
- **未结清不离舍**：离舍前核对 `paidCents >= totalCents`，否则 409 `unpaid_balance`。
- **费用自动汇总**：寄养费（实际在舍天数×日价，向上取整至少 1 天）+ 课程快照价 + 医疗项。
- **审计**：入舍、状态变化（隔离进出）、缴费、离舍、下单均同事务写 `audit_logs`。
- **事务完整性**：所有多步写入包在 `BEGIN IMMEDIATE` 事务里，任一步失败整体回滚，不留半条数据；缴费按幂等键去重，重试不重复入账。
- **并发安全**：写事务先取写锁；棚位容量在事务内校验，4 并发抢 2 个棚位只有 2 个成功。
- **持久化**：SQLite(WAL) 落盘，重启后订单、账单、审计均可查。
