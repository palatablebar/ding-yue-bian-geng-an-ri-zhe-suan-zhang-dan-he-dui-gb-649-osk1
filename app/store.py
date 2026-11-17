"""SQLite 存储：批次、输入摘要、订阅、变更、隔离、分段、抵扣行、账单、对账、清单。

约定：
- 金额列一律 TEXT（Decimal 字符串）加一份「分」整数，不落 float；
- 一个批次的所有派生行在同一个事务里「先删后插」，失败整体回滚，不留半批；
- 页面只读本模块查询结果，不在模板里做业务计算。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    batch_key TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    input_digest TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    counters_json TEXT NOT NULL DEFAULT '{}',
    pricing_model TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_key ON runs(batch_key, status);

CREATE TABLE IF NOT EXISTS inputs (
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    encoding TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    rows INTEGER NOT NULL,
    header_json TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS subscriptions (
    run_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT '',
    price_text TEXT NOT NULL DEFAULT '',
    monthly TEXT NOT NULL DEFAULT '',
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    tz TEXT NOT NULL,
    state TEXT NOT NULL,
    period_days INTEGER NOT NULL DEFAULT 0,
    leap INTEGER NOT NULL DEFAULT 0,
    pricing_model TEXT NOT NULL DEFAULT 'legacy_monthly',
    cycle_months INTEGER NOT NULL DEFAULT 0,
    cycle_start TEXT NOT NULL DEFAULT '',
    cycle_end TEXT NOT NULL DEFAULT '',
    cycle_days INTEGER NOT NULL DEFAULT 0,
    contract_amount TEXT NOT NULL DEFAULT '',
    contract_id TEXT NOT NULL DEFAULT '',
    rate_days INTEGER NOT NULL DEFAULT 0,
    source_file TEXT NOT NULL DEFAULT '',
    row_no INTEGER NOT NULL DEFAULT 0,
    flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS changes (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    change_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    effective_utc TEXT NOT NULL,
    effective_local TEXT NOT NULL,
    local_date TEXT NOT NULL,
    kind TEXT NOT NULL,
    new_plan TEXT NOT NULL DEFAULT '',
    new_price_text TEXT NOT NULL DEFAULT '',
    new_price TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',
    row_no INTEGER NOT NULL DEFAULT 0,
    applied INTEGER NOT NULL DEFAULT 1,
    superseded_by TEXT NOT NULL DEFAULT '',
    flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS quarantine (
    run_id TEXT NOT NULL,
    file TEXT NOT NULL,
    row_no INTEGER NOT NULL,
    code TEXT NOT NULL,
    level TEXT NOT NULL,
    field TEXT NOT NULL DEFAULT '',
    raw_value TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_quarantine_run ON quarantine(run_id, code);

CREATE TABLE IF NOT EXISTS segments (
    run_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    days INTEGER NOT NULL,
    plan TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'active',
    monthly TEXT NOT NULL DEFAULT '',
    pricing_model TEXT NOT NULL DEFAULT 'legacy_monthly',
    contract_id TEXT NOT NULL DEFAULT '',
    contract_amount TEXT NOT NULL DEFAULT '',
    cycle_months INTEGER NOT NULL DEFAULT 0,
    rate_days INTEGER NOT NULL DEFAULT 0,
    amount_exact TEXT NOT NULL DEFAULT '0',
    amount_cents INTEGER NOT NULL DEFAULT 0,
    flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS components (
    run_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    days INTEGER NOT NULL DEFAULT 0,
    monthly TEXT NOT NULL DEFAULT '',
    pricing_model TEXT NOT NULL DEFAULT 'legacy_monthly',
    contract_id TEXT NOT NULL DEFAULT '',
    rate_days INTEGER NOT NULL DEFAULT 0,
    exact_text TEXT NOT NULL DEFAULT '0',
    cents INTEGER NOT NULL DEFAULT 0,
    flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS invoices (
    run_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    period_days INTEGER NOT NULL,
    billed_days INTEGER NOT NULL,
    free_days INTEGER NOT NULL,
    charge_cents INTEGER NOT NULL,
    credit_cents INTEGER NOT NULL,
    total_cents INTEGER NOT NULL,
    exact_total TEXT NOT NULL DEFAULT '0',
    pricing_model TEXT NOT NULL DEFAULT 'legacy_monthly',
    contract_id TEXT NOT NULL DEFAULT '',
    contract_amount TEXT NOT NULL DEFAULT '',
    cycle_months INTEGER NOT NULL DEFAULT 0,
    cycle_start TEXT NOT NULL DEFAULT '',
    cycle_end TEXT NOT NULL DEFAULT '',
    cycle_days INTEGER NOT NULL DEFAULT 0,
    period_rate TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ok',
    flags_json TEXT NOT NULL DEFAULT '[]',
    source_row INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stages (
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    in_count INTEGER NOT NULL DEFAULT 0,
    out_count INTEGER NOT NULL DEFAULT 0,
    quarantine_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'done',
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS totals (
    run_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliations (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ledger TEXT NOT NULL,
    label TEXT NOT NULL,
    left_text TEXT NOT NULL,
    right_text TEXT NOT NULL,
    diff_text TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS manifests (
    run_id TEXT PRIMARY KEY,
    json TEXT NOT NULL
);
"""

