"""排单工具 — V2 开发版"""

from datetime import date, datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory
from flask_cors import CORS
import os
import re
import sys
import signal
import logging
import shutil
import uuid
import json
import time
from urllib.parse import urlparse
import db
from models import (
    OrderCreate, OrderUpdate, CustomerCreate, CustomerUpdate,
)
from image_processor import (
    process_uploaded_file, process_uploaded_file_multi, save_without_pillow,
    ALLOWED_EXTS, MAX_UPLOAD_SIZE,
)

logger = logging.getLogger(__name__)
# 动态选择列表从 db.get_choices() 读取（替代硬编码常量）
from pydantic import ValidationError


def _resource_dir() -> str:
    """只读资源目录（templates/static）：PyInstaller 冻结时为 _MEIPASS；开发时为 app.py 所在目录。"""
    return sys._MEIPASS if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))


app = Flask(__name__,
            template_folder=os.path.join(_resource_dir(), 'templates'),
            static_folder=os.path.join(_resource_dir(), 'static'))

# P19-F7 CORS 收敛：不再 origins="*"，仅白名单——本机页面（任意端口）+ 米画师插件宿主页面 + 插件扩展来源。
# supports_credentials 保持 False：本地单用户工具无凭据场景，浏览器不会附带 cookie 跨域。
_CORS_ORIGINS = [
    re.compile(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$"),
    re.compile(r"^https://(www\.)?mihuashi\.com$"),
    re.compile(r"^chrome-extension://[a-z]+$"),
    re.compile(r"^moz-extension://[a-z0-9-]+$"),
]
CORS(app, resources={r"/api/*": {"origins": _CORS_ORIGINS}}, supports_credentials=False)


# P19-F7 来源校验白名单（与 CORS 清单同源语义）
_LOCAL_ORIGIN_HOSTS = {'127.0.0.1', 'localhost'}
_PLUGIN_ORIGIN_HOSTS = {'mihuashi.com', 'www.mihuashi.com'}  # 插件内容脚本宿主页面


def _is_local_origin() -> bool:
    """P19-F7 来源校验：仅放行本机页面与浏览器插件来源。

    规则（按第一个非空 Origin/Referer 判定）：
    - chrome-extension:// / moz-extension:// 前缀（米画师插件扩展页面）→ 放行；
    - host ∈ {127.0.0.1, localhost}（任意端口）→ 放行；
    - host ∈ 插件宿主（mihuashi.com，内容脚本页面直接 fetch 场景）→ 放行；
    - Origin/Referer 两头都缺失 → 放行（浏览器跨站请求必带其一，缺失即同源导航/本机脚本/命令行工具，本地单用户定位下可接受）；
    - 其余任意外站来源 → False（403）。
    """
    for header_val in (request.headers.get('Origin', ''), request.headers.get('Referer', '')):
        if not header_val:
            continue
        if header_val.startswith(('chrome-extension://', 'moz-extension://')):
            return True
        host = urlparse(header_val).hostname or ''
        return host in _LOCAL_ORIGIN_HOSTS or host in _PLUGIN_ORIGIN_HOSTS
    return True
app.config['VERSION'] = 'V2'

# 订单导出目录（与 exe 同级、持久化；开发时为 app.py 同级）
EXPORT_DIR = os.path.join(db.data_dir(), 'exports')
EXPORT_FILENAME = '全部订单.md'

# 图片上传目录（与 orders.db 同级、持久化）
UPLOAD_DIR = os.path.join(db.data_dir(), 'uploads')
UPLOAD_ORDERS_DIR = os.path.join(UPLOAD_DIR, 'orders')


# ═══════════════════════════════════════════════════════════
# Jinja 自定义过滤器
# ═══════════════════════════════════════════════════════════

STAGE_CLASS_MAP = {
    '待开始': 'pending', '色稿': 'sketch', '线稿': 'lineart',
    '细化': 'detail', '收尾': 'finish', '完成': 'completed', '退单': 'cancelled'
}

@app.template_filter('stage_class')
def stage_class(stage_name):
    """将阶段名称转为 CSS class 名"""
    return STAGE_CLASS_MAP.get(stage_name, 'pending')


def _iso_or_none(s):
    """P19-F6：ISO 日期字符串 → date；空/非法 → None（调用方决定容错或 400）。"""
    if not s:
        return None
    try:
        return date.fromisoformat(str(s).strip()[:10])
    except (TypeError, ValueError):
        return None


def _reject_bad_range(start_date, end_date):
    """P19-F6：from/to 查询参数非空但非 ISO 日期 → True（路由应返回 400）。"""
    return bool((start_date and not _iso_or_none(start_date)) or (end_date and not _iso_or_none(end_date)))


@app.template_filter('render_notes')
def render_notes(text):
    """将备注中的图片 URL 渲染为可点击缩略图

    P19-F6 安全：URL 经 HTML 属性转义后仅放 src，onclick 改 window.open(this.src)，
    消除 URL 引号逃逸 onclick 属性的注入面。
    """
    import html
    import re
    if not text:
        return text
    # 匹配图片 URL（常见格式）
    img_pattern = re.compile(
        r'(https?://\S+\.(?:png|jpg|jpeg|gif|webp|bmp)(?:\?\S*)?)',
        re.IGNORECASE
    )
    # 匹配 Markdown 图片语法 ![](url)
    md_img_pattern = re.compile(r'!\[.*?\]\((https?://\S+)\)')

    result = text
    # 先处理 Markdown 图片
    def md_replace(m):
        url = html.escape(m.group(1), quote=True)
        return f'<img src="{url}" class="notes-img" loading="lazy" onclick="window.open(this.src)" title="点击查看原图">'
    result = md_img_pattern.sub(md_replace, result)

    # 再处理裸 URL（跳过已处理的和已在 <img> 标签中的）
    def url_replace(m):
        url = html.escape(m.group(1), quote=True)
        return f'<br><img src="{url}" class="notes-img" loading="lazy" onclick="window.open(this.src)" title="点击查看原图"><br>'
    # 简化：只在非 img 标签内替换
    lines = result.split('\n')
    new_lines = []
    for line in lines:
        if '<img' not in line:
            line = img_pattern.sub(url_replace, line)
        new_lines.append(line)
    result = '\n'.join(new_lines)

    return result


# ═══════════════════════════════════════════════════════════
# 全局上下文 — 侧边栏需要的常量
# ═══════════════════════════════════════════════════════════

# ── P16j 主题定制：自定义 CSS 导入并存为预设 ──
# 存储：settings KV 中 custom_themes = JSON 列表 [{id,name,css}]；active_custom_theme = 激活的自定义主题 id
MAX_THEME_CSS_LEN = 20000
_FORBIDDEN_CSS_TOKENS = ('<', '>', 'javascript:', 'expression(', '@import', 'behavior:', 'url(javascript')


def _load_custom_themes(settings: dict) -> list:
    """从 settings 解析自定义主题列表（JSON），失败返回空列表"""
    raw = settings.get('custom_themes')
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            # 只保留结构合法的条目
            return [t for t in data if isinstance(t, dict) and t.get('id') and 'css' in t]
    except Exception:
        pass
    return []


def _sanitize_theme_css(css: str):
    """校验导入的主题 CSS。返回 (cleaned_css, error)：只接受纯 CSS、限制大小、剔除脚本/注入。"""
    if css is None:
        return '', 'CSS 内容为空'
    css = css.strip()
    if not css:
        return '', 'CSS 内容为空'
    if len(css) > MAX_THEME_CSS_LEN:
        return '', f'CSS 内容过长（上限 {MAX_THEME_CSS_LEN} 字符）'
    lowered = css.lower()
    for token in _FORBIDDEN_CSS_TOKENS:
        if token in lowered:
            return '', f'包含非法内容：{token}（仅接受纯 CSS）'
    return css, None


def _build_theme_css(settings: dict) -> str:
    """将设置转为 CSS 变量覆盖"""
    mapping = {
        'theme_bg': '--color-bg',
        'theme_surface': '--color-surface',
        'theme_sidebar': '--color-sidebar',
        'theme_text': '--color-text',
        'theme_text_secondary': '--color-text-secondary',
        'theme_border': '--color-border',
        'theme_accent': '--color-accent',
        'theme_link': '--color-link',
        'theme_success': '--color-success',
        'theme_warning': '--color-warning',
        'theme_danger': '--color-danger',
        'stage_pending': '--stage-pending',
        'stage_sketch': '--stage-sketch',
        'stage_lineart': '--stage-lineart',
        'stage_detail': '--stage-detail',
        'stage_finish': '--stage-finish',
        'stage_completed': '--stage-completed',
        'stage_cancelled': '--stage-cancelled',
    }
    lines = []
    # 外观设置
    if 'font_size' in settings:
        lines.append(f"    --font-size-base: {settings['font_size']};")
    if 'font_family' in settings:
        ff = settings['font_family']
        if ff == 'serif':
            lines.append("    --font-sans: 'Noto Serif SC', 'Source Han Serif', Georgia, serif;")
        elif ff == 'mono':
            lines.append("    --font-sans: 'JetBrains Mono', 'SF Mono', 'Fira Code', monospace;")
        # system = default, no override needed
    for key, css_var in mapping.items():
        if key in settings:
            val = settings[key]
            lines.append(f"    {css_var}: {val};")
            # 自动生成背景色（bg 变体 = 原色 + 低透明度）
            if key.startswith('stage_'):
                lines.append(f"    {css_var}-bg: color-mix(in srgb, {val} 12%, transparent);")
            elif key == 'theme_success':
                lines.append(f"    --color-success-bg: color-mix(in srgb, {val} 12%, transparent);")
            elif key == 'theme_warning':
                lines.append(f"    --color-warning-bg: color-mix(in srgb, {val} 12%, transparent);")
            elif key == 'theme_danger':
                lines.append(f"    --color-danger-bg: color-mix(in srgb, {val} 12%, transparent);")
    root_block = ':root {\n' + '\n'.join(lines) + '\n}' if lines else ''
    # html 背景双写：全量刷新跳转间隙 html 底色随主题同步，避免黑屏/白闪
    # （静态 app.css 提供默认底色 var(--color-bg)，此处 theme_css 覆盖为当前主题底色）
    if settings.get('theme_bg'):
        root_block += f"\nhtml {{ background: {settings['theme_bg']}; }}"
    # P16j：若已选用自定义主题，将其 CSS 追加在内置 :root 之后（后写覆盖，优先级更高）
    active_id = settings.get('active_custom_theme')
    if active_id:
        for _t in _load_custom_themes(settings):
            if _t.get('id') == active_id:
                root_block += f"\n/* custom theme: {_t.get('name', '')} */\n{_t.get('css', '')}"
                break
    return root_block


# P18-F4：快捷键功能集固定，按键可自定义（存 settings['shortcuts_json']）
DEFAULT_SHORTCUTS = {
    'new_order': 'Ctrl+N',
    'focus_search': 'Ctrl+K',
    'toggle_sidebar': 'Ctrl+B',
    'close': 'Escape',
    'nav_home': 'g h',
    'nav_income': 'g i',
    'nav_calendar': 'g c',
    'nav_orders': 'g l',
    'nav_kanban': 'g k',
    'nav_customers': 'g u',
    'nav_gallery': 'g g',
    'nav_settings': 'g s',
}
SHORTCUT_LABELS = {
    'new_order': '新建订单',
    'focus_search': '聚焦搜索框',
    'toggle_sidebar': '切换侧边栏',
    'close': '关闭弹窗/抽屉',
    'nav_home': '跳转 · 主页',
    'nav_income': '跳转 · 收入看板',
    'nav_calendar': '跳转 · 日历视图',
    'nav_orders': '跳转 · 排单列表',
    'nav_kanban': '跳转 · 看板视图',
    'nav_customers': '跳转 · 客户管理',
    'nav_gallery': '跳转 · 画廊',
    'nav_settings': '跳转 · 设置',
}


def merge_shortcuts(settings):
    """合并存储的自定义按键与默认布局（功能集固定，按键可覆盖）。"""
    merged = dict(DEFAULT_SHORTCUTS)
    try:
        raw = settings.get('shortcuts_json')
        if raw:
            saved = json.loads(raw)
            for k in merged:
                v = saved.get(k)
                if isinstance(v, str) and v.strip():
                    merged[k] = v.strip()
    except Exception:
        pass
    return merged


@app.context_processor
def inject_constants():
    settings = {}
    try:
        settings = db.get_all_settings()
    except Exception:
        pass
    # 从已取回的 settings 直接派生来源列表/平台集合，避免 get_source_list()/get_platform_sources()
    # 动态选择列表：统一从 db.get_choices() 读取
    source_list = db.get_choices('source')
    raw_platforms = settings.get('platform_sources', '米画师,B站工坊,画加')
    platform_sources = {x.strip() for x in raw_platforms.split(',') if x.strip()} \
        or {'米画师', 'B站工坊', '画加'}
    return {
        'STAGE_CHOICES': db.get_choices('stage'),
        'SOURCE_CHOICES': source_list,
        'DDL_CHOICES': db.get_choices('ddl'),
        'PAYMENT_CHOICES': db.get_choices('payment'),
        'COMMISSION_TYPE_CHOICES': db.get_choices('commission_type'),
        'STAGE_CLASS_MAP': STAGE_CLASS_MAP,
        'PLATFORM_SOURCES': platform_sources,
        'theme_css': _build_theme_css(settings),
        'settings': settings,
        'shortcuts': merge_shortcuts(settings),
        'SHORTCUT_LABELS': SHORTCUT_LABELS,
    }


# ═══════════════════════════════════════════════════════════
# 页面路由
# ═══════════════════════════════════════════════════════════

@app.route('/')
def dashboard():
    start_date = request.args.get('from', '')
    end_date = request.args.get('to', '')
    if _reject_bad_range(start_date, end_date):
        return '日期格式须为 YYYY-MM-DD', 400
    preset = request.args.get('preset') or ('custom' if (start_date or end_date) else 'month')
    stats = db.get_dashboard_stats(start_date=start_date or None, end_date=end_date or None, preset=preset)
    gantt_orders = db.get_orders_for_gantt()
    # P19-F11：本周排单无分页 UI，显式全量（per_page<=0）防默认 30 静默截断
    week_orders = db.list_orders({'week': 'current', 'per_page': 0})
    overdue_orders = db.get_overdue_orders()
    top_customers = db.get_top_customers(limit=5)

    return render_template('index.html',
                           stats=stats,
                           gantt_orders=gantt_orders,
                           week_orders=week_orders,
                           overdue_orders=overdue_orders,
                           top_customers=top_customers)


@app.route('/income')
def income_dashboard():
    year_str = request.args.get('year', '')
    year = int(year_str) if year_str.isdigit() else date.today().year
    years = db.get_available_years()

    monthly_stats = db.get_monthly_income_stats(year=year, months=12)
    monthly_projected = db.get_monthly_projected_income(year=year, months=12)
    cumulative = db.get_cumulative_annual_income(year=year)
    distribution = db.get_commission_type_distribution(year=year)
    top_customers = db.get_top_customers(limit=10)
    return render_template('income.html',
                           selected_year=year,
                           available_years=years,
                           monthly_stats=monthly_stats,
                           monthly_projected=monthly_projected,
                           cumulative_income=cumulative,
                           type_distribution=distribution,
                           top_customers=top_customers)


@app.route('/api/income/type-distribution')
def api_type_distribution():
    """品类分布 AJAX 接口 — 按 year/month 筛选"""
    year_str = request.args.get('year', '')
    year = int(year_str) if year_str.isdigit() else date.today().year
    month_str = request.args.get('month', '')
    month = int(month_str) if month_str.isdigit() else None
    distribution = db.get_commission_type_distribution(year=year, month=month)
    return jsonify(distribution)


@app.route('/calendar')
def calendar_view():
    unscheduled = db.get_unscheduled_orders()
    color_mode = request.args.get('color', 'source')  # 默认按来源着色
    # P13b F1 级联筛选：维度 → 值列表
    filter_options = {
        'stage': db.get_choices('stage'),
        'source': db.get_choices('source'),
        'commission_type': db.get_choices('commission_type'),
        'customer': [{'id': c['id'], 'name': c['name']} for c in db.list_customers()],
        'payment_status': db.get_choices('payment_status'),
    }
    return render_template('calendar.html', unscheduled=unscheduled, color_mode=color_mode,
                           filter_options=filter_options)


@app.route('/orders')
def orders_list():
    filters = {
        'stage': request.args.get('stage'),
        'source': request.args.get('source'),
        'status': request.args.get('status'),
        'search': request.args.get('search'),
        'archived': request.args.get('archived', '0') == '1',
        'page': request.args.get('page', '1'),
        'per_page': request.args.get('per_page', '30'),
        'sort': request.args.get('sort'),
        'dir': request.args.get('dir'),
    }
    # 移除 None 值
    filters = {k: v for k, v in filters.items() if v is not None and v != ''}
    if 'archived' not in filters:
        filters['archived'] = False

    orders = db.list_orders(filters)

    # 提取分页信息（过滤掉无订单记录的空上下文）
    pagination = {'total': 0, 'page': 1, 'per_page': 30}
    if orders:
        first = orders[0]
        pagination = {
            'total': first.get('_total', len(orders)),
            'page': first.get('_page', 1),
            'per_page': first.get('_per_page', 30),
        }
        # 清理内部字段
        for o in orders:
            o.pop('_total', None)
            o.pop('_page', None)
            o.pop('_per_page', None)

    # HTMX 请求 → 只返回表格行
    if request.headers.get('HX-Request'):
        return render_template('partials/order_rows.html', orders=orders)

    return render_template('orders/list.html', orders=orders, filters=filters, pagination=pagination)


@app.route('/orders/kanban')
def kanban_view():
    # P19-F11：看板无分页 UI，显式全量（per_page<=0）防默认 30 静默截断丢单
    orders = db.list_orders({'archived': False, 'per_page': 0})
    stages = db.get_choices('stage')
    columns = {stage: [] for stage in stages}
    for o in orders:
        stage = o['current_stage']
        if stage in columns:
            columns[stage].append(o)
    return render_template('orders/kanban.html', columns=columns)


@app.route('/orders/<int:order_id>')
def order_detail(order_id):
    order = db.get_order(order_id)
    if not order:
        return "订单不存在", 404
    _calc_pct_for_display(order)
    customer = db.get_customer(order['customer_id']) if order['customer_id'] else None
    images = db.get_order_images(order_id)
    return render_template('orders/detail.html', order=order, customer=customer, images=images)


def _get_existing_values():
    """获取已有的来源和品类列表（供 datalist 使用）"""
    # 来源优先从设置读取
    sources = db.get_source_list()
    # 也合并数据库中已有的来源
    conn = db.get_db()
    db_sources = [r[0] for r in conn.execute(
        "SELECT DISTINCT source FROM orders WHERE source IS NOT NULL AND source != '' ORDER BY source"
    ).fetchall()]
    for s in db_sources:
        if s not in sources:
            sources.append(s)
    types = [r[0] for r in conn.execute(
        "SELECT DISTINCT commission_type FROM orders WHERE commission_type IS NOT NULL AND commission_type != '' ORDER BY commission_type"
    ).fetchall()]
    # 合并设置中预设的稿件类别
    try:
        raw_types = db.get_all_settings().get('commission_type_list', '')
        preset_types = [x.strip() for x in raw_types.split(',') if x.strip()]
        for t in preset_types:
            if t not in types:
                types.append(t)
    except Exception:
        pass
    conn.close()
    return sources, types


def _calc_pct_for_display(order):
    """表单显示用 pct：P19-F9 起优先订单快照列；快照缺失（NULL/无列）时按 fee/income 反算兜底。"""
    if order and order.get('platform_fee_pct') is not None:
        order['platform_fee_pct'] = float(order['platform_fee_pct'] or 0)
    elif order and order.get('income', 0) > 0:
        order['platform_fee_pct'] = round(order['platform_fee'] / order['income'] * 100, 1)
    else:
        order['platform_fee_pct'] = 0.0


@app.route('/orders/new')
def order_new():
    """新建订单 — 统一返回模态框表单（P18-F3：支持 ?template=<id> 预填）"""
    customers = db.list_customers()
    sources, types = _get_existing_values()
    templates = db.list_order_templates()
    prefill = {}
    tid = request.args.get('template')
    if tid:
        try:
            tpl = db.get_order_template(int(tid))
            if tpl:
                prefill = tpl.get('data') or {}
        except (TypeError, ValueError):
            pass
    return render_template('orders/form_modal.html', order=None, customers=customers,
                           existing_sources=sources, existing_types=types, is_new=True,
                           templates=templates, prefill=prefill)


@app.route('/orders/templates')
def order_templates():
    """模板管理 — 居中模态框片段（侧边栏「模板管理」入口）。"""
    templates = db.list_order_templates()
    return render_template('orders/templates_modal.html', templates=templates)


@app.route('/orders/templates/<int:template_id>/delete', methods=['POST'])
def order_template_delete(template_id):
    """删除模板，返回刷新后的列表片段（供管理模态框 HTMX 局部替换；
    form_modal 的 deleteTpl() 忽略响应体，同一路由兼顾两处）。"""
    db.delete_order_template(template_id)
    return render_template('orders/templates_list.html', templates=db.list_order_templates())


@app.route('/orders/templates/list')
def order_templates_list():
    """模板列表片段（供表单取消/删改后回到列表视图）。"""
    return render_template('orders/templates_list.html', templates=db.list_order_templates())


@app.route('/orders/templates/new')
def order_template_new():
    """新建模板表单片段——独立于订单，不写入 orders 表。"""
    return render_template('orders/templates_form.html', tpl=None,
                           customers=db.list_customers(), is_new=True)


@app.route('/orders/templates/<int:template_id>/edit')
def order_template_edit(template_id):
    """编辑模板表单片段（预填名称 + 字段快照）。"""
    tpl = db.get_order_template(template_id)
    if not tpl:
        return "模板不存在", 404
    return render_template('orders/templates_form.html', tpl=tpl,
                           customers=db.list_customers(), is_new=False)


@app.route('/orders/templates', methods=['POST'])
def order_template_create():
    """创建独立模板（仅写 order_templates，不创建订单），返回刷新后的列表片段。"""
    name = (request.form.get('name', '') or '').strip()
    if name:
        db.create_order_template(name, db._snapshot_template_data(request.form))
    return render_template('orders/templates_list.html', templates=db.list_order_templates())


@app.route('/orders/templates/<int:template_id>', methods=['POST'])
def order_template_update(template_id):
    """全量更新模板（名称 + 字段快照），返回刷新后的列表片段。"""
    name = (request.form.get('name', '') or '').strip()
    if name:
        db.update_order_template(template_id, name, db._snapshot_template_data(request.form))
    return render_template('orders/templates_list.html', templates=db.list_order_templates())


@app.route('/orders/<int:order_id>/edit')
def order_edit(order_id):
    order = db.get_order(order_id)
    if not order:
        return "订单不存在", 404
    _calc_pct_for_display(order)
    customers = db.list_customers()
    sources, types = _get_existing_values()
    inline = request.args.get('inline') == '1'
    modal = request.args.get('modal') == '1'
    images = db.get_order_images(order_id)
    if modal:
        return render_template('orders/form_modal_edit.html', order=order, customers=customers,
                               existing_sources=sources, existing_types=types, is_new=False,
                               images=images)
    template = 'orders/form_inline.html' if inline else 'orders/form.html'
    return render_template(template, order=order, customers=customers,
                           existing_sources=sources, existing_types=types, is_new=False,
                           images=images)


@app.route('/customers')
def customers_list():
    search = request.args.get('search')
    sort = request.args.get('sort')
    direction = request.args.get('dir')
    customers = db.list_customers(search=search, sort=sort, direction=direction)
    return render_template('customers/list.html', customers=customers, search=search,
                           sort=sort, direction=direction)


@app.route('/customers/<int:customer_id>')
def customer_detail(customer_id):
    customer = db.get_customer(customer_id)
    if not customer:
        return "客户不存在", 404
    orders = db.get_customer_orders(customer_id)
    images = db.get_customer_images(customer_id)
    return render_template('customers/detail.html', customer=customer, orders=orders, images=images)


@app.route('/customers/new')
def customer_new():
    return render_template('customers/form.html', customer=None, is_new=True)


@app.route('/customers/<int:customer_id>/edit')
def customer_edit(customer_id):
    customer = db.get_customer(customer_id)
    if not customer:
        return "客户不存在", 404
    return render_template('customers/form.html', customer=customer, is_new=False)


@app.route('/gallery')
def gallery_view():
    """画廊视图 — 瀑布流展示有图片的订单（P13c 起图片经 /api/gallery 分批加载）"""
    stages = db.get_choices('stage')
    sources = db.get_choices('source')
    types = db.get_choices('commission_type')
    customers = db.list_customers()
    return render_template('gallery.html', stages=stages,
                           sources=sources, types=types, customers=customers)


@app.route('/api/gallery')
def api_gallery():
    """画廊分页接口（P13c）：offset/limit 分批 + 4 个筛选参数服务端化"""
    filters = {
        'stage': request.args.get('stage'),
        'source': request.args.get('source'),
        'customer': request.args.get('customer'),
        'type': request.args.get('type'),
    }
    filters = {k: v for k, v in filters.items() if v}
    try:
        offset = max(0, int(request.args.get('offset', 0)))
        limit = min(100, max(1, int(request.args.get('limit', 24))))
    except (TypeError, ValueError):
        offset, limit = 0, 24
    items, total = db.list_gallery_page(filters, offset=offset, limit=limit)
    return jsonify({
        'items': [{
            'id': o['id'],
            'img': o['image_url'],
            'full': ('/uploads/' + o['image_path']) if o['image_path'] else o['image_url'],
            'title': o['project_name'],
            'customer': o['customer_name'],
            'stage': o['current_stage'],
            'income': o['income'],
        } for o in items],
        'total': total,
        'offset': offset,
        'limit': limit,
    })


# ═══════════════════════════════════════════════════════════
# 数据操作路由 (POST)
# ═══════════════════════════════════════════════════════════

def _needs_overdue_archive_confirm(order):
    """P18-F7：订单排单截止日期已过（today > scheduled_end）且未归档，
    则保存后应弹出归档确认（等同点击 A2「归档」按钮），复用 P15b 逾期确认链。

    不限制 current_stage：任何阶段的订单只要截止日期早于当前时间且未归档，
    点「保存修改」（A1）即触发归档确认弹窗（A2 效果），由用户决定是否归档。

    与「完成+已结清静默归档」语义并存：已归档(is_archived==1)不再提示。
    """
    if not order:
        return False
    if order.get('is_archived'):
        return False
    ed = order.get('scheduled_end')
    if not ed:
        return False
    try:
        return date.today() > date.fromisoformat(ed)
    except (TypeError, ValueError):
        return False


@app.post('/orders')
def create_order():
    form_data = dict(request.form)
    is_modal = form_data.pop('modal', None) == '1'
    # customer_id 空字符串 → None
    if form_data.get('customer_id', '').strip() == '':
        form_data['customer_id'] = None
    # platform_fee_pct 空字符串 → None
    if form_data.get('platform_fee_pct', '').strip() == '':
        form_data['platform_fee_pct'] = None
    # 不从前端接收 is_repeat / repeat_count（后台自动算）
    form_data.pop('is_repeat', None)
    form_data.pop('repeat_count', None)
    try:
        data = OrderCreate(**form_data).model_dump()
    except ValidationError as e:
        return f"数据校验失败: {e.errors()}", 400

    # 转换 bool → int
    data['is_commercial'] = 1 if data['is_commercial'] else 0

    # P19-F5：创建订单（+可选模板保存）单事务，全成或全败
    save_as_template = request.form.get('save_as_template') in ('1', 'on', 'true')
    template_name = (request.form.get('template_name', '') or '').strip()
    if save_as_template and template_name:
        order_id = db.create_order_with_template(
            data, template_name, db._snapshot_template_data(request.form))
    else:
        order_id = db.create_order(data)

    # P18-F7：保存后判定是否需过期归档确认
    saved = db.get_order(order_id)
    needs_confirm = _needs_overdue_archive_confirm(saved)

    if is_modal:
        if needs_confirm:
            return render_template('partials/archive_overdue_confirm.html', order=saved, now_month=date.today().strftime('%Y-%m'))
        # 返回成功页面并触发关闭
        project_name_escaped = data['project_name'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
        return render_template('partials/modal_success.html', order_id=order_id, project_name=project_name_escaped)

    if needs_confirm:
        return redirect(url_for('order_detail', order_id=order_id, archive_confirm=1))
    return redirect(url_for('order_detail', order_id=order_id))


@app.post('/orders/<int:order_id>/edit')
def update_order(order_id):
    form_data = dict(request.form)
    is_inline = form_data.pop('inline', None) == '1'
    is_modal = form_data.pop('modal', None) == '1'

    # customer_id 空字符串 → None
    if form_data.get('customer_id', '').strip() == '':
        form_data['customer_id'] = None
    # platform_fee_pct 空字符串 → None
    if form_data.get('platform_fee_pct', '').strip() == '':
        form_data['platform_fee_pct'] = None
    # 不从前端接收 is_repeat / repeat_count
    form_data.pop('is_repeat', None)
    form_data.pop('repeat_count', None)

    try:
        data = OrderUpdate(**form_data).model_dump()
    except ValidationError as e:
        return f"数据校验失败: {e.errors()}", 400

    # 转换 bool → int（is_archived 为 None 时不写入，保留现有归档状态）
    if 'is_commercial' in data:
        data['is_commercial'] = 1 if data['is_commercial'] else 0
    if data.get('is_archived') is not None:
        data['is_archived'] = 1 if data['is_archived'] else 0
    else:
        data.pop('is_archived', None)

    # 移除 None 值，但保留 customer_id / platform_fee_pct（None 表示清空/无手续费，需传到 db 层）
    data = {k: v for k, v in data.items() if v is not None or k in ('customer_id', 'platform_fee_pct')}

    db.update_order(order_id, data)

    # P18-F7：保存后判定是否需过期归档确认
    saved = db.get_order(order_id)
    needs_confirm = _needs_overdue_archive_confirm(saved)

    # 如果是内联编辑，返回刷新触发器
    if is_inline:
        resp = jsonify({'success': True, 'order_id': order_id})
        if needs_confirm:
            # 携带 archiveConfirm 让前端在抽屉关闭后打开归档确认
            resp.headers['HX-Trigger'] = json.dumps({'orderUpdated': {'archiveConfirm': order_id}})
        else:
            resp.headers['HX-Trigger'] = 'orderUpdated'
        return resp

    # 如果是模态框编辑，关闭弹窗并刷新
    if is_modal:
        if needs_confirm:
            return render_template('partials/archive_overdue_confirm.html', order=saved, now_month=date.today().strftime('%Y-%m'))
        return """<div style="text-align:center;padding:32px;">
            <div style="font-size:3rem;margin-bottom:16px;">✓</div>
            <h2 style="margin-bottom:8px;">订单已更新</h2>
            <p style="color:var(--color-text-secondary);margin-bottom:24px;">#{}</p>
            <script>setTimeout(function(){closeCenterModal();}, 800);</script>
            <button class="btn btn-primary" onclick="closeCenterModal()">关闭</button>
        </div>""".replace('#{}', f'#{order_id}')

    if needs_confirm:
        return redirect(url_for('order_detail', order_id=order_id, archive_confirm=1))
    return redirect(url_for('order_detail', order_id=order_id))


@app.post('/orders/<int:order_id>/delete')
def delete_order(order_id):
    db.delete_order(order_id)
    if request.headers.get('HX-Request'):
        resp = jsonify({'success': True})
        resp.headers['HX-Trigger'] = 'orderDeleted'
        return resp
    return redirect(url_for('orders_list'))


@app.post('/orders/<int:order_id>/archive')
def archive_order(order_id):
    """归档/取消归档。P15b：归档为完成时按排单区间做时间判断 + 两阶段确认。"""
    order = db.get_order(order_id)
    if not order:
        return jsonify({'error': '订单不存在'}), 404

    # 取消归档方向：直接 toggle，清空 completed_at/is_overdue
    if order['is_archived']:
        db.archive_order(order_id)
        return _archive_done_response(order_id)

    confirm = request.args.get('confirm')
    today = date.today()
    # P19-F6：库内日期脏数据容错（非法按 None 处理，不再 500）
    sd = _iso_or_none(order.get('scheduled_start'))
    ed = _iso_or_none(order.get('scheduled_end'))

    # 辅助：判断排单截止月与当前月是否不同
    def _month_mismatch():
        if not ed:
            return False
        return (ed.year, ed.month) != (today.year, today.month)

    if confirm == 'early_yes':
        db.archive_order(order_id)                                 # completed_at=now, is_overdue=0
    elif confirm == 'overdue_yes':
        db.archive_order(order_id, is_overdue=1)                   # completed_at=now, 逾期
    elif confirm == 'overdue_import':
        db.archive_order(order_id, completed_at=order['scheduled_end'])  # 按预设截止日期
    elif confirm == 'month_scheduled':
        db.archive_order(order_id, completed_at=order['scheduled_end'])  # 按排单截止月统计
    elif confirm == 'month_current':
        db.archive_order(order_id)                                 # 按当前月统计（completed_at=now）
    else:
        # 无 confirm：后端主导时间判断
        if sd and today < sd:
            return render_template('partials/archive_early_confirm.html', order=order)
        if ed and today > ed:
            now_month = today.strftime('%Y-%m')
            return render_template('partials/archive_overdue_confirm.html', order=order, now_month=now_month)
        # 区间内（或缺失排期）：检查月份是否一致
        if ed and _month_mismatch():
            now_month = today.strftime('%Y-%m')
            return render_template('partials/archive_month_confirm.html', order=order, now_month=now_month)
        db.archive_order(order_id)                                 # 区间内且月份一致 → 直接归档
    return _archive_done_response(order_id)


def _archive_done_response(order_id):
    """归档/取消归档落库后返回成功片段（渲染进 #center-modal-body）。"""
    order = db.get_order(order_id)
    return render_template('partials/archive_done.html', order=order)


@app.post('/orders/<int:order_id>/stage')
def update_order_stage(order_id):
    new_stage = request.form['stage']
    if new_stage not in db.get_choices('stage'):
        return jsonify({'error': f'无效阶段: {new_stage}'}), 400
    db.update_stage(order_id, new_stage)
    order = db.get_order(order_id)
    if not order:
        return jsonify({'error': '订单不存在'}), 404
    return render_template('partials/kanban_card.html', order=order)


@app.post('/orders/<int:order_id>/reschedule')
def reschedule_order(order_id):
    data = request.get_json()
    start = data.get('start', '')
    end = data.get('end', start)
    db.reschedule_order(order_id, start, end)
    return jsonify({'success': True})


@app.post('/orders/<int:order_id>/unschedule')
def unschedule_order(order_id):
    """清除订单排期日期，将订单移回未排期池"""
    db.reschedule_order(order_id, None, None)
    return jsonify({'success': True})


@app.post('/orders/<int:order_id>/color')
def set_order_color(order_id):
    """设置订单在日历中的自定义颜色"""
    data = request.get_json()
    color = data.get('color', None)
    db.set_order_custom_color(order_id, color)
    return jsonify({'success': True})




@app.post('/orders/batch')
def batch_update_orders():
    """批量更新订单"""
    ids_str = request.form.get('ids', '')
    action = request.form.get('action', '')
    value = request.form.get('value', '')

    if not ids_str:
        return jsonify({'success': False, 'error': '未选择订单'}), 400

    ids = [int(x) for x in ids_str.split(',') if x.strip().isdigit()]
    if not ids:
        return jsonify({'success': False, 'error': '无效的ID列表'}), 400

    count = 0
    try:
        if action == 'stage':
            if value in db.get_choices('stage'):
                count = db.batch_update_stage(ids, value)          # P19-F5 整批单事务
        elif action == 'archive':
            count = db.batch_set_archived(ids, value == '1')       # P19-F1 唯一入口 + F5 单事务
        elif action == 'delete':
            count = db.batch_delete_orders(ids)
        elif action == 'source':
            count = db.batch_update_field(ids, {'source': value})
        elif action == 'payment':
            if value in db.get_choices('payment'):
                count = db.batch_update_field(ids, {'payment_status': value})
        elif action == 'ddl':
            if value in db.get_choices('ddl'):
                count = db.batch_update_field(ids, {'ddl_status': value})
        elif action == 'commercial':
            count = db.batch_update_field(ids, {'is_commercial': 1 if value == '1' else 0})
        elif action == 'recompute':
            # P17a 按最新算法重算并保存派生状态（不改用户手填内容）
            count = db.batch_recompute_orders(ids)
    except Exception as e:
        return jsonify({'success': False, 'error': f'批量操作失败已回滚: {e}'}), 500

    return jsonify({'success': True, 'count': count})


@app.post('/customers')
def create_customer():
    try:
        data = CustomerCreate(**request.form).model_dump()
    except ValidationError as e:
        return f"数据校验失败: {e.errors()}", 400
    _normalize_customer_tags(data)
    customer_id = db.create_customer(data)
    return redirect(url_for('customer_detail', customer_id=customer_id))


@app.post('/customers/<int:customer_id>/edit')
def update_customer(customer_id):
    try:
        data = CustomerUpdate(**request.form).model_dump()
    except ValidationError as e:
        return f"数据校验失败: {e.errors()}", 400
    data = {k: v for k, v in data.items() if v is not None and v != ''}
    _normalize_customer_tags(data)
    db.update_customer(customer_id, data)
    return redirect(url_for('customer_detail', customer_id=customer_id))


def _normalize_customer_tags(data: dict) -> None:
    """标签规范化：中文逗号统一换成英文逗号，去除各标签前后空格与空项，与表单提示一致"""
    if data.get('tags'):
        data['tags'] = ','.join(
            t.strip() for t in data['tags'].replace('，', ',').split(',') if t.strip()
        )


@app.post('/customers/<int:customer_id>/delete')
def delete_customer(customer_id):
    db.delete_customer(customer_id)
    return redirect(url_for('customers_list'))


@app.post('/customers/batch')
def batch_update_customers():
    """批量更新客户"""
    ids_str = request.form.get('ids', '')
    action = request.form.get('action', '')
    value = request.form.get('value', '')

    if not ids_str:
        return jsonify({'success': False, 'error': '未选择客户'}), 400

    ids = [int(x) for x in ids_str.split(',') if x.strip().isdigit()]
    if not ids:
        return jsonify({'success': False, 'error': '无效的ID列表'}), 400

    count = 0
    skipped = 0
    for cid in ids:
        try:
            if action == 'delete':
                # 检查是否有关联订单
                orders = db.get_customer_orders(cid)
                if orders and len(orders) > 0:
                    skipped += 1
                    continue
                db.delete_customer(cid)
                count += 1
        except Exception:
            pass

    msg = f'已删除 {count} 位客户'
    if skipped > 0:
        msg += f'，跳过 {skipped} 位（有关联订单）'
    return jsonify({'success': True, 'count': count, 'skipped': skipped, 'message': msg})


# ═══════════════════════════════════════════════════════════
# 图片上传 / 移除 / 静态服务
# ═══════════════════════════════════════════════════════════

@app.post('/orders/<int:order_id>/upload-image')
def upload_order_image(order_id):
    """上传订单图片（P15d 多图）：追加一条 order_images 记录，首图回填 orders 封面三列。"""
    order = db.get_order(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    if 'image' not in request.files:
        return jsonify({'success': False, 'error': '未选择文件'}), 400
    file = request.files['image']
    if not file or not file.filename:
        return jsonify({'success': False, 'error': '空文件'}), 400

    # 检查文件类型
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        return jsonify({'success': False, 'error': f'不支持的格式: {ext}'}), 400

    # 检查文件大小
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > MAX_UPLOAD_SIZE:
        return jsonify({'success': False, 'error': f'文件过大 ({size // 1024 // 1024}MB)，最大 10MB'}), 400

    img_key = uuid.uuid4().hex[:12]
    try:
        try:
            result = process_uploaded_file_multi(file, order_id, UPLOAD_ORDERS_DIR, img_key)
        except ImportError:
            # Pillow 未安装，回退到直接保存（仍返回多图记录）
            result = save_without_pillow(file, order_id, UPLOAD_ORDERS_DIR)
            result.setdefault('thumb_url', result['image_url'])
            result.setdefault('original_url', result['image_url'])

        image_id = db.add_order_image(order_id, result['image_url'], result['image_path'])

        # 首图（当前订单尚无封面）回填 orders 封面三列，保证画廊卡/列表缩略图不回归
        if not order.get('has_image'):
            db.update_order(order_id, {
                'image_url': result['image_url'],
                'image_path': result['image_path'],
                'has_image': 1,
            })

        return jsonify({
            'success': True,
            'image_id': image_id,
            'url': result['image_url'],
            'thumb_url': result.get('thumb_url'),
            'original_url': result.get('original_url'),
            'image_path': result['image_path'],
        })

    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    except Exception as e:
        logger.error("订单 #%d 图片上传失败: %s", order_id, e)
        return jsonify({'success': False, 'error': f'图片处理失败: {str(e)}'}), 500


def _refresh_order_cover(order_id):
    """删图后重新指向封面：有剩余图则取首图，否则清空封面三列。"""
    remaining = db.get_order_images(order_id)
    if remaining:
        first = remaining[0]
        db.update_order(order_id, {
            'image_url': first['image_url'],
            'image_path': first['image_path'],
            'has_image': 1,
        })
    else:
        db.update_order(order_id, {'image_url': None, 'image_path': None, 'has_image': 0})


@app.post('/orders/<int:order_id>/remove-image')
def remove_order_image(order_id):
    """移除订单图片（P15d）：带 image_id 时精确删除单张，否则回退删除该订单全部图片。"""
    order = db.get_order(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404

    image_id = request.form.get('image_id') or request.args.get('image_id')

    if image_id:
        # 单张删除：删 DB 记录 + 磁盘子目录
        try:
            image_id = int(image_id)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'image_id 无效'}), 400
        rec = db.get_order_image(image_id)
        if not rec or rec['order_id'] != order_id:
            return jsonify({'success': False, 'error': '图片不存在'}), 404
        db.remove_order_image(image_id)
        # 删除该图片子目录 orders/<id>/imgs/<key>/
        if rec.get('image_path'):
            img_dir = os.path.dirname(os.path.join(UPLOAD_DIR, rec['image_path']))
            if os.path.isdir(img_dir):
                shutil.rmtree(img_dir, ignore_errors=True)
        _refresh_order_cover(order_id)
        return jsonify({'success': True, 'image_id': image_id})

    # 无 image_id：回退为清空整个订单图片（兼容旧单图调用）
    for rec in db.get_order_images(order_id):
        db.remove_order_image(rec['id'])
    order_dir = os.path.join(UPLOAD_ORDERS_DIR, str(order_id))
    if os.path.isdir(order_dir):
        shutil.rmtree(order_dir, ignore_errors=True)
    # 兼容旧版平铺文件
    image_url = order.get('image_url', '')
    if image_url:
        filename = os.path.basename(image_url)
        old_path = os.path.join(UPLOAD_DIR, filename)
        if os.path.exists(old_path):
            os.remove(old_path)
    db.update_order(order_id, {'image_url': None, 'image_path': None, 'has_image': 0})
    return jsonify({'success': True})


@app.route('/uploads/<path:filename>')
def serve_upload(filename):
    """提供上传文件的访问（支持结构化目录）"""
    return send_from_directory(UPLOAD_DIR, filename)


# ═══════════════════════════════════════════════════════════
# API / HTMX Partial 路由
# ═══════════════════════════════════════════════════════════

@app.route('/api/stats')
def api_stats():
    start_date = request.args.get('from', '')
    end_date = request.args.get('to', '')
    if _reject_bad_range(start_date, end_date):
        return '日期格式须为 YYYY-MM-DD', 400
    preset = request.args.get('preset') or ('custom' if (start_date or end_date) else 'month')
    stats = db.get_dashboard_stats(start_date=start_date or None, end_date=end_date or None, preset=preset)
    return render_template('partials/stats_cards.html', stats=stats)


@app.route('/api/stats/detail')
def api_stats_detail():
    """统计卡明细（小票弹窗数据源）"""
    metric = request.args.get('metric', '')
    start_date = request.args.get('from', '')
    end_date = request.args.get('to', '')
    if _reject_bad_range(start_date, end_date):
        return jsonify({'error': '日期格式须为 YYYY-MM-DD'}), 400
    year_str = request.args.get('year', '')
    month_str = request.args.get('month', '')
    year = int(year_str) if year_str.isdigit() else None
    month = int(month_str) if month_str.isdigit() else None
    result = db.get_stats_detail(metric, start_date=start_date or None,
                                 end_date=end_date or None, year=year, month=month)
    return jsonify(result)


@app.route('/api/orders')
def api_orders():
    filters = {
        'stage': request.args.get('stage'),
        'source': request.args.get('source'),
        'status': request.args.get('status'),
        'search': request.args.get('search'),
        'end_from': request.args.get('end_from'),  # P16e 结束时间范围（起）
        'end_to': request.args.get('end_to'),      # P16e 结束时间范围（止）
        'archived': request.args.get('archived', '0') == '1',
        'sort': request.args.get('sort'),
        'dir': request.args.get('dir'),
    }
    filters = {k: v for k, v in filters.items() if v is not None and v != ''}
    if 'archived' not in filters:
        filters['archived'] = False
    orders = db.list_orders(filters)
    return render_template('partials/order_rows.html', orders=orders)


@app.post('/api/customers/quick')
def api_quick_create_customer():
    """快速创建客户（订单表单内联），返回更新后的下拉选项"""
    name = request.form.get('quick_name', '').strip()
    if not name:
        return '<span style="color:var(--color-danger);font-size:0.82rem;">请输入客户名称</span>', 400

    cid = db.create_customer({
        'name': name,
        'platform_url': request.form.get('quick_platform', ''),
        'preferences': request.form.get('quick_preferences', ''),
        'notes': request.form.get('quick_notes', ''),
    })

    customers = db.list_customers()
    # 返回 select 并将新客户设为选中
    return render_template('partials/customer_select.html',
                           customers=customers, selected_id=cid)


@app.route('/api/orders/gantt-data')
def api_gantt_data():
    return jsonify(db.get_orders_for_gantt())


def _calendar_filters_from_args():
    """P13b F1：从 query string 读取级联筛选参数"""
    keys = ('stage', 'source', 'commission_type', 'customer_id', 'payment_status')
    return {k: v for k in keys if (v := request.args.get(k))}


@app.route('/api/orders/calendar-events')
def api_calendar_events():
    color_mode = request.args.get('color', 'source')  # 默认按来源着色
    show_archived = request.args.get('show_archived') == '1'  # P16d 归档显隐开关
    return jsonify(db.get_orders_for_calendar(color_mode=color_mode,
                                              filters=_calendar_filters_from_args(),
                                              show_archived=show_archived))


@app.route('/api/orders/unscheduled')
def api_unscheduled():
    show_archived = request.args.get('show_archived') == '1'  # P16d 与日历开关同步
    unscheduled = db.get_unscheduled_orders(filters=_calendar_filters_from_args(),
                                            show_archived=show_archived)
    return render_template('partials/unscheduled_pool.html', unscheduled=unscheduled)


# ═══════════════════════════════════════════════════════════
# 主题设置
# ═══════════════════════════════════════════════════════════

# 预设主题（内联，源自 git 096f69c settings_service.py；按任务卡 16 扁平化结论不建 app/services/）
PRESET_THEMES = {
    'notion': {
        'label': 'Notion 默认',
        'desc': '温暖纸感 · 经典默认',
        'preview': ['#f9f9f7', '#0b0b0b', '#2a78d6', '#0ca30c', '#fab219', '#d03b3b'],
        'colors': {
            'theme_bg': '#f9f9f7', 'theme_surface': '#fcfcfb', 'theme_sidebar': '#f5f4f1',
            'theme_text': '#0b0b0b', 'theme_text_secondary': '#52514e', 'theme_border': '#e1e0d9',
            'theme_accent': '#0b0b0b', 'theme_link': '#2a78d6',
            'theme_success': '#0ca30c', 'theme_warning': '#fab219', 'theme_danger': '#d03b3b',
            'stage_pending': '#898781', 'stage_sketch': '#2a78d6', 'stage_lineart': '#1baf7a',
            'stage_detail': '#eda100', 'stage_finish': '#4a3aa7', 'stage_completed': '#008300',
            'stage_cancelled': '#e34948',
        },
    },
    'premium': {
        'label': 'Premium',
        'desc': 'Apple 风格 · 精致简洁',
        'preview': ['#f5f5f7', '#1d1d1f', '#0071e3', '#30d158', '#ff9f0a', '#ff3b30'],
        'colors': {
            'theme_bg': '#f5f5f7', 'theme_surface': '#ffffff', 'theme_sidebar': '#efeff1',
            'theme_text': '#1d1d1f', 'theme_text_secondary': '#6e6e73', 'theme_border': '#e8e8ed',
            'theme_accent': '#0071e3', 'theme_link': '#0071e3',
            'theme_success': '#30d158', 'theme_warning': '#ff9f0a', 'theme_danger': '#ff3b30',
            'stage_pending': '#86868b', 'stage_sketch': '#0071e3', 'stage_lineart': '#30d158',
            'stage_detail': '#ff9f0a', 'stage_finish': '#bf5af2', 'stage_completed': '#34c759',
            'stage_cancelled': '#ff3b30',
        },
    },
    'artistic': {
        'label': 'Artistic',
        'desc': '艺术冲击 · 珊瑚撞色',
        'preview': ['#fff9ec', '#0f0c06', '#ff4757', '#00b894', '#ffd23f', '#ff4757'],
        'colors': {
            'theme_bg': '#fff9ec', 'theme_surface': '#fbf8f4', 'theme_sidebar': '#ece4d6',
            'theme_text': '#0f0c06', 'theme_text_secondary': '#544830', 'theme_border': '#dcd1bf',
            'theme_accent': '#ff4757', 'theme_link': '#ff6b9d',
            'theme_success': '#00b894', 'theme_warning': '#ffd23f', 'theme_danger': '#ff4757',
            'stage_pending': '#7a6c52', 'stage_sketch': '#00d2ff', 'stage_lineart': '#00b894',
            'stage_detail': '#ffd23f', 'stage_finish': '#a66cff', 'stage_completed': '#00a77d',
            'stage_cancelled': '#ff4757',
        },
    },
    'codex': {
        'label': 'Codex Dark',
        'desc': '纯黑终端 · 极简代码',
        'preview': ['#000000', '#ededed', '#ffffff', '#4ade80', '#fbbf24', '#f87171'],
        'colors': {
            'theme_bg': '#000000', 'theme_surface': '#0d0d0d', 'theme_sidebar': '#000000',
            'theme_text': '#ededed', 'theme_text_secondary': '#888888', 'theme_border': '#2a2a2a',
            'theme_accent': '#ffffff', 'theme_link': '#60a5fa',
            'theme_success': '#4ade80', 'theme_warning': '#fbbf24', 'theme_danger': '#f87171',
            'stage_pending': '#888888', 'stage_sketch': '#60a5fa', 'stage_lineart': '#2dd4bf',
            'stage_detail': '#fbbf24', 'stage_finish': '#a78bfa', 'stage_completed': '#4ade80',
            'stage_cancelled': '#f87171',
        },
    },
    'doodle': {
        'label': 'Doodle',
        'desc': '手绘涂鸦 · 粉嫩蜡笔',
        'preview': ['#fffbeb', '#2a2a2a', '#ff6b9d', '#6bcb77', '#ff8b3d', '#ff5c5c'],
        'colors': {
            'theme_bg': '#fffbeb', 'theme_surface': '#fffce6', 'theme_sidebar': '#f7f2d6',
            'theme_text': '#2a2a2a', 'theme_text_secondary': '#6b6548', 'theme_border': '#ece5c4',
            'theme_accent': '#ff6b9d', 'theme_link': '#4d96ff',
            'theme_success': '#6bcb77', 'theme_warning': '#ff8b3d', 'theme_danger': '#ff5c5c',
            'stage_pending': '#a69e75', 'stage_sketch': '#4d96ff', 'stage_lineart': '#6bcb77',
            'stage_detail': '#ffd93d', 'stage_finish': '#a66cff', 'stage_completed': '#4caf50',
            'stage_cancelled': '#ff5c5c',
        },
    },
    'dramatic': {
        'label': 'Dramatic',
        'desc': '戏剧舞台 · 朱砂金箔',
        'preview': ['#fff9ee', '#0e0a05', '#c7153a', '#1b9e5e', '#c9a227', '#c7153a'],
        'colors': {
            'theme_bg': '#fff9ee', 'theme_surface': '#fbf8f3', 'theme_sidebar': '#eadfc9',
            'theme_text': '#0e0a05', 'theme_text_secondary': '#5a5040', 'theme_border': '#d8cbae',
            'theme_accent': '#c7153a', 'theme_link': '#3d2e8f',
            'theme_success': '#1b9e5e', 'theme_warning': '#c9a227', 'theme_danger': '#c7153a',
            'stage_pending': '#7a6e55', 'stage_sketch': '#3d2e8f', 'stage_lineart': '#0d8d7a',
            'stage_detail': '#e8730d', 'stage_finish': '#c7153a', 'stage_completed': '#1b9e5e',
            'stage_cancelled': '#e22855',
        },
    },
    'vintage': {
        'label': 'Vintage',
        'desc': '怀旧老报 · 大地色调',
        'preview': ['#faf3e0', '#3e3322', '#d4a017', '#6b8e23', '#8b4513', '#a0452a'],
        'colors': {
            'theme_bg': '#faf3e0', 'theme_surface': '#faf3e0', 'theme_sidebar': '#e4d6b8',
            'theme_text': '#3e3322', 'theme_text_secondary': '#5e4f32', 'theme_border': '#d0be9a',
            'theme_accent': '#d4a017', 'theme_link': '#2f8b8b',
            'theme_success': '#6b8e23', 'theme_warning': '#8b4513', 'theme_danger': '#a0452a',
            'stage_pending': '#7e6b48', 'stage_sketch': '#2f8b8b', 'stage_lineart': '#6b8e23',
            'stage_detail': '#d4a017', 'stage_finish': '#8b4513', 'stage_completed': '#568203',
            'stage_cancelled': '#a0452a',
        },
    },
}

# 所有可自定义的颜色项
THEME_COLORS = [
    # (key, label, group)
    ('theme_bg', '页面背景', '全局'),
    ('theme_surface', '卡片/表面', '全局'),
    ('theme_sidebar', '侧边栏背景', '全局'),
    ('theme_text', '主文字色', '全局'),
    ('theme_text_secondary', '次要文字', '全局'),
    ('theme_border', '边框色', '全局'),
    ('theme_accent', '强调色', '全局'),
    ('theme_link', '链接色', '全局'),
    ('theme_success', '成功/绿色', '语义色'),
    ('theme_warning', '警告/橙色', '语义色'),
    ('theme_danger', '危险/红色', '语义色'),
    ('stage_pending', '阶段：待开始', '阶段色'),
    ('stage_sketch', '阶段：色稿', '阶段色'),
    ('stage_lineart', '阶段：线稿', '阶段色'),
    ('stage_detail', '阶段：细化', '阶段色'),
    ('stage_finish', '阶段：收尾', '阶段色'),
    ('stage_completed', '阶段：完成', '阶段色'),
    ('stage_cancelled', '阶段：退单', '阶段色'),
]


@app.route('/settings')
def settings_page():
    all_settings = db.get_all_settings()

    # 预设主题（T14e.2：比对当前主题颜色值判定激活预设，用于高亮）
    preset_themes = []
    for _pid, _p in PRESET_THEMES.items():
        _active = all(
            (all_settings.get(_k) or '').lower() == _v.lower()
            for _k, _v in _p['colors'].items()
        )
        preset_themes.append({
            'id': _pid,
            'label': _p['label'],
            'desc': _p['desc'],
            'preview': _p['preview'],
            'colors': _p['colors'],
            'active': _active,
        })

    # 全局主题色（保持现有分组）
    groups = {}
    for key, label, group in THEME_COLORS:
        groups.setdefault(group, []).append({
            'key': key,
            'label': label,
            'value': all_settings.get(key, ''),
        })

    # 日历着色模式 + 色板
    cal_modes = [
        {'id': 'source', 'name': '按来源', 'icon': '📡'},
        {'id': 'stage', 'name': '按阶段', 'icon': '🎨'},
        {'id': 'ddl', 'name': '按DDL状态', 'icon': '⏰'},
        {'id': 'payment', 'name': '按收款', 'icon': '💰'},
        {'id': 'commission', 'name': '按类别', 'icon': '📋'},
    ]
    # 每个 mode 的默认调色板
    default_palettes = db.CALENDAR_PALETTES

    cal_palettes = {}
    for mode in cal_modes:
        mid = mode['id']
        prefix = f'cal_{mid}_'
        items = []
        defaults = default_palettes.get(mid, {})
        for label, default_color in defaults.items():
            items.append({
                'key': f'{prefix}{label}',
                'label': label,
                'value': all_settings.get(f'{prefix}{label}', default_color),
                'default': default_color,
            })
        cal_palettes[mid] = items

    # 来源管理
    source_list = db.get_source_list()
    platform_sources = db.get_platform_sources()
    font_size = all_settings.get('font_size', '16px')
    font_family = all_settings.get('font_family', 'system')

    # P16j：自定义 CSS 主题（导入并存为预设）
    custom_themes = _load_custom_themes(all_settings)
    active_custom_theme = all_settings.get('active_custom_theme', '')
    theme_err = request.args.get('theme_err', '')

    return render_template('settings.html',
                           custom_themes=custom_themes,
                           active_custom_theme=active_custom_theme,
                           theme_err=theme_err,
                           preset_themes=preset_themes,
                           groups=groups,
                           cal_modes=cal_modes,
                           cal_palettes=cal_palettes,
                           source_list=source_list,
                           platform_sources=platform_sources,
                           font_size=font_size,
                           font_family=font_family,
                           all_settings=all_settings)


@app.post('/settings')
def save_settings():
    data = dict(request.form)

    # 来源管理
    if 'source_list_input' in data:
        data['source_list'] = data['source_list_input']
        del data['source_list_input']
    platform_keys = [k for k in data if k.startswith('platform_')]
    platform_vals = [data[k] for k in platform_keys]
    data['platform_sources'] = ','.join(platform_vals)
    for k in platform_keys:
        del data[k]

    # 重命名时同步更新 orders 表（收款状态已锁定，不需要同步）
    # P19-F5：重命名 + settings 更新包同一事务，中途失败整体回滚
    with db.transaction() as conn:
        db.sync_choice_renames(conn, data, 'commission_type_list', 'commission_type')
        renamed_src = db.sync_choice_renames(conn, data, 'source_list', 'source')
        db.update_settings(data, conn=conn)
        # P19-F9：来源重命名级联——新名称对应费率写入受影响订单快照并按快照重算（同事务）
        for _old, (_new, _ids) in renamed_src.items():
            db.resnapshot_fee_for_renamed_source(conn, _ids, _new)
    return redirect(url_for('settings_page'))


def _sync_choices_to_orders(data, settings_key, order_field):
    """兼容包装（P19-F5 起实现迁移至 db.sync_choice_renames；请优先在事务中调用后者）"""
    with db.transaction() as conn:
        return db.sync_choice_renames(conn, data, settings_key, order_field)


@app.post('/settings/reset')
def reset_settings_route():
    db.reset_settings()
    return redirect(url_for('settings_page'))


# ── P16j 自定义主题：导入 / 选用 / 删除 ──
@app.post('/settings/theme/import')
def import_custom_theme():
    """导入 CSS 并命名保存为自定义预设，保存后即选用。"""
    name = (request.form.get('theme_name') or '').strip()
    css_raw = request.form.get('theme_css') or ''
    if not name:
        return redirect(url_for('settings_page', theme_err='请填写主题名称'))
    css, err = _sanitize_theme_css(css_raw)
    if err:
        return redirect(url_for('settings_page', theme_err=err))
    settings = db.get_all_settings()
    themes = _load_custom_themes(settings)
    tid = 'custom_%d' % int(time.time() * 1000)
    themes.append({'id': tid, 'name': name, 'css': css})
    db.update_settings({
        'custom_themes': json.dumps(themes, ensure_ascii=False),
        'active_custom_theme': tid,
    })
    return redirect(url_for('settings_page'))


@app.post('/settings/theme/apply')
def apply_custom_theme():
    """选用一个已保存的自定义主题。"""
    tid = request.form.get('theme_id') or ''
    settings = db.get_all_settings()
    themes = _load_custom_themes(settings)
    if any(t.get('id') == tid for t in themes):
        db.update_settings({'active_custom_theme': tid})
    return redirect(url_for('settings_page'))


@app.post('/settings/theme/delete')
def delete_custom_theme():
    """删除一个自定义主题（不影响内置预设）。"""
    tid = request.form.get('theme_id') or ''
    settings = db.get_all_settings()
    themes = [t for t in _load_custom_themes(settings) if t.get('id') != tid]
    updates = {'custom_themes': json.dumps(themes, ensure_ascii=False)}
    if settings.get('active_custom_theme') == tid:
        updates['active_custom_theme'] = ''
    db.update_settings(updates)
    return redirect(url_for('settings_page'))


# ═══════════════════════════════════════════════════════════
# 订单导出（Markdown → 本地文件）
# ═══════════════════════════════════════════════════════════

@app.post('/export/orders')
def export_orders_md():
    """导出全部订单为单个 Markdown 文件，写入 exports/全部订单.md（覆盖）。"""
    orders = db.get_all_orders()
    platform_sources = db.get_platform_sources()  # 设置驱动，非 models.py 常量
    active = sum(1 for o in orders if not o['is_archived'])
    archived = len(orders) - active
    md = render_template('export/orders.md',
                         orders=orders,
                         platform_sources=platform_sources,
                         exported_at=datetime.now().strftime('%Y-%m-%d %H:%M'),
                         active_count=active,
                         archived_count=archived)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    path = os.path.join(EXPORT_DIR, EXPORT_FILENAME)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(md)
    return jsonify({'success': True, 'count': len(orders),
                    'active': active, 'archived': archived, 'path': path})


@app.route('/export/open-folder')
def open_export_folder():
    """在文件管理器中打开导出文件夹（便于本地查看导出的 .md）。"""
    os.makedirs(EXPORT_DIR, exist_ok=True)
    try:
        if os.name == 'nt':
            os.startfile(EXPORT_DIR)  # Windows
        elif sys.platform == 'darwin':
            import subprocess
            subprocess.Popen(['open', EXPORT_DIR])  # macOS
        else:
            import subprocess
            subprocess.Popen(['xdg-open', EXPORT_DIR])  # Linux
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════
# 浏览器插件 API
# ═══════════════════════════════════════════════════════════

@app.post('/api/import/mihuashi')
def api_import_mihuashi():
    """接收浏览器插件抓取的米画师数据，创建订单（P19-F7：校验本机/插件来源）"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源被拒绝'}), 403
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': '无数据'}), 400

    def _str(v):
        return str(v).strip() if v is not None else ''
    project_name = _str(data.get('projectName') or data.get('project_name') or '未命名')
    platform_url = _str(data.get('url') or '')
    customer_name = _str(data.get('customerName') or data.get('custName') or data.get('customer_name') or '')
    customer_mhs_id = _str(data.get('customerId') or data.get('custId') or data.get('customer_id') or '')
    price_str = _str(data.get('price') or '0')
    category = _str(data.get('category') or data.get('cat') or data.get('commission_type') or '')
    page_deadline = _str(data.get('deadline') or data.get('page_deadline') or '')
    start_date = _str(data.get('startDate') or data.get('scheduled_start') or '')
    end_date = _str(data.get('endDate') or data.get('scheduled_end') or '')
    fee_pct = _str(data.get('fee') or data.get('platform_fee_pct') or '')
    description = _str(data.get('description') or data.get('desc') or data.get('notes') or '')

    # 解析价格
    try:
        price = float(price_str.replace(',', '').replace('¥', '').replace(' ', ''))
    except (ValueError, TypeError):
        price = 0.0

    # 解析手续费百分比
    try:
        fee_val = float(fee_pct) if fee_pct else None
    except (ValueError, TypeError):
        fee_val = None

    # 解析日期
    import re as _re2
    def _parse_d(s):
        if not s: return None
        s = s.strip()
        m = _re2.match(r'(\d{4})[年\-/](\d{1,2})[月\-/](\d{1,2})', s)
        if m: return f"{int(m.group(1))}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        if _re2.match(r'\d{4}-\d{2}-\d{2}', s): return s
        return None
    scheduled_start = _parse_d(start_date) if start_date else None
    scheduled_end = _parse_d(end_date) if end_date else None

    # 尝试匹配已有客户（无客户名则留空）
    customer_id = None
    if customer_name:
        customers = db.list_customers(search=customer_name)
        for c in customers:
            if c['name'] == customer_name:
                customer_id = c['id']
                break
        if not customer_id:
            try:
                cdata = {'name': customer_name}
                if customer_mhs_id:
                    cdata['platform_url'] = f'https://www.mihuashi.com/profiles/{customer_mhs_id}'
                customer_id = db.create_customer(cdata)
            except Exception:
                pass  # 创建失败则留空
    # 已有客户也更新米画师 ID
    if customer_id and customer_mhs_id:
        try:
            cust = db.get_customer(customer_id)
            if cust and not cust.get('platform_url'):
                db.update_customer(customer_id, {
                    'platform_url': f'https://www.mihuashi.com/profiles/{customer_mhs_id}'
                })
        except Exception:
            pass

    order_data = {
        'project_name': project_name,
        'platform_url': platform_url,
        'source': '米画师',
        'current_stage': '待开始',
        'deposit': price,
        'balance': 0,
        'commission_type': category if category else None,
        'customer_id': customer_id,
        'page_deadline': page_deadline if page_deadline else None,
        'scheduled_start': scheduled_start,
        'scheduled_end': scheduled_end,
        'notes': description if description else None,
    }
    if fee_val is not None:
        order_data['platform_fee_pct'] = fee_val

    try:
        order_id = db.create_order(order_data)
        return jsonify({
            'success': True,
            'order_id': order_id,
            'project_name': project_name,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════
# 系统工具端点
# ═══════════════════════════════════════════════════════════

@app.post('/api/shutdown')
def api_shutdown():
    """关闭服务器（P19-F7：仅 POST + 本机来源，跨站 img/脚本触发失效）"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源被拒绝'}), 403
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        return jsonify({'success': True, 'message': '服务器正在关闭'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/log-error', methods=['POST'])
def api_log_error():
    """接收前端错误日志"""
    data = request.get_json(silent=True) or {}
    print(f"[FRONTEND ERROR] {data.get('time', '')} {data.get('type', '')}: {data.get('message', '')}",
          flush=True)
    return jsonify({'success': True})


# ═══════════════════════════════════════════════════════════
# favicon 路由 — 优先 SVG，不支持时回退 .ico
# ═══════════════════════════════════════════════════════════

@app.route('/favicon.ico')
def favicon():
    return redirect(url_for('static', filename='logo.svg'))


# ═══════════════════════════════════════════════════════════
# 错误处理
# ═══════════════════════════════════════════════════════════

@app.errorhandler(500)
def handle_500(e):
    print(f"[SERVER ERROR 500] {e}", flush=True)
    return "服务器内部错误", 500


@app.errorhandler(Exception)
def handle_exception(e):
    # HTTP 异常（404/405 等）交给 Flask 默认处理，不转为 500
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    print(f"[SERVER ERROR] {type(e).__name__}: {e}", flush=True)
    return "服务器错误", 500


# ═══════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════

if __name__ == '__main__':
    import sys
    db.init_db()
    port = 5000
    if '--port' in sys.argv:
        idx = sys.argv.index('--port')
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])
    elif '-p' in sys.argv:
        idx = sys.argv.index('-p')
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])
    print(f"[V2 开发版] 启动于 http://127.0.0.1:{port}")
    app.run(debug=True, host='127.0.0.1', port=port)
