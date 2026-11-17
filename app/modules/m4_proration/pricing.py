"""定价口径：旧文件按月价整期计价，新文件按合同周期折算。

两种口径并存是硬需求：同一个导出文件里既可能有旧订阅（只有月价），也可能有
月付、季付、年付订阅。逐行决定口径，**不按批次决定**，这样运营一次采信就能出账，
财务也能各按合同期核对。

关键公式（``rate_days`` 是日价分母）：

- 旧文件：``rate_amount = 月价``，``rate_days = 期间天数``；
- 合同文件：``rate_amount = 合同金额``，``rate_days = 合同周期天数``。

于是「一整期金额」= ``rate_amount × 期间天数 / rate_days``：
年付合同 12000、账期一个月时得到 12000 × 28/365 ≈ 920.55，而不是把年价当整期月价
放大十二倍；账期恰好等于合同周期时又精确回到 12000。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from ... import config
from ..m2_calendar import days as days_mod


@dataclass(frozen=True)
class Pricing:
    """一条订阅（或变更后一个段）的定价事实。"""

    model: str
    contract_amount: Decimal     # 完整合同周期的金额；旧文件即月价
    cycle_months: int            # 旧文件为 0
    cycle_start: dt.date | None
    cycle_end: dt.date | None
    cycle_days: int              # 合同周期天数；旧文件等于期间天数
    rate_days: int               # 日价分母
    contract_id: str = ""        # 旧文件为空；合同口径为 CT-<订阅号>-<周期起>
    flags: list[str] = field(default_factory=list)

    @property
    def legacy(self) -> bool:
        return self.model == config.PRICING_LEGACY

    def period_rate(self, period_days: int) -> Decimal:
        """本账期应计金额：合同额 × 账期天数 ÷ 周期天数（旧文件即月价）。"""
        if self.rate_days <= 0 or self.rate_days == period_days:
            return self.contract_amount
        from . import prorate

        return prorate.share(self.contract_amount, period_days, self.rate_days)

    def daily_rate(self) -> Decimal:
        from . import prorate

        return prorate.daily_rate(self.contract_amount, self.rate_days)


def contract_id_for(sub_id: str, cycle_start: dt.date | None) -> str:
    """合同号：订阅号 + 合同周期起，账单/分段/导出三处都带同一个号。"""
    stamp = cycle_start.strftime("%Y%m%d") if cycle_start else "00000000"
    return f"CT-{sub_id}-{stamp}"


def cycle_label(months: int) -> str:
    return config.CYCLE_LABELS.get(int(months), f"{int(months)}个月")


def legacy(monthly: Decimal, period_days: int, flags: list[str] | None = None) -> Pricing:
    """旧口径：月价就是整期金额，分母是期间天数。"""
    return Pricing(
        model=config.PRICING_LEGACY,
        contract_amount=monthly,
        cycle_months=0,
        cycle_start=None,
        cycle_end=None,
        cycle_days=period_days,
        rate_days=period_days,
        flags=list(flags or []),
    )


def contract(contract_amount: Decimal, cycle_months: int, period_start: dt.date,
             period_days: int, flags: list[str] | None = None,
             contract_id: str = "") -> Pricing:
    """合同口径：周期起点同期间起，周期天数由周期月数推。"""
    months = max(int(cycle_months), 1)
    start, end = days_mod.cycle_bounds(period_start, months)
    length = days_mod.days(start, end)
    return Pricing(
        model=config.PRICING_CONTRACT,
        contract_amount=Decimal(contract_amount),
        cycle_months=months,
        cycle_start=start,
        cycle_end=end,
        cycle_days=length,
        rate_days=length if length > 0 else period_days,
        contract_id=contract_id,
        flags=list(flags or []),
    )


def contract_monthly(pricing: Pricing) -> Decimal:
    """合同额反推月价，用于展示与「月价×周期」一致性检查。"""
    months = pricing.cycle_months or 1
    return Decimal(pricing.contract_amount) / Decimal(months)


def reprice(pricing: Pricing, new_monthly: Decimal) -> Pricing:
    """变更后按新月价重定合同额：合同口径下 合同额 = 新月价 × 周期月数。

    旧口径保持「新月价即整期金额」。周期起点与周期天数不随变更移动，
    这样一张账单里所有段共用同一个日价分母，望远镜恒等式仍精确成立。
    """
    if pricing.legacy:
        return legacy(new_monthly, pricing.rate_days, pricing.flags)
    return Pricing(
        model=pricing.model,
        contract_amount=Decimal(new_monthly) * Decimal(pricing.cycle_months or 1),
        cycle_months=pricing.cycle_months,
        cycle_start=pricing.cycle_start,
        cycle_end=pricing.cycle_end,
        cycle_days=pricing.cycle_days,
        rate_days=pricing.rate_days,
        contract_id=pricing.contract_id,
        flags=list(pricing.flags),
    )


def carry(pricing: Pricing) -> Pricing:
    """暂停/取消/恢复未给价：合同额原样保留，金额由零费率状态决定。"""
    return Pricing(
        model=pricing.model,
        contract_amount=pricing.contract_amount,
        cycle_months=pricing.cycle_months,
        cycle_start=pricing.cycle_start,
        cycle_end=pricing.cycle_end,
        cycle_days=pricing.cycle_days,
        rate_days=pricing.rate_days,
        contract_id=pricing.contract_id,
        flags=list(pricing.flags),
    )


def from_sub(sub: dict) -> Pricing:
    """从已归一入库的订阅行还原定价事实；旧批次缺列时回落到月价口径。"""
    model = sub.get("pricing_model") or config.PRICING_LEGACY
    monthly = Decimal(str(sub.get("monthly") or "0"))
    period_days = int(sub.get("period_days") or 0)
    if model != config.PRICING_CONTRACT:
        return legacy(monthly, period_days)
    cycle_start = _parse_day(sub.get("cycle_start"))
    cycle_end = _parse_day(sub.get("cycle_end"))
    cycle_days = int(sub.get("cycle_days") or 0)
    rate_days = int(sub.get("rate_days") or cycle_days or period_days)
    amount_text = str(sub.get("contract_amount") or "")
    amount = Decimal(amount_text) if amount_text else monthly
    return Pricing(
        model=model,
        contract_amount=amount,
        cycle_months=int(sub.get("cycle_months") or 0),
        cycle_start=cycle_start,
        cycle_end=cycle_end,
        cycle_days=cycle_days,
        rate_days=rate_days,
        contract_id=str(sub.get("contract_id") or ""),
        flags=[],
    )


def _parse_day(text) -> dt.date | None:
    if not text:
        return None
    try:
        return dt.date.fromisoformat(str(text))
    except ValueError:
        return None
