# 全部订单

> 导出时间：{{ exported_at }} · 共 {{ orders|length }} 单（活跃 {{ active_count }} / 已归档 {{ archived_count }}）

{% for o in orders %}
---

## #{{ "%03d"|format(o.id) }} · {{ o.customer_name or '（无客户）' }} — {{ o.project_name }}

| 字段 | 值 |
|---|---|
| 客户 | {{ o.customer_name or '—' }} |
| 来源 | {{ o.source }}{% if o.source in platform_sources %}（平台）{% endif %} |
| 稿件类别 | {{ o.commission_type or '—' }} |
| 阶段 | {{ o.current_stage }} |
| DDL状态 | {{ o.ddl_status }} |
| 排期 | {{ o.scheduled_start.replace('T', ' ') if o.scheduled_start else '—' }} → {{ o.scheduled_end.replace('T', ' ') if o.scheduled_end else '—' }} |
| 定金 / 尾款 | ¥{{ "%.2f"|format(o.deposit or 0) }} / ¥{{ "%.2f"|format(o.balance or 0) }} |
| 收入 / 手续费 / 实收 | ¥{{ "%.2f"|format(o.income or 0) }} / ¥{{ "%.2f"|format(o.platform_fee or 0) }} / ¥{{ "%.2f"|format(o.actual_received or 0) }} |
| 收款状态 | {{ o.payment_status }} |
| 商用 | {{ '是' if o.is_commercial else '否' }} |
| 复购 | {{ '是（第 %d 次）'|format(o.repeat_count) if o.is_repeat else '否' }} |
| 归档 | {{ '是' if o.is_archived else '否' }} |
| 平台链接 | {{ o.platform_url or '—' }} |
| 页面截稿日 | {{ o.page_deadline.replace('T', ' ') if o.page_deadline else '—' }} |
| 创建 / 更新 | {{ o.created_at }} / {{ o.updated_at }} |
{% if o.notes %}

> **备注**：{{ o.notes|replace('\n', '\n> ') }}
{% endif %}

{% endfor %}
