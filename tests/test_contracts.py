"""合同期计价：新旧字段兼容、年付不被放大、期内变更、导出还原同一份合同。

手算基准（2026-02 账期 28 天）：
- 年付 12000，合同期 2026-02-01..2027-02-01 共 365 天 → 12000×28/365 = 920.5479… → 920.55
  朴素「把年价当月价」会出 12000，正好放大 12 倍；合同口径下不会。
- 季付 2400，周期 89 天 → 2400×28/89 = 755.0561… → 755.06
- 月付 1200，周期 28 天 → 整期 1200.00，与旧口径一致。
- 月付 1200 在 2-15 升配到 2400：1200×14/28 + 2400×14/28 = 1800.00。
- 闰年 2028-02 年付 12000，周期 366 天 → 12000×29/366 = 950.8196… → 950.82
"""
from __future__ import annotations

import csv
import io
from dataclasses import replace
from decimal import Decimal

import pytest

from app import config
from app.errors import SubbillError
from app.modules.m1_ingest import service as ingest_service
from app.modules.m4_proration import prorate
from app.modules.m4_proration import pricing as pricing_mod
from app.store import Store


def _write(tmp_path, name: str, text: str) -> None:
    (tmp_path / name).write_text(text, encoding="utf-8")


def _local_settings(tmp_path) -> config.Settings:
    return replace(config.Settings(), db_path=str(tmp_path / "t.sqlite"),
                   sample_dir=str(tmp_path))


def _copy_samples(tmp_path) -> None:
    """把仓库自带的合同样例复制进临时样例目录，走与容器相同的采信路径。"""
    import shutil
    from pathlib import Path

    source = Path(config.Settings().sample_dir)
    for name in ("subscriptions_contracts.csv", "changes_contracts.csv"):
        shutil.copyfile(source / name, tmp_path / name)


SUB_HEAD = "订阅号,账户,套餐,月价(元),计费周期,合同金额(元),期间起,期间止,时区,状态\n"
CHG_HEAD = "变更号,订阅号,生效时间,类型,新套餐,新月价(元)\n"


@pytest.fixture()
def contract_run(tmp_path):
    _copy_samples(tmp_path)
    settings = _local_settings(tmp_path)
    store = Store(settings.db_path)
    data = ingest_service.import_sample(
        store, settings, "subscriptions_contracts.csv", "changes_contracts.csv")
    return store, settings, data


def test_cycle_day_math():
    import datetime as dt

    from app.modules.m2_calendar import days as days_mod

    assert days_mod.cycle_days(dt.date(2026, 2, 1), 1) == 28
    assert days_mod.cycle_days(dt.date(2026, 2, 1), 12) == 365
    assert days_mod.cycle_days(dt.date(2028, 2, 1), 12) == 366
    # 月末夹紧：1-31 加一个月落到 2-28
    assert days_mod.add_months(dt.date(2026, 1, 31), 1) == dt.date(2026, 2, 28)


def test_annual_not_inflated(contract_run):
    store, _settings, data = contract_run
    invoices = {row["sub_id"]: row for row in store.list_invoices(data["run_id"])}
    annual = invoices["SUB-3001"]
    assert annual["pricing_model"] == config.PRICING_CONTRACT
    assert annual["cycle_months"] == 12
    assert annual["cycle_days"] == 365
    # 年付合同 12000 的月度账期：约 1/12，不是 12000
    assert annual["total_cents"] == 92055
    assert annual["total_cents"] < 100000  # 绝不会被放大成十二倍
    # 月付合同 1200 在 2-15 升配到 2400：14 天 1200 + 14 天 2400 = 1800.00
    monthly = invoices["SUB-3002"]
    assert monthly["total_cents"] == 180000
    assert monthly["cycle_days"] == 28


def test_mixed_legacy_and_contract_in_one_batch(contract_run):
    store, _settings, data = contract_run
    subs = {row["sub_id"]: row for row in store.list_subscriptions(data["run_id"])}
    assert subs["SUB-3003"]["pricing_model"] == config.PRICING_LEGACY
    assert subs["SUB-3001"]["pricing_model"] == config.PRICING_CONTRACT
    assert data["counters"]["legacy_subs"] == 1
    assert data["counters"]["contract_subs"] == 6
    # 一次采信里两种口径并存，四本账仍全平
    assert [row["status"] for row in data["ledgers"]] == ["ok"] * 4


def test_contract_prices(contract_run):
    store, _settings, data = contract_run
    invoices = {row["sub_id"]: row for row in store.list_invoices(data["run_id"])}
    assert invoices["SUB-3004"]["total_cents"] == 75506      # 季付 2400/89×28
    assert invoices["SUB-3007"]["cycle_days"] == 366         # 闰年
    assert invoices["SUB-3007"]["total_cents"] == 95082      # 年付 12000×29/366
    # 月价×周期 ≠ 合同额：以合同额计价，只留复核标记
    assert invoices["SUB-3006"]["total_cents"] == 76712      # 10000×28/365
    assert "R_CONTRACT_MISMATCH" in invoices["SUB-3006"]["flags"]


