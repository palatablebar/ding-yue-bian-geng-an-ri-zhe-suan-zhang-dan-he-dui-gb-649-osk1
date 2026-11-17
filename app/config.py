"""subbill 全局配置：列契约、编码链、状态与变更类型、Decimal 规则。

规则唯一来源，其他模块只读不写：
- 金额一律 Decimal，最小刻度 0.01 元，入库按「分」整数；
- 计费日边界取订阅时区的本地日 0 点，入库 UTC，展示固定 Asia/Shanghai；
- 期间一律半开区间 ``[start, end)``，天数为本地日历日差。
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UTC = dt.timezone.utc
DISPLAY_TZ = "Asia/Shanghai"

RULE_VERSION = "subbill/1.0.0"
# 定价模型版本：旧导出只有月价（legacy-monthly），新导出带计费周期与合同金额
# （contract-aware）。旧文件结果与 1.0.0 逐字一致，因此规则版本保持不变；
# 模型版本单独记录在运行清单里，便于审计「这批账单用的是哪套定价口径」。
PRICING_MODEL = "contract-aware/1"
CENT = Decimal("0.01")
DECIMAL_PREC = 34
MONEY_UNIT = "元"
DEFAULT_CURRENCY = "CNY"

# 编码回退链：先按 BOM，再严格 UTF-8，最后按 GB18030（GBK/GB2312 超集）
ENCODING_CHAIN = ("utf-8-sig", "utf-8", "gb18030")

# 空值哨兵：集中定义。None 哨兵 / 空串 / 0 三个语义分开
SENTINELS = frozenset(
    {"", "-", "--", "—", "——", "/", "n/a", "na", "null", "none", "nil",
     "未知", "无", "暂无", "待定", "不适用", "未提供", "?"}
)

# 订阅表列：规范键 → 中文列名 + 别名
SUB_COLUMNS = {
    "sub_id": ("订阅号", ("订阅号", "订阅ID", "订阅编号", "账号编号")),
    "account": ("账户", ("账户", "客户名称", "客户", "账户名")),
    "plan": ("套餐", ("套餐", "套餐名", "产品")),
    "price": ("月价(元)", ("月价(元)", "月价", "单价", "月价格")),
    "billing_cycle": ("计费周期", ("计费周期", "周期", "付款周期", "billing_cycle", "cycle")),
    "contract_amount": ("合同金额(元)", ("合同金额(元)", "合同金额", "合同额",
                                        "contract_amount", "contract")),
    "period_start": ("期间起", ("期间起", "计费起", "起租日")),
    "period_end": ("期间止", ("期间止", "计费止", "到期日")),
    "tz": ("时区", ("时区", "地区时区", "timezone")),
    "state": ("状态", ("状态", "订阅状态")),
}

# 计费周期：声明「一个完整合同周期」有几个月。合同金额按这个周期计价，
# 账单期只是合同周期的一个切片（月付通常切片 == 周期，年付通常切片 < 周期）。
CYCLE_MONTHS = {
    "月付": 1, "月": 1, "月度": 1, "monthly": 1, "month": 1, "1": 1, "1个月": 1,
    "季付": 3, "季度": 3, "quarterly": 3, "quarter": 3, "3": 3, "3个月": 3,
    "半年付": 6, "半年": 6, "semi-annual": 6, "semiannual": 6, "6": 6, "6个月": 6,
    "年付": 12, "年": 12, "年度": 12, "annual": 12, "yearly": 12, "year": 12,
    "12": 12, "12个月": 12,
}
CYCLE_LABELS = {1: "月付", 3: "季付", 6: "半年付", 12: "年付"}
# 定价口径：旧文件只有月价，新文件带周期与合同金额
PRICING_LEGACY = "legacy_monthly"
PRICING_CONTRACT = "contract"
PRICING_LABELS = {
    PRICING_LEGACY: "月价（旧文件）",
    PRICING_CONTRACT: "合同期计价",
}
# 月价 × 周期月数 与合同金额的允许差额（元）；超过即留复核标记
CONTRACT_TOLERANCE = Decimal("0.01")

CHANGE_COLUMNS = {
    "change_id": ("变更号", ("变更号", "变更ID", "变更编号")),
    "sub_id": ("订阅号", ("订阅号", "订阅ID", "订阅编号")),
    "effective": ("生效时间", ("生效时间", "生效日", "生效时刻")),
    "kind": ("类型", ("类型", "变更类型")),
    "new_plan": ("新套餐", ("新套餐", "目标套餐")),
    "new_price": ("新月价(元)", ("新月价(元)", "新月价", "新价格")),
}

STATE_LABELS = {
    "active": "生效中",
    "paused": "已暂停",
    "trial": "试用期",
    "cancelled": "已取消",
}
# 不计费状态：金额为 0，但天数计入「免费/暂停」一栏
ZERO_RATE_STATES = frozenset({"paused", "trial", "cancelled"})

CHANGE_KINDS = {
    "升配": "upgrade",
    "升级": "upgrade",
    "降配": "downgrade",
    "降级": "downgrade",
    "暂停": "pause",
    "挂起": "pause",
    "恢复": "resume",
    "取消": "cancel",
    "解约": "cancel",
}
KIND_LABELS = {
    "upgrade": "升配",
    "downgrade": "降配",
    "pause": "暂停",
    "resume": "恢复",
    "cancel": "取消",
}
KIND_EFFECT = {
    "upgrade": "active",
    "downgrade": "active",
    "resume": "active",
    "pause": "paused",
    "cancel": "cancelled",
}

# 流水线阶段：采信 → 期间 → 质量 → 变更 → 折算 → 账单
STAGES = ("ingest", "calendar", "quality", "changes", "proration", "invoices")
STAGE_LABELS = {
    "ingest": "采信",
    "calendar": "期间",
    "quality": "质量",
    "changes": "变更",
    "proration": "折算",
    "invoices": "账单",
}
STAGE_FLOW = "采信 → 期间 → 质量 → 变更 → 折算 → 账单"

INVOICE_STATUS_LABELS = {
    "ok": "已出账",
    "zero": "零金额",
    "conflict": "冲突",
}
RUN_STATUS_LABELS = {
    "done": "完成",
    "running": "运行中",
    "failed": "失败",
}


@dataclass(frozen=True)
class Settings:
    """运行参数；测试用 replace() 覆盖，不读环境变量分支。"""

    db_path: str = str(PROJECT_ROOT / "var" / "subbill.sqlite")
    sample_dir: str = str(PROJECT_ROOT / "sample_data")
    display_tz: str = DISPLAY_TZ
    rule_version: str = RULE_VERSION
    default_subscriptions: str = "subscriptions_dirty.csv"
    default_changes: str = "changes_dirty.csv"
    export_dir: str = str(PROJECT_ROOT / "var" / "export")

    @classmethod
    def from_env(cls) -> "Settings":
        base = cls()
        db_path = os.getenv("SUBBIL_DB") or base.db_path
        sample_dir = os.getenv("SUBBIL_SAMPLE_DIR") or base.sample_dir
        return replace(cls(), db_path=db_path, sample_dir=sample_dir)

    @property
    def data_dir(self) -> Path:
        return Path(self.db_path).resolve().parent
