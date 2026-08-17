"""排单工具 — 数据校验层 (Pydantic v2)

所有选择列表从 db.CHOICE_REGISTRY 统一管理。
"""

import math
import re
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Literal, Optional

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
    # Spec19 VIP 折扣：折后应收百分比（88 = 88 折），NULL = 不打折；合法域 (0, 100]（D1/D2）
    discount_pct: Optional[float] = Field(default=None, gt=0, le=100)
    # Spec 26 收款模式：simple=一次性（虚拟事件，按排期月计收入）/ installment=分期（order_payments 流水）
    payment_mode: Literal['simple', 'installment'] = 'simple'
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

    @field_validator('discount_pct', mode='before')
    @classmethod
    def _norm_discount_pct(cls, v):
        """Spec19：折扣留空（表单空字符串）→ None（NULL = 不打折，D2）。"""
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


class PaymentRecord(BaseModel):
    """Spec 26 分期收款单笔记录（order_payments 写入前校验；路由层归 task-108）。"""
    paid_at: str = Field(..., min_length=1)   # 到账日期，归一为 YYYY-MM-DD（月度归属依据）
    amount: float = Field(..., ge=0)
    note: Optional[str] = Field(default=None, max_length=500)  # 备注（定金/阶段款/尾款，可选）

    @field_validator('paid_at', mode='before')
    @classmethod
    def _norm_paid_at(cls, v):
        """到账日期归一：空串 → 422；接受 YYYY-MM-DD[THH:MM]，统一截取前 10 位。"""
        s = str(v or '').strip()
        if not s:
            raise ValueError('到账日期不能为空')
        try:
            return date.fromisoformat(s[:10]).isoformat()
        except ValueError:
            raise ValueError('到账日期格式须为 YYYY-MM-DD')

    @field_validator('amount', mode='before')
    @classmethod
    def _norm_amount(cls, v):
        """金额留空（表单空字符串）→ 0；非有限数值 → 422（对齐价目表 isfinite 守卫）。"""
        if v is None:
            return 0.0
        s = str(v).strip()
        return 0.0 if not s else s

    @field_validator('note', mode='before')
    @classmethod
    def _norm_note(cls, v):
        """备注留空 → None；首尾空白归一。"""
        s = str(v or '').strip()
        return s or None

    @model_validator(mode='after')
    def _check_amount_finite(self):
        """NaN/inf 拒收（Pydantic 的 ge=0 对 NaN 失效，显式守卫；对齐 PricelistItemIn 先例）。"""
        if not math.isfinite(self.amount):
            raise ValueError('收款金额数值溢出，请填写正常金额')
        return self


class CustomerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    platform_url: Optional[str] = None
    preferences: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[str] = None          # 逗号分隔标签
    # Spec19 VIP 折扣：is_vip 仅徽标语义（D6）；discount_pct 合法域 (0, 100]，None = 不打折（D2）
    is_vip: bool = False
    discount_pct: Optional[float] = Field(default=None, gt=0, le=100)

    @field_validator('discount_pct', mode='before')
    @classmethod
    def _norm_discount_pct(cls, v):
        """Spec19：折扣留空（表单空字符串）→ None（NULL = 不打折，D2）。"""
        if v is None:
            return None
        s = str(v).strip()
        return None if not s else s


class CustomerUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    platform_url: Optional[str] = None
    preferences: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[str] = None
    # Spec19 VIP 折扣（同 CustomerCreate 口径）
    is_vip: bool = False
    discount_pct: Optional[float] = Field(default=None, gt=0, le=100)

    @field_validator('discount_pct', mode='before')
    @classmethod
    def _norm_discount_pct(cls, v):
        """Spec19：折扣留空（表单空字符串）→ None（NULL = 不打折，D2）。"""
        if v is None:
            return None
        s = str(v).strip()
        return None if not s else s


# ═══ Spec 22 小工具 ═══

class ReplyTemplateIn(BaseModel):
    """002 回复模板（类剪贴板纯文本，D9 不做富文本）。"""
    group_name: str = Field(default='未分组', max_length=50)
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=5000)

    @field_validator('group_name', mode='before')
    @classmethod
    def _norm_group(cls, v):
        """分组名 strip 后为空 → 「未分组」。"""
        s = str(v or '').strip()
        return s or '未分组'

    @field_validator('title', 'content', mode='before')
    @classmethod
    def _norm_text(cls, v):
        """首尾空白归一（保留内容内部换行）。"""
        return str(v or '').strip()