def test_monthly_change_inside_annual_style_cycle(tmp_path):
    _write(tmp_path, "subs.csv", SUB_HEAD
           + "SUB-5001,客户,标准版,1200.00,月付,1200.00,2026-02-01,2026-03-01,+08:00,active\n")
    _write(tmp_path, "changes.csv", CHG_HEAD
           + "CHG-5001,SUB-5001,2026-02-15T00:00:00+08:00,升配,专业版,2400.00\n")
    settings = _local_settings(tmp_path)
    store = Store(settings.db_path)
    data = ingest_service.import_sample(store, settings, "subs.csv", "changes.csv")
    invoice = store.list_invoices(data["run_id"])[0]
    # 14 天 1200 + 14 天 2400 = 1800.00，合同期内变更不改变账期分母
    assert invoice["total_cents"] == 180000
    segments = store.list_segments(data["run_id"])
    assert [row["days"] for row in segments] == [14, 14]
    assert {row["rate_days"] for row in segments} == {28}


def test_contract_missing_and_partial_columns(tmp_path):
    _write(tmp_path, "subs.csv", SUB_HEAD
           + "SUB-5101,年付缺额,企业版,1000.00,年付,,2026-02-01,2026-03-01,+08:00,active\n"
           + "SUB-5102,有额无周期,企业版,1000.00,,12000.00,2026-02-01,2026-03-01,+08:00,active\n"
           + "SUB-5103,周期不可识别,企业版,1000.00,双周,12000.00,2026-02-01,2026-03-01,+08:00,active\n"
           + "SUB-5104,月付缺额可推,标准版,1200.00,月付,,2026-02-01,2026-03-01,+08:00,active\n")
    _write(tmp_path, "changes.csv", CHG_HEAD)
    settings = _local_settings(tmp_path)
    store = Store(settings.db_path)
    data = ingest_service.import_sample(store, settings, "subs.csv", "changes.csv")
    codes = {(row["row_no"], row["code"]) for row in store.list_quarantine(data["run_id"])}
    assert (2, "R_CONTRACT_MISSING") in codes      # 年付缺合同额 → 隔离，不猜
    assert (3, "R_CYCLE_MISSING") in codes         # 有合同额无周期 → 隔离
    assert (4, "R_CYCLE_UNKNOWN") in codes         # 周期名不认识 → 隔离
    subs = {row["sub_id"]: row for row in store.list_subscriptions(data["run_id"])}
    assert "SUB-5101" not in subs and "SUB-5102" not in subs
    assert subs["SUB-5104"]["contract_amount"] == "1200.00"
    assert "R_CONTRACT_INFERRED" in subs["SUB-5104"]["flags"]


def test_legacy_file_has_no_contract_columns(tmp_path):
    # 旧文件（无周期/合同列）结果与 1.0.0 逐字一致，且合同列在导出里留空
    settings = replace(config.Settings(), db_path=str(tmp_path / "t.sqlite"))
    store = Store(settings.db_path)
    data = ingest_service.import_sample(store, settings)
    invoice = {row["invoice_id"]: row
               for row in store.list_invoices(data["run_id"])}["INV-202602-SUB-1001"]
    assert invoice["pricing_model"] == config.PRICING_LEGACY
    assert invoice["total_cents"] == 174286
    assert invoice["contract_amount"] == "1200.00"


def test_export_carries_contract_chain(contract_run):
    from fastapi.testclient import TestClient

    from app.main import create_app

    store, settings, data = contract_run
    with TestClient(create_app(settings)) as client:
        text = client.get("/api/export/invoices.csv").text
        rows = {row["订阅号"]: row
                for row in csv.DictReader(io.StringIO(text.lstrip("﻿")))}
        annual = rows["SUB-3001"]
        assert annual["合同号"] == "CT-SUB-3001-20260201"
        assert annual["计费周期"] == "年付"
        assert annual["合同金额(元)"] == "12000.00"
        assert annual["本期费率(元)"] == "920.55"
        # 旧文件行合同列留空，但账单额与 API 一致
        legacy = rows["SUB-3003"]
        assert legacy["合同号"] == "" and legacy["账单额(元)"] == "600.00"
        detail = client.get("/api/invoices/INV-202602-SUB-3002").json()
        assert detail["invoice"]["contract_id"] == "CT-SUB-3002-20260201"
        assert detail["invoice"]["contract_amount"] == "1200.00"
        # 导出合计 = 账单 API 合计
        api_total = Decimal(client.get("/api/reconcile").json()["totals"]["invoice_cents"])
        csv_total = sum((Decimal(row["账单额(元)"]) * 100 for row in rows.values()),
                        Decimal("0"))
        assert csv_total == api_total


