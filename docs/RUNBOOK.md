# 运行手册 subbill

## 启动

```bash
docker compose config --quiet
docker compose up -d --build
curl -s http://127.0.0.1:18208/api/health
```

宿主机端口 18208 → 容器 8000。数据库在命名卷 `subbill-data` 的 `subbill.sqlite`。

## 主流程

```bash
# 1 采信自带脏样例（订阅 + 变更），返回 run_id 与四本账
curl -s -X POST http://127.0.0.1:18208/api/subscriptions/import \
  -H 'Content-Type: application/json' -d '{}'

# 2 周期诊断
curl -s http://127.0.0.1:18208/api/calendar | head -c 400

# 3 隔离区与原因码
curl -s http://127.0.0.1:18208/api/quarantine | head -c 400
curl -s -o /tmp/quarantine.csv http://127.0.0.1:18208/api/quarantine.csv

# 4 折算与变更
curl -s http://127.0.0.1:18208/api/proration | head -c 300
curl -s -X POST http://127.0.0.1:18208/api/changes | head -c 300

# 5 账单与对账
curl -s http://127.0.0.1:18208/api/invoices | head -c 400
curl -s http://127.0.0.1:18208/api/reconcile

# 6 导出
curl -s -o /tmp/invoices.csv http://127.0.0.1:18208/api/export/invoices.csv
curl -s -o /tmp/manifest.json http://127.0.0.1:18208/api/export/manifest.json
```

## 换成自己的数据

样例目录挂载在容器 `/app/sample_data`，采信时传文件名即可（只接受纯文件名）：

```bash
curl -s -X POST http://127.0.0.1:18208/api/subscriptions/import \
  -H 'Content-Type: application/json' \
  -d '{"subscriptions":"subscriptions_gbk.csv","changes":"changes_dirty.csv"}'
```

两份文件的列名会被映射到规范字段：订阅表需要 `订阅号`、`月价(元)`、`期间起`、`期间止`、`状态`；变更表需要 `变更号`、`订阅号`、`生效时间`、`类型`。缺必需列会整体隔离并给出 `R_HEADER_MISSING`。

订阅表还可选带 `计费周期`、`合同金额(元)` 两列：旧文件没有它们是合法的，按整期月价计价；
带上它们则按合同周期折算（详见 `docs/DATA-DICTIONARY.md`）。一次采信可以同时含旧订阅与年付订阅。

## 新旧字段与合同期计价

- 同一份订阅导出里「有合同列」与「没合同列」的行可以并存，系统逐行决定口径，**不要按文件拆批**。
- 年付 12000、账期一个自然月：账单约 920.55，不是 12000。若看到年付账单接近整数万元，
  检查这一行是否被识别成旧口径（订阅清单页「定价」列应为「合同期计价」）。
- 年付/季付缺合同金额会进隔离区（`R_CONTRACT_MISSING`）；月付缺合同额用月价补出（`R_CONTRACT_INFERRED`）。
- 月价×周期与合同金额不一致只留复核标记（`R_CONTRACT_MISMATCH`），仍以合同额计价。
- 导出账单 CSV 末尾带 `合同号/计费周期/合同金额/合同期起止/本期费率` 五列；旧文件这几列留空。

## 重算与幂等

- 同一份输入再采信一次：命中 `batch_key`，直接返回原批次，行数与合计不变，不重复出账。
- 上次采信失败（`failed`）后重试同一份文件：复用同一个 `run_id`，不堆新批次；失败批次在批次清单可见。
- 采信失败时本批派生表被清空，**上一批仍可查**：页面默认显示最近一批 `done`，用 `?run_id=` 可看指定批次。
- 只重算金额：`POST /api/proration`；重算分段与抵扣：`POST /api/changes`。两者都会重新出账并刷新四本账。
- 想强制新建批次：改任一输入文件的内容（`batch_key` 由输入 sha256 决定）。

## 升级旧库

旧库直接启动即可：`Store` 会增量补列（`ALTER TABLE ADD COLUMN`），不改写旧行。
旧批次读出时按 `legacy_monthly` 解释，账单与导出与升级前一致。无需手工迁移；如需干净重建再执行下面的「重建数据库」。


## 重建数据库

```bash
docker compose down -v          # 删掉命名卷，下次启动会重建空库
docker compose up -d --build
```

## 排错

| 现象 | 检查 |
|---|---|
| 首页 200 但表格为空 | 还没有批次，点「采信」；或 `/api/runs` 看是否有 `done` 批次 |
| 采信返回 400 `E_FILE_MISSING` | 文件名不在 `sample_data/`；只接受纯文件名 |
| 采信返回 400 `E_BAD_FILE_NAME` | 传了路径分隔符或空串 |
| 账单为 0 | 期间内全是暂停/试用，或月价为 0；看账单标记 `R_INVOICE_ZERO` |
| 对账不平 | 看 `/api/reconcile` 哪一本账 `status=mismatch`，再看对应明细 |
| 导出 404 `E_NO_RUN` | 还没批次；先采信 |
| 容器内 pytest 失败 | `docker compose exec -T app python -m pytest -q`，先看第一个失败 |

## 测试

```bash
docker compose exec -T app python -m pytest -q
docker compose exec -T app python -m pytest tests/test_invariants.py -q
```

## 日志

```bash
docker compose logs -f app
docker compose exec -T app ls -l /data
```