# 增量迁移：旧库已有表时 CREATE TABLE IF NOT EXISTS 不会补列，必须显式 ALTER。
# 只加列、不改旧行；旧批次读出时新列取默认值（legacy_monthly），历史结果原样可查。
MIGRATIONS: dict[str, dict[str, str]] = {
    "runs": {
        "pricing_model": "TEXT NOT NULL DEFAULT ''",
    },
    "subscriptions": {
        "pricing_model": "TEXT NOT NULL DEFAULT 'legacy_monthly'",
        "cycle_months": "INTEGER NOT NULL DEFAULT 0",
        "cycle_start": "TEXT NOT NULL DEFAULT ''",
        "cycle_end": "TEXT NOT NULL DEFAULT ''",
        "cycle_days": "INTEGER NOT NULL DEFAULT 0",
        "contract_amount": "TEXT NOT NULL DEFAULT ''",
        "contract_id": "TEXT NOT NULL DEFAULT ''",
        "rate_days": "INTEGER NOT NULL DEFAULT 0",
    },
    "segments": {
        "pricing_model": "TEXT NOT NULL DEFAULT 'legacy_monthly'",
        "contract_id": "TEXT NOT NULL DEFAULT ''",
        "contract_amount": "TEXT NOT NULL DEFAULT ''",
        "cycle_months": "INTEGER NOT NULL DEFAULT 0",
        "rate_days": "INTEGER NOT NULL DEFAULT 0",
    },
    "components": {
        "pricing_model": "TEXT NOT NULL DEFAULT 'legacy_monthly'",
        "contract_id": "TEXT NOT NULL DEFAULT ''",
        "rate_days": "INTEGER NOT NULL DEFAULT 0",
    },
    "invoices": {
        "pricing_model": "TEXT NOT NULL DEFAULT 'legacy_monthly'",
        "contract_id": "TEXT NOT NULL DEFAULT ''",
        "contract_amount": "TEXT NOT NULL DEFAULT ''",
        "cycle_months": "INTEGER NOT NULL DEFAULT 0",
        "cycle_start": "TEXT NOT NULL DEFAULT ''",
        "cycle_end": "TEXT NOT NULL DEFAULT ''",
        "cycle_days": "INTEGER NOT NULL DEFAULT 0",
        "period_rate": "TEXT NOT NULL DEFAULT ''",
    },
}


def _migrate(conn: sqlite3.Connection) -> list[str]:
    """给既有表补列，返回实际新增的列，便于日志与测试核对。"""
    added: list[str] = []
    for table, columns in MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for column, ddl in columns.items():
            if column in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            added.append(f"{table}.{column}")
    return added


