"""排单工具 — 数据校验层 (Pydantic v2)

所有选择列表从 db.CHOICE_REGISTRY 统一管理。
"""

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional

# P19-F12 移除 STAGE_CHOICES/SOURCE_CHOICES/DDL_CHOICES/PAYMENT_CHOICES 死代码：
# 所有选择字段值从 db.get_choices() 动态获取（注册表 + settings 自定义 + auto-discover）；
# 平台来源判定见 PLATFORM_SOURCES/DIRECT_SOURCES。

# 平台类来源（有手续费）
PLATFORM_SOURCES = {'米画师', 'B站工坊', '画加'}
# 直接来源（无手续费）
DIRECT_SOURCES = {'微信', 'QQ', '其他'}


class OrderCreate(BaseModel):
    customer_id: Optional[int] = None
    project_name: str = Field(..., min_length=1, max_length=500)
    source: str = Field(default='米画师')
    is_commercial: bool = False
    commission_type: Optional[str] = None
    current_stage: str = Field(default='待开始')
    ddl_status: str = Field(default='正常')
    deposit: float = Field(default=0.0, ge=0)
    balance: float = Field(default=0.0, ge=0)
    platform_fee_pct: Optional[float] = Field(default=None, ge=0, le=100)
    platform_fee: float = Field(default=0.0, ge=0)
    payment_status: str = Field(default='未收款')  # P19-F4 默认值归一（锁定 5 值之一）
    platform_url: Optional[str] = None
    page_deadline: Optional[str] = None
    notes: Optional[str] = None
    scheduled_start: Optional[str] = None
    scheduled_end: Optional[str] = None
    is_repeat: bool = False
    repeat_count: int = Field(default=0, ge=0)
    # P20b 时薪：工时单位小时，允许小数（step 0.5），范围 0~10000；留空 = None
    estimated_hours: Optional[float] = Field(default=None, ge=0, le=10000)
    work_hours: Optional[float] = Field(default=None, ge=0, le=10000)
    # Spec12 阶段流程快照（JSON 字符串；实际结构校验交给 db.validate_stage_flow）
    stage_flow: Optional[str] = None
    # 所有选择字段不再硬编码校验 — 值从 db.get_choices() 动态获取

    @field_validator('estimated_hours', 'work_hours', mode='before')
    @classmethod
    def _norm_hours(cls, v):
        """工时留空（表单空字符串）→ None，避免未填工时时校验失败。"""
        if v is None:
            return None
        s = str(v).strip()
        return None if not s else s

    @field_validator('scheduled_start', 'scheduled_end', 'page_deadline', mode='before')
    @classmethod
    def _norm_iso_date(cls, v):
        """ISO 日期/日期时间格式校验：空串 → None；非法格式 → 422。
        支持 YYYY-MM-DD（按日模式）和 YYYY-MM-DDTHH:MM（精确到分钟模式）。
        """
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        try:
            date.fromisoformat(s[:10])
        except ValueError:
            raise ValueError('日期格式须为 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM')
        # 若含时间部分，校验完整 datetime 格式
        if 'T' in s:
            try:
                datetime.fromisoformat(s)
            except ValueError:
                raise ValueError('日期时间格式须为 YYYY-MM-DDTHH:MM')
        return s

    @model_validator(mode='after')
    def _check_cross_fields(self):
        """跨字段校验（P19-F6）：排期顺序 + 手续费不超总额（防负实收入库）。"""
        if self.scheduled_start and self.scheduled_end:
            sd = date.fromisoformat(self.scheduled_start[:10])
            ed = date.fromisoformat(self.scheduled_end[:10])
            if ed < sd:
                raise ValueError('排单截止日期不能早于开始日期')
        income = (self.deposit or 0) + (self.balance or 0)
        if (self.platform_fee or 0) > income:
            raise ValueError('手续费不能超过订单总额（定金+尾款），实收不可为负')
        return self


class OrderUpdate(OrderCreate):
    is_archived: Optional[bool] = None
    sort_order: Optional[int] = None
    exclude_hourly: bool = False  # P20b 单订单排除时薪统计


class CustomerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    platform_url: Optional[str] = None
    preferences: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[str] = None          # 逗号分隔标签


class CustomerUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    platform_url: Optional[str] = None
    preferences: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[str] = None