class PricelistItemIn(BaseModel):
    """003 价目表项目。"""
    category: str = Field(default='默认', max_length=50)
    name: str = Field(..., min_length=1, max_length=100)
    price: float = Field(default=0.0, ge=0)
    price_max: float | None = Field(default=None, ge=0)
    unit: str = Field(default='', max_length=20)
    description: str = Field(default='', max_length=500)

    @field_validator('category', mode='before')
    @classmethod
    def _norm_category(cls, v):
        """分类 strip 后为空 → 「默认」。"""
        s = str(v or '').strip()
        return s or '默认'

    @field_validator('price', mode='before')
    @classmethod
    def _norm_price(cls, v):
        """价格留空（表单空字符串）→ 0。"""
        if v is None:
            return 0.0
        s = str(v).strip()
        return 0.0 if not s else s

    @field_validator('price_max', mode='before')
    @classmethod
    def _norm_price_max(cls, v):
        """价格上限留空 → None（一口价语义）。"""
        if v is None:
            return None
        s = str(v).strip()
        return None if not s else s

    @model_validator(mode='after')
    def _check_price_range(self):
        """跨字段：非有限数值拒收；上限 ≤0 → None；双价并存时上限不得低于起价。"""
        if not math.isfinite(self.price):
            raise ValueError('价格数值溢出，请填写正常金额')
        if self.price_max is not None:
            if not math.isfinite(self.price_max):
                raise ValueError('价格上限数值溢出，请填写正常金额')
            if self.price_max <= 0:
                self.price_max = None
        if (self.price_max is not None and self.price > 0
                and self.price_max < self.price):
            raise ValueError('价格上限不能低于起价')
        return self

    @field_validator('name', 'unit', 'description', mode='before')
    @classmethod
    def _norm_text(cls, v):
        """首尾空白归一。"""
        return str(v or '').strip()


# ═══════════════════════════════════════════════════════════
# Spec 23 小票打印机（草稿整体保存 D4，校验口径见 spec §3.2）
# ═══════════════════════════════════════════════════════════

RECEIPT_PRESETS = ('list', 'retro', 'hand', 'mono')


class ReceiptExtraIn(BaseModel):
    """附加服务子行（加钱型：挂单个制品的加价金额）。"""
    name: str = Field(..., min_length=1, max_length=50)
    price: float = Field(default=0, ge=0, le=999999)
    qty: float = Field(default=1, gt=0, le=9999)

    @field_validator('name', mode='before')
    @classmethod
    def _norm(cls, v):
        return str(v or '').strip()


class ReceiptItemIn(BaseModel):
    """主制品行（extras 为附加服务子行；Spec 24 加单品倍率/折扣）。"""
    name: str = Field(..., min_length=1, max_length=50)
    price: float = Field(default=0, ge=0, le=999999)
    qty: float = Field(default=1, gt=0, le=9999)
    is_gift: bool = False
    multiplier: float = Field(default=1, ge=0.1, le=99)
    mult_label: str = Field(default='', max_length=20)
    discount_type: str = Field(default='none')
    discount_value: float = Field(default=0, ge=0)
    extras: list[ReceiptExtraIn] = []

    @field_validator('name', 'mult_label', mode='before')
    @classmethod
    def _norm(cls, v):
        return str(v or '').strip()

    @field_validator('discount_type')
    @classmethod
    def _check_disc_type(cls, v):
        if v not in ('none', 'amount', 'rate'):
            raise ValueError('单品折扣形态必须是 none/amount/rate 之一')
        return v

    @model_validator(mode='after')
    def _post(self):
        """赠品行不参与倍率折扣（恒计 ¥0）；rate 折数限 (0, 10]。"""
        if self.is_gift:
            self.multiplier, self.mult_label = 1.0, ''
            self.discount_type, self.discount_value = 'none', 0.0
        if self.discount_type == 'rate' and not (0 < self.discount_value <= 10):
            raise ValueError('折数必须在 0-10 之间（如 8.8 表示 8.8 折）')
        if self.discount_type == 'none':
            self.discount_value = 0.0
        return self