def _loads(value: str, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self.migrations: list[str] = []
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self.migrations = _migrate(conn)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 批次 ----------
    def create_run(self, run: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO runs (run_id, batch_key, status, source, rule_version,
                        created_at, finished_at, input_digest, params_json, counters_json,
                        pricing_model, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     input_digest=excluded.input_digest,
                     counters_json=excluded.counters_json,
                     pricing_model=excluded.pricing_model,
                     params_json=excluded.params_json""",
                (run["run_id"], run["batch_key"], run["status"], run["source"],
                 run["rule_version"], run["created_at"], run.get("finished_at"),
                 run.get("input_digest", ""),
                 json.dumps(run.get("params", {}), ensure_ascii=False),
                 json.dumps(run.get("counters", {}), ensure_ascii=False),
                 run.get("pricing_model", ""),
                 run.get("note", "")),
            )

    def update_run(self, run_id: str, **fields) -> None:
        allowed = {"status", "finished_at", "input_digest", "counters_json", "note",
                   "pricing_model", "params_json"}
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in {"counters_json", "params_json"} and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            sets.append(f"{key}=?")
            values.append(value)
        if not sets:
            return
        values.append(run_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE run_id=?", values)

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    def find_run_by_key(self, batch_key: str, status: str = "done") -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE batch_key=? AND status=? ORDER BY created_at LIMIT 1",
                (batch_key, status)).fetchone()
        return self._run_row(row) if row else None

    def find_any_run_by_key(self, batch_key: str) -> dict | None:
        """同输入的任何状态批次：用于「上次跑到一半」时复用同一个 run_id 重来。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE batch_key=? ORDER BY created_at LIMIT 1",
                (batch_key,)).fetchone()
        return self._run_row(row) if row else None

    def latest_run(self, status: str | None = None) -> dict | None:
        with self.connect() as conn:
            if status:
                row = conn.execute(
                    "SELECT * FROM runs WHERE status=? ORDER BY created_at DESC, rowid DESC "
                    "LIMIT 1", (status,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT 1"
                ).fetchone()
        return self._run_row(row) if row else None

    def latest_done_run(self) -> dict | None:
        """最近一批「完成」的批次：失败的 running 批次不会顶掉上一批可查结果。"""
        return self.latest_run("done")

    def list_runs(self, limit: int = 30) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
        return [self._run_row(row) for row in rows]

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["params"] = _loads(data.pop("params_json", "{}"), {})
        data["counters"] = _loads(data.pop("counters_json", "{}"), {})
        return data

    # ---------- 输入摘要 ----------
    def put_inputs(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM inputs WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO inputs (run_id, name, encoding, bytes, sha256, rows,
                        header_json, note) VALUES (?,?,?,?,?,?,?,?)""",
                [(run_id, item["name"], item["encoding"], int(item["bytes"]),
                  item["sha256"], int(item["rows"]),
                  json.dumps(item.get("header", []), ensure_ascii=False),
                  item.get("note", "")) for item in items],
            )

    def list_inputs(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM inputs WHERE run_id=? ORDER BY name", (run_id,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["header"] = _loads(data.pop("header_json", "[]"), [])
            out.append(data)
        return out

    # ---------- 订阅 ----------
    def put_subscriptions(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM subscriptions WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO subscriptions (run_id, sub_id, account, plan, price_text,
                        monthly, period_start, period_end, tz, state, period_days, leap,
                        pricing_model, cycle_months, cycle_start, cycle_end, cycle_days,
                        contract_amount, contract_id, rate_days,
                        source_file, row_no, flags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, item["sub_id"], item["account"], item["plan"],
                  item["price_text"], str(item["monthly"]), item["period_start"],
                  item["period_end"], item["tz"], item["state"], int(item["period_days"]),
                  int(item["leap"]),
                  item.get("pricing_model", "legacy_monthly"),
                  int(item.get("cycle_months", 0)), item.get("cycle_start", ""),
                  item.get("cycle_end", ""), int(item.get("cycle_days", 0)),
                  str(item.get("contract_amount", "")), item.get("contract_id", ""),
                  int(item.get("rate_days", item["period_days"])),
                  item.get("source_file", ""), int(item.get("row_no", 0)),
                  json.dumps(item.get("flags", []), ensure_ascii=False)) for item in items],
            )

    def list_subscriptions(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE run_id=? ORDER BY sub_id", (run_id,)
            ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            out.append(data)
        return out

    # ---------- 变更 ----------
    def put_changes(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM changes WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO changes (run_id, seq, change_id, sub_id, effective_utc,
                        effective_local, local_date, kind, new_plan, new_price_text,
                        new_price, source_file, row_no, applied, superseded_by, flags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, int(item["seq"]), item["change_id"], item["sub_id"],
                  item["effective_utc"], item["effective_local"], item["local_date"],
                  item["kind"], item.get("new_plan", ""), item.get("new_price_text", ""),
                  str(item.get("new_price", "")), item.get("source_file", ""),
                  int(item.get("row_no", 0)), int(item.get("applied", 1)),
                  item.get("superseded_by", ""),
                  json.dumps(item.get("flags", []), ensure_ascii=False)) for item in items],
            )

    def list_changes(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM changes WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            data["applied"] = bool(data["applied"])
            out.append(data)
        return out

    # ---------- 隔离 ----------
    def put_quarantine(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM quarantine WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO quarantine (run_id, file, row_no, code, level, field,
                        raw_value, detail) VALUES (?,?,?,?,?,?,?,?)""",
                [(run_id, item["file"], int(item["row_no"]), item["code"], item["level"],
                  item.get("field", ""), item.get("raw_value", ""),
                  item.get("detail", "")) for item in items],
            )

    def list_quarantine(self, run_id: str, limit: int | None = None) -> list[dict]:
        sql = ("SELECT * FROM quarantine WHERE run_id=? ORDER BY file, row_no, code")
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self.connect() as conn:
            rows = conn.execute(sql, (run_id,)).fetchall()
        return [dict(row) for row in rows]

    # ---------- 分段 ----------
    def put_segments(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM segments WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO segments (run_id, invoice_id, sub_id, seq, start_date,
                        end_date, days, plan, state, monthly, pricing_model, contract_id,
                        contract_amount, cycle_months, rate_days, amount_exact,
                        amount_cents, flags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, item["invoice_id"], item["sub_id"], int(item["seq"]),
                  item["start_date"], item["end_date"], int(item["days"]), item["plan"],
                  item["state"], str(item.get("monthly", "")),
                  item.get("pricing_model", "legacy_monthly"), item.get("contract_id", ""),
                  str(item.get("contract_amount", "")), int(item.get("cycle_months", 0)),
                  int(item.get("rate_days", 0)),
                  str(item.get("amount_exact", "0")), int(item.get("amount_cents", 0)),
                  json.dumps(item.get("flags", []), ensure_ascii=False)) for item in items],
            )

    def list_segments(self, run_id: str, invoice_id: str | None = None) -> list[dict]:
        with self.connect() as conn:
            if invoice_id:
                rows = conn.execute(
                    "SELECT * FROM segments WHERE run_id=? AND invoice_id=? "
                    "ORDER BY invoice_id, seq", (run_id, invoice_id)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM segments WHERE run_id=? ORDER BY invoice_id, seq",
                    (run_id,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            out.append(data)
        return out

    def update_segment_amounts(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.executemany(
                "UPDATE segments SET amount_exact=?, amount_cents=? "
                "WHERE run_id=? AND invoice_id=? AND seq=?",
                [(str(item["amount_exact"]), int(item["amount_cents"]), run_id,
                  item["invoice_id"], int(item["seq"])) for item in items],
            )

    # ---------- 抵扣行 ----------
    def put_components(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM components WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO components (run_id, invoice_id, sub_id, seq, kind, source,
                        label, days, monthly, pricing_model, contract_id, rate_days,
                        exact_text, cents, flags_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, item["invoice_id"], item["sub_id"], int(item["seq"]),
                  item["kind"], item.get("source", ""), item.get("label", ""),
                  int(item.get("days", 0)), str(item.get("monthly", "")),
                  item.get("pricing_model", "legacy_monthly"), item.get("contract_id", ""),
                  int(item.get("rate_days", 0)),
                  str(item["exact"]), int(item["cents"]),
                  json.dumps(item.get("flags", []), ensure_ascii=False)) for item in items],
            )

    def list_components(self, run_id: str, invoice_id: str | None = None) -> list[dict]:
        with self.connect() as conn:
            if invoice_id:
                rows = conn.execute(
                    "SELECT * FROM components WHERE run_id=? AND invoice_id=? "
                    "ORDER BY invoice_id, seq", (run_id, invoice_id)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM components WHERE run_id=? ORDER BY invoice_id, seq",
                    (run_id,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            out.append(data)
        return out

    # ---------- 账单 ----------
    def put_invoices(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM invoices WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO invoices (run_id, invoice_id, sub_id, account, period_start,
                        period_end, period_days, billed_days, free_days, charge_cents,
                        credit_cents, total_cents, exact_total, pricing_model, contract_id,
                        contract_amount, cycle_months, cycle_start, cycle_end, cycle_days,
                        period_rate, status, flags_json, source_row)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, item["invoice_id"], item["sub_id"], item.get("account", ""),
                  item["period_start"], item["period_end"], int(item["period_days"]),
                  int(item["billed_days"]), int(item["free_days"]),
                  int(item["charge_cents"]), int(item["credit_cents"]),
                  int(item["total_cents"]), str(item.get("exact_total", "0")),
                  item.get("pricing_model", "legacy_monthly"), item.get("contract_id", ""),
                  str(item.get("contract_amount", "")), int(item.get("cycle_months", 0)),
                  item.get("cycle_start", ""), item.get("cycle_end", ""),
                  int(item.get("cycle_days", 0)), str(item.get("period_rate", "")),
                  item.get("status", "ok"),
                  json.dumps(item.get("flags", []), ensure_ascii=False),
                  int(item.get("source_row", 0))) for item in items],
            )

    def list_invoices(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM invoices WHERE run_id=? ORDER BY invoice_id", (run_id,)
            ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["flags"] = _loads(data.pop("flags_json", "[]"), [])
            out.append(data)
        return out

    def get_invoice(self, run_id: str, invoice_id: str) -> dict | None:
        for invoice in self.list_invoices(run_id):
            if invoice["invoice_id"] == invoice_id:
                return invoice
        return None

    # ---------- 阶段 / 合计 / 对账 ----------
    def put_stages(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM stages WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO stages (run_id, stage, in_count, out_count,
                        quarantine_count, status, note) VALUES (?,?,?,?,?,?,?)""",
                [(run_id, item["stage"], int(item["in_count"]), int(item["out_count"]),
                  int(item["quarantine_count"]), item.get("status", "done"),
                  item.get("note", "")) for item in items],
            )

    def list_stages(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM stages WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def put_totals(self, run_id: str, totals: dict) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM totals WHERE run_id=?", (run_id,))
            conn.executemany(
                "INSERT INTO totals (run_id, key, value) VALUES (?,?,?)",
                [(run_id, key, str(value)) for key, value in sorted(totals.items())],
            )

    def list_totals(self, run_id: str) -> dict:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM totals WHERE run_id=? ORDER BY key", (run_id,)
            ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def put_reconciliations(self, run_id: str, items: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM reconciliations WHERE run_id=?", (run_id,))
            conn.executemany(
                """INSERT INTO reconciliations (run_id, seq, ledger, label, left_text,
                        right_text, diff_text, status, note) VALUES (?,?,?,?,?,?,?,?,?)""",
                [(run_id, int(item["seq"]), item["ledger"], item["label"],
                  item["left_text"], item["right_text"], item["diff_text"],
                  item["status"], item.get("note", "")) for item in items],
            )

    def list_reconciliations(self, run_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM reconciliations WHERE run_id=? ORDER BY seq", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 清单 ----------
    def put_manifest(self, run_id: str, data: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO manifests (run_id, json) VALUES (?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET json=excluded.json",
                (run_id, json.dumps(data, ensure_ascii=False)),
            )

    def get_manifest(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT json FROM manifests WHERE run_id=?", (run_id,)).fetchone()
        return _loads(row["json"], {}) if row else None

    # ---------- 整体重算 ----------
    def clear_derived(self, run_id: str) -> None:
        """重算前清派生表；订阅、变更、隔离、输入摘要保留。"""
        with self.connect() as conn:
            for table in ("segments", "components", "invoices", "stages", "totals",
                          "reconciliations"):
                conn.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