def test_same_file_rerun_does_not_double_bill(contract_run):
    store, settings, data = contract_run
    again = ingest_service.import_sample(
        store, settings, "subscriptions_contracts.csv", "changes_contracts.csv")
    assert again["reused"] is True
    assert again["run_id"] == data["run_id"]
    assert len(store.list_runs()) == 1


def test_failed_batch_keeps_previous_result(tmp_path, monkeypatch):
    settings = replace(config.Settings(), db_path=str(tmp_path / "t.sqlite"))
    store = Store(settings.db_path)
    first = ingest_service.import_sample(store, settings)
    before = dict(store.list_totals(first["run_id"]))

    _write(tmp_path, "subs2.csv", SUB_HEAD
           + "SUB-5201,新客户,标准版,900.00,月付,900.00,2026-02-01,2026-03-01,+08:00,active\n")
    _write(tmp_path, "changes2.csv", CHG_HEAD)
    settings = replace(settings, sample_dir=str(tmp_path))

    original = ingest_service.invoice_service.rebuild

    def explode(*args, **kwargs):
        raise RuntimeError("模拟迁移失败")

    monkeypatch.setattr(ingest_service.invoice_service, "rebuild", explode)
    with pytest.raises(RuntimeError):
        ingest_service.import_sample(store, settings, "subs2.csv", "changes2.csv")
    monkeypatch.setattr(ingest_service.invoice_service, "rebuild", original)

    failed = store.latest_run()
    assert failed["status"] == "failed"
    # 上一批仍然可查，合计不变；默认批次视图仍取上一批 done
    assert dict(store.list_totals(first["run_id"])) == before
    assert store.latest_done_run()["run_id"] == first["run_id"]
    assert store.get_manifest(first["run_id"]) is not None
    assert not store.list_invoices(failed["run_id"])


def test_failed_batch_is_reused_on_retry(tmp_path, monkeypatch):
    settings = replace(config.Settings(), db_path=str(tmp_path / "t.sqlite"),
                       sample_dir=str(tmp_path))
    store = Store(settings.db_path)
    _write(tmp_path, "subs.csv", SUB_HEAD
           + "SUB-5301,客户,标准版,600.00,月付,600.00,2026-02-01,2026-03-01,+08:00,active\n")
    _write(tmp_path, "changes.csv", CHG_HEAD)
    original = ingest_service.invoice_service.rebuild
    monkeypatch.setattr(ingest_service.invoice_service, "rebuild",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        ingest_service.import_sample(store, settings, "subs.csv", "changes.csv")
    failed_id = store.latest_run()["run_id"]
    monkeypatch.setattr(ingest_service.invoice_service, "rebuild", original)
    data = ingest_service.import_sample(store, settings, "subs.csv", "changes.csv")
    assert data["run_id"] == failed_id          # 重试复用同一 run_id，不堆批次
    assert store.get_run(failed_id)["status"] == "done"
    assert len(store.list_runs()) == 1


def test_migration_adds_columns_to_legacy_db(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE subscriptions (run_id TEXT, sub_id TEXT, account TEXT,
               plan TEXT, price_text TEXT, monthly TEXT, period_start TEXT,
               period_end TEXT, tz TEXT, state TEXT, period_days INTEGER,
               leap INTEGER, source_file TEXT, row_no INTEGER, flags_json TEXT);
           CREATE TABLE invoices (run_id TEXT, invoice_id TEXT, sub_id TEXT,
               account TEXT, period_start TEXT, period_end TEXT, period_days INTEGER,
               billed_days INTEGER, free_days INTEGER, charge_cents INTEGER,
               credit_cents INTEGER, total_cents INTEGER, exact_total TEXT,
               status TEXT, flags_json TEXT, source_row INTEGER);""")
    conn.commit()
    conn.close()
    store = Store(str(path))
    assert "subscriptions.pricing_model" in store.migrations
    assert "invoices.contract_amount" in store.migrations
    with store.connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(subscriptions)")}
    assert {"pricing_model", "contract_amount", "rate_days", "cycle_days"} <= columns
    # 再打开一次不重复加列
    assert Store(str(path)).migrations == []


def test_proration_module_math():
    import datetime as dt

    annual = pricing_mod.contract(Decimal("12000"), 12, dt.date(2026, 2, 1), 28)
    assert annual.cycle_days == 365
    assert prorate.quantize_exact(annual.period_rate(28)) == Decimal("920.54794521")
    assert prorate.quantize_exact(annual.period_rate(365)) == Decimal("12000.00000000")