class ReceiptMetaIn(BaseModel):
    """小票文案 + 计算参数（整体倍率/折扣双形态/定金，Spec 24）。"""
    shop_name: str = Field(default='', max_length=50)
    subtitle: str = Field(default='', max_length=100)
    order_no: str = Field(default='', max_length=50)
    order_date: str = Field(default='', max_length=30)
    contact: str = Field(default='', max_length=100)
    footer: str = Field(default='感谢惠顾', max_length=200)
    multiplier: float = Field(default=1, ge=0.1, le=99)
    # 2026-08-13 用户需求 2b：整单倍率行文案自定义（mult_expr 的 {n} 为数值占位符，空串=不显示乘数）
    mult_label: str = Field(default='倍率', max_length=20)
    mult_expr: str = Field(default='×{n}', max_length=30)
    discount_type: str = Field(default='none')
    discount_value: float = Field(default=0, ge=0)
    deposit: float = Field(default=0, ge=0)

    @model_validator(mode='before')
    @classmethod
    def _compat_old_discount(cls, data):
        """Spec 24 兼容：旧 discount（金额）键归一为 discount_type='amount' + discount_value。"""
        if isinstance(data, dict) and 'discount' in data and not data.get('discount_type'):
            data['discount_type'] = 'amount'
            data['discount_value'] = data.pop('discount', 0)
        return data

    @field_validator('discount_type')
    @classmethod
    def _check_disc_type(cls, v):
        if v not in ('none', 'amount', 'rate'):
            raise ValueError('折扣形态必须是 none/amount/rate 之一')
        return v

    @field_validator('mult_label', 'mult_expr', mode='before')
    @classmethod
    def _norm_mult_text(cls, v):
        return str(v or '').strip()

    @model_validator(mode='after')
    def _post(self):
        if self.discount_type == 'rate' and not (0 < self.discount_value <= 10):
            raise ValueError('折数必须在 0-10 之间（如 8.8 表示 8.8 折）')
        if self.discount_type == 'none':
            self.discount_value = 0.0
        return self


class ReceiptMultPresetIn(BaseModel):
    """单品倍率快捷预设（Spec 24：名称+倍率均可自定义）。"""
    label: str = Field(..., min_length=1, max_length=20)
    value: float = Field(default=1, ge=0.1, le=99)

    @field_validator('label', mode='before')
    @classmethod
    def _norm(cls, v):
        return str(v or '').strip()


class ReceiptStyleIn(BaseModel):
    """样式：预设/纸色/墨色/背景/主图/开关（D9）。"""
    preset: str = Field(default='list')
    paper: str = Field(default='#fdfcf8')
    ink: str = Field(default='#1a1a1a')
    bg_path: str = Field(default='', max_length=200)
    image_path: str = Field(default='', max_length=200)
    image_mode: str = Field(default='dither')
    # 2026-08-13 用户需求 3：footer 插图（总计与感谢语之间，同主图三模式上传管线）
    footer_image_path: str = Field(default='', max_length=200)
    footer_image_mode: str = Field(default='color')
    barcode: bool = True
    zigzag: bool = True

    @field_validator('preset')
    @classmethod
    def _check_preset(cls, v):
        if v not in RECEIPT_PRESETS:
            raise ValueError(f'预设必须是 {RECEIPT_PRESETS} 之一')
        return v

    @field_validator('image_mode', 'footer_image_mode')
    @classmethod
    def _check_mode(cls, v):
        if v not in ('gray', 'dither', 'color'):
            raise ValueError('图片模式必须是 gray、dither 或 color')
        return v

    @field_validator('paper', 'ink')
    @classmethod
    def _check_hex(cls, v):
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', v or ''):
            raise ValueError('颜色必须是 #RRGGBB 十六进制')
        return v.lower()


class ReceiptDraftIn(BaseModel):
    """小票草稿（整体保存；items 允许空数组=空票）。"""
    items: list[ReceiptItemIn] = []
    meta: ReceiptMetaIn = ReceiptMetaIn()
    style: ReceiptStyleIn = ReceiptStyleIn()

    @model_validator(mode='after')
    def _check_amounts(self):
        """冻结公式（Spec 24）：金额折扣 ≤ 加价后总计；定金 ≤ 最终总计。"""
        total = 0.0
        for it in self.items:
            if it.is_gift:
                continue
            subtotal = it.price * it.qty + sum(e.price * e.qty for e in it.extras)
            subtotal *= it.multiplier
            if it.discount_type == 'amount':
                subtotal = max(0.0, subtotal - it.discount_value)
            elif it.discount_type == 'rate':
                subtotal *= it.discount_value / 10
            total += subtotal
        multed = total * self.meta.multiplier
        if self.meta.discount_type == 'rate':
            grand = multed * self.meta.discount_value / 10
        elif self.meta.discount_type == 'amount':
            grand = multed - self.meta.discount_value
            if self.meta.discount_value > multed + 1e-6:
                raise ValueError('折扣金额不能超过加价后的总计')
        else:
            grand = multed
        if self.meta.deposit > grand + 1e-6:
            raise ValueError('已收定金不能超过最终总计')
        return self
