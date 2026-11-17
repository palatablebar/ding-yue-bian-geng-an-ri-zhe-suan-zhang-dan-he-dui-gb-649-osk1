"""变更抵扣：预缴整期 + 每次变更「抵扣旧套餐剩余额度、补收新套餐剩余额度」。

净额恒等式（望远镜求和）：
    账单 = 整期预缴 − Σ抵扣 + Σ补收 = Σ(各段费率 × 段天数)
任何一张账单都必须满足这条恒等式，测试拿它当不变量。

合同口径下每一段有自己的日价分母（``rate_days``，即合同周期天数），
因此「整期预缴」是合同额按账期切片后的金额，而不是合同额本身：
年付 12000 的月度账期只预缴约 920.55，绝不会把年价当整期金额放大十二倍。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ... import config
from ..m4_proration import pricing as pricing_mod
from ..m4_proration import prorate
from .segments import Segment


@dataclass
class Component:
    seq: int
    kind: str            # charge / credit
    source: str          # base / change:<变更号>
    label: str
    days: int
    monthly: Decimal
    exact: Decimal
    flags: list[str] = field(default_factory=list)
    pricing_model: str = config.PRICING_LEGACY
    contract_id: str = ""
    rate_days: int = 0


def _pricing(segment: Segment, period_days: int) -> pricing_mod.Pricing:
    return segment.pricing or pricing_mod.legacy(
        prorate.rate_for(segment.monthly, segment.state), period_days)


def _rate_days(pricing: pricing_mod.Pricing, period_days: int) -> int:
    return int(pricing.rate_days or period_days)


def _rate(segment: Segment, pricing: pricing_mod.Pricing) -> Decimal:
    """计费费率：零费率状态（暂停/试用/取消）返回 0，天数照常记录。"""
    return prorate.rate_for(pricing.contract_amount, segment.state)


def _share(segment: Segment, pricing: pricing_mod.Pricing, days: int,
           period_days: int) -> Decimal:
    return prorate.share(_rate(segment, pricing), days, _rate_days(pricing, period_days))


def build_components(segments: list[Segment], period_days: int) -> list[Component]:
    """按段序列生成「预缴 / 抵扣 / 补收」三类行；金额一律精确 Decimal。"""
    if not segments:
        return []
    components: list[Component] = []
    first = segments[0]
    first_pricing = _pricing(first, period_days)
    components.append(Component(
        seq=0, kind="charge", source="base", label="整期预缴",
        days=period_days, monthly=first.monthly,
        exact=_share(first, first_pricing, period_days, period_days),
        pricing_model=first_pricing.model, contract_id=first_pricing.contract_id or "",
        rate_days=_rate_days(first_pricing, period_days),
    ))
    for index in range(1, len(segments)):
        previous = segments[index - 1]
        current = segments[index]
        previous_pricing = _pricing(previous, period_days)
        current_pricing = _pricing(current, period_days)
        # 剩余天数 = 期间天数 − 已经走过的段天数（等价于 current.start 到期间止）
        remaining = period_days - sum(segment.days for segment in segments[:index])
        credit_exact = _share(previous, previous_pricing, remaining, period_days)
        charge_exact = _share(current, current_pricing, remaining, period_days)
        change_id = current.change_id or f"seg{index}"
        components.append(Component(
            seq=len(components), kind="credit", source=f"change:{change_id}",
            label="抵扣剩余", days=remaining, monthly=previous.monthly,
            exact=-credit_exact, flags=list(current.flags),
            pricing_model=previous_pricing.model,
            contract_id=previous_pricing.contract_id or "",
            rate_days=_rate_days(previous_pricing, period_days),
        ))
        components.append(Component(
            seq=len(components), kind="charge", source=f"change:{change_id}",
            label="补收剩余", days=remaining, monthly=current.monthly,
            exact=charge_exact, flags=list(current.flags),
            pricing_model=current_pricing.model,
            contract_id=current_pricing.contract_id or "",
            rate_days=_rate_days(current_pricing, period_days),
        ))
    return components


def net_exact(components: list[Component]) -> Decimal:
    return prorate.net_exact([component.exact for component in components])


def segment_exact(segments: list[Segment], period_days: int) -> Decimal:
    return prorate.net_exact([
        _share(segment, _pricing(segment, period_days), segment.days, period_days)
        for segment in segments
    ])


def verify(components: list[Component], segments: list[Segment],
           period_days: int) -> dict:
    """恒等式核对：净额与分段合计的差额（应为 0E-8）。"""
    net = prorate.quantize_exact(net_exact(components))
    by_segment = prorate.quantize_exact(segment_exact(segments, period_days))
    return {"net": net, "by_segment": by_segment, "diff": net - by_segment}


def charged_days(components: list[Component]) -> int:
    """补收行覆盖的天数（不含预缴整期），用于页面复核。"""
    return sum(component.days for component in components
               if component.kind == "charge" and component.source != "base")
