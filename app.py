"""排单工具 — V2 开发版"""

from datetime import date, datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory, make_response
from flask_cors import CORS
import io
import os
import re
import sys
import signal
import logging
import shutil
import glob
import hashlib
import uuid
import json
import time
import threading
import atexit
import secrets
import socket as _socket
import subprocess
from urllib.parse import urlparse
import db
from werkzeug.datastructures import FileStorage
from models import (
    OrderCreate, OrderUpdate, CustomerCreate, CustomerUpdate,
    ReplyTemplateIn, PricelistItemIn, ReceiptDraftIn, ReceiptStyleIn,
                       ReceiptMultPresetIn, PaymentRecord,
)
from image_processor import (
    process_uploaded_file, process_uploaded_file_multi, save_without_pillow,
    process_tool_image, ALLOWED_EXTS, MAX_UPLOAD_SIZE,
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
UPLOAD_PRICELIST_DIR = os.path.join(UPLOAD_DIR, 'pricelist')  # Spec 22（003 价目表例图）
UPLOAD_RECEIPT_DIR = os.path.join(UPLOAD_DIR, 'receipt')  # Spec 23（小票主图/背景）


# ═══════════════════════════════════════════════════════════
# Jinja 自定义过滤器
# ═══════════════════════════════════════════════════════════

# #41 当前应用版本（发版时与上传版 CHANGELOG/installer.iss/git tag 保持一致）
APP_VERSION = '1.4.0'  # 定版 1.40：功能冻结，进入打包验证
# #41 上传版 GitHub 仓库（前端直连其 Releases API 检测新版本）
GITHUB_REPO = 'mimo9708/oimimo-scheduler'

STAGE_CLASS_MAP = {
    '待开始': 'pending', '色稿': 'sketch', '线稿': 'lineart',
    '细化': 'detail', '收尾': 'finish', '完成': 'completed', '退单': 'cancelled'
}

@app.template_filter('stage_class')
def stage_class(stage_name):
    """将阶段名称转为 CSS class 名"""
    return STAGE_CLASS_MAP.get(stage_name, 'pending')


@app.template_filter('price_fmt')
def price_fmt(price):
    """Spec 22（003）价格展示：¥ + 去尾零；0/空/非法 → 「面议」。"""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return '面议'
    if p <= 0:
        return '面议'
    return '¥' + ('%.2f' % p).rstrip('0').rstrip('.')


@app.template_filter('price_range_fmt')
def price_range_fmt(item):
    """价目表价格展示（2026-08-12 UX 改造）：上限 > 起价 → 区间 ¥350–500；否则单价/面议。"""
    item = item or {}
    try:
        p = float(item.get('price') or 0)
    except (TypeError, ValueError):
        p = 0
    try:
        pmax = item.get('price_max')
        pmax = float(pmax) if pmax is not None else None
    except (TypeError, ValueError):
        pmax = None
    if p > 0 and pmax and pmax > p:
        return price_fmt(p) + '–' + price_fmt(pmax).lstrip('¥')
    return price_fmt(p)


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


def _safe_int(value, default, min_val=1):
    """#40 P2：查询参数安全转 int，非法/超界回退默认值（防 ?page=abc 直接 500）。"""
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result >= min_val else default


@app.template_filter('render_notes')
def render_notes(text):
    """将备注中的图片 URL 渲染为可点击缩略图

    P19-F6 安全：URL 经 HTML 属性转义后仅放 src，onclick 改 window.open(this.src)，
    消除 URL 引号逃逸 onclick 属性的注入面。
    #40 P1 安全：正文先整段 html.escape 再解析图片标记，封堵存储型 XSS
    （模板侧 |safe 输出，此前正文未转义）；图片正则要求 https?:// 前缀，
    天然排除 javascript: 伪协议。换行由模板容器 white-space:pre-wrap 处理。
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

    # 先整段转义，后续替换均基于已转义文本（URL 内的 & 已变 &amp;，
    # 放入 src 属性合法，浏览器会解码回原值）
    result = html.escape(text, quote=True)

    # 先处理 Markdown 图片
    def md_replace(m):
        url = m.group(1)  # 已随整段转义，不重复 escape 避免双重转义
        return f'<img src="{url}" class="notes-img" loading="lazy" onclick="window.open(this.src)" title="点击查看原图">'
    result = md_img_pattern.sub(md_replace, result)

    # 再处理裸 URL（跳过已处理的和已在 <img> 标签中的）
    def url_replace(m):
        url = m.group(1)  # 已随整段转义
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


# #45 R2：阶段中文标签 → CSS slug（与 db.py stage_class 过滤器对齐）
STAGE_LABEL_TO_SLUG = {'待开始': 'pending', '色稿': 'sketch', '线稿': 'lineart',
                       '细化': 'detail', '收尾': 'finish', '完成': 'completed', '退单': 'cancelled'}


# Spec 32 D1：深色主题判定阈值（theme_bg 相对亮度低于此值判深）
_DARK_LUMINANCE_THRESHOLD = 0.35


def _relative_luminance(color_hex):
    """hex 色（#rgb/#rrggbb）→ WCAG 相对亮度（0-1）；解析失败返回 None。

    与 db._contrast_text_color 同口径（gamma 2.2 近似）。Spec 32 D1。
    """
    try:
        h = (color_hex or '').strip().lstrip('#')
        if len(h) == 3:
            h = ''.join(c * 2 for c in h)
        if len(h) != 6:
            return None
        r, g, b = int(h[:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255
        return 0.2126 * r ** 2.2 + 0.7152 * g ** 2.2 + 0.0722 * b ** 2.2
    except (ValueError, TypeError, AttributeError):
        return None


def _is_dark_theme(settings: dict) -> bool:
    """由 theme_bg 亮度自动判定深色主题；缺键/非法值 → 浅色。Spec 32 D1。"""
    lum = _relative_luminance(settings.get('theme_bg'))
    return lum is not None and lum < _DARK_LUMINANCE_THRESHOLD


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
        # Spec 32：扩展 token（用户显式配置时直接输出；缺省时深色自动派生见下方）
        'theme_text_tertiary': '--color-text-tertiary',
        'theme_border_strong': '--color-border-strong',
        'theme_hover': '--color-hover',
        'theme_active': '--color-active',
        'theme_sidebar_hover': '--color-sidebar-hover',
        'theme_placeholder': '--color-placeholder',
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
            if key == 'theme_warning' and not _is_dark_theme(settings):
                # Spec 32 卡 154：浅色主题下亮琥珀黄文字不可见（对米白底 ratio≈1.8）
                # → 压暗为深琥珀（ratio≈5.2）；bg 变体仍按用户原值 12% 派生
                lines.append(f"    {css_var}: color-mix(in srgb, {val} 55%, #000);")
            else:
                lines.append(f"    {css_var}: {val};")
            # 自动生成背景色（bg 变体 = 原色 + 低透明度）
            if key == 'theme_success':
                lines.append(f"    --color-success-bg: color-mix(in srgb, {val} 12%, transparent);")
            elif key == 'theme_warning':
                lines.append(f"    --color-warning-bg: color-mix(in srgb, {val} 12%, transparent);")
            elif key == 'theme_danger':
                lines.append(f"    --color-danger-bg: color-mix(in srgb, {val} 12%, transparent);")
    # Spec 32 D2：accent 对比字色 + hover（服务端注入，CSS 端 var(--x, 默认) 消费）
    if 'theme_accent' in settings:
        _accent = settings['theme_accent']
        _accent_text = db._contrast_text_color(_accent)
        lines.append(f"    --color-accent-text: {_accent_text};")
        # hover 向对比极混合 15%：深 accent 提亮、浅 accent 压暗，双主题均产生可见变化
        lines.append("    --color-accent-hover: color-mix(in srgb, "
                     f"{_accent} 85%, {_accent_text} 15%);")
    # #45 R2：阶段色统一由「着色模式·按阶段」面板管理。
    # 读取链：cal_stage_<中文> > 旧 stage_<slug>（老用户存量值）> app.css 内置默认
    for _label, _slug in STAGE_LABEL_TO_SLUG.items():
        _val = settings.get(f'cal_stage_{_label}') or settings.get(f'stage_{_slug}')
        if _val:
            lines.append(f"    --stage-{_slug}: {_val};")
            lines.append(f"    --stage-{_slug}-bg: color-mix(in srgb, {_val} 12%, transparent);")
    # Spec 32 D1：深色派生变量（仅当用户未显式配置对应键）
    if _is_dark_theme(settings):
        _txt2 = settings.get('theme_text_secondary')
        _txt = settings.get('theme_text')
        _bdr = settings.get('theme_border')
        if 'theme_text_tertiary' not in settings and _txt2 and _txt:
            lines.append(f"    --color-text-tertiary: color-mix(in srgb, {_txt2} 50%, {_txt} 50%);")
        if 'theme_border_strong' not in settings and _bdr:
            lines.append(f"    --color-border-strong: color-mix(in srgb, {_bdr} 75%, white 25%);")
        if 'theme_hover' not in settings:
            lines.append("    --color-hover: rgba(255,255,255,0.06);")
        if 'theme_active' not in settings:
            lines.append("    --color-active: rgba(255,255,255,0.10);")
        if 'theme_sidebar_hover' not in settings:
            lines.append("    --color-sidebar-hover: rgba(255,255,255,0.08);")
        if 'theme_placeholder' not in settings and _txt2:
            lines.append(f"    --color-placeholder: {_txt2};")
    # Spec 32 D5：color-scheme 接管原生控件（select/date 弹层）配色
    lines.append(f"    color-scheme: {'dark' if _is_dark_theme(settings) else 'light'};")
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
    'save_settings': 'Ctrl+S',
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
    'save_settings': '保存全部（仅设置页）',
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
        'STAGE_FLOWS': db.get_stage_flows(),  # Spec12 阶段流程预设（表单下拉/编辑器用）
        'SOURCE_CHOICES': source_list,
        'DDL_CHOICES': db.get_choices('ddl'),
        'PAYMENT_CHOICES': db.get_choices('payment'),
        'COMMISSION_TYPE_CHOICES': db.get_choices('commission_type'),
        'STAGE_CLASS_MAP': STAGE_CLASS_MAP,
        'PLATFORM_SOURCES': platform_sources,
        'DEFAULT_FEES': db.get_default_fees_map(),  # #43：表单选平台后自动填入默认费率
        'theme_css': _build_theme_css(settings),
        'settings': settings,
        'shortcuts': merge_shortcuts(settings),
        'SHORTCUT_LABELS': SHORTCUT_LABELS,
        'APP_VERSION': APP_VERSION,
        'GITHUB_REPO': GITHUB_REPO,
        # P20c：时薪功能全局保险丝（'1'/'0'，缺省视为开）
        'hourly_rate_enabled': settings.get('hourly_rate_enabled', '1') == '1',
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
    # P20d：时薪统计四组数据（开关关闭时传 None，模板整块不渲染）
    settings = db.get_all_settings()
    hourly_enabled = settings.get('hourly_rate_enabled', '1') == '1'
    hourly_summary = hourly_trend = hourly_by_type = hourly_by_source = None
    if hourly_enabled:
        hourly_summary = db.get_hourly_rate_summary(year=year)
        hourly_trend = db.get_monthly_hourly_trend(year=year)
        hourly_by_type = db.get_hourly_by_commission_type(year=year)
        hourly_by_source = db.get_hourly_by_source(year=year)
    return render_template('income.html',
                           selected_year=year,
                           available_years=years,
                           monthly_stats=monthly_stats,
                           monthly_projected=monthly_projected,
                           cumulative_income=cumulative,
                           type_distribution=distribution,
                           commission_colors=db.get_merged_palette('commission'),  # #43：品类甜甜圈与日历着色同源
                           source_colors=db.get_merged_palette('source'),  # 来源时薪图与着色来源颜色同步
                           top_customers=top_customers,
                           income_layout=settings.get('income_layout', ''),  # P21a 自定义布局（空串=用模板默认）
                           hourly_summary=hourly_summary,
                           hourly_trend=hourly_trend,
                           hourly_by_type=hourly_by_type,
                           hourly_by_source=hourly_by_source)


@app.route('/api/income/type-distribution')
def api_type_distribution():
    """品类分布 AJAX 接口 — 按 year/month 筛选"""
    year_str = request.args.get('year', '')
    year = int(year_str) if year_str.isdigit() else date.today().year
    month_str = request.args.get('month', '')
    month = int(month_str) if month_str.isdigit() else None
    distribution = db.get_commission_type_distribution(year=year, month=month)
    return jsonify(distribution)


@app.route('/api/income/hourly-type-distribution')
def api_hourly_type_distribution():
    """P20d：时薪×稿件类别分布 AJAX 接口 — 按 year/month 筛选"""
    year_str = request.args.get('year', '')
    year = int(year_str) if year_str.isdigit() else date.today().year
    month_str = request.args.get('month', '')
    month = int(month_str) if month_str.isdigit() else None
    return jsonify(db.get_hourly_by_commission_type(year=year, month=month))


@app.route('/api/quote-suggestion')
def api_quote_suggestion():
    """P20d：报价建议条 — 依据历史时薪样本 × 预计工时给出建议价与 P25~P75 区间。

    无样本 / 开关关闭 / 预计工时无效 → 返回空串（报价条容器保持空白）。
    """
    if db.get_all_settings().get('hourly_rate_enabled', '1') != '1':
        return ''
    try:
        est_hours = float(request.args.get('estimated_hours') or 0)
    except (TypeError, ValueError):
        return ''
    if est_hours <= 0:
        return ''
    ctype = (request.args.get('commission_type') or '').strip()
    sample = db.get_quote_sample(ctype or None)
    if not sample:
        return ''
    rates = sample['rates']  # 已升序
    n = len(rates)

    def _pct(p):
        return rates[min(n - 1, max(0, round(p * (n - 1))))]

    suggest = round(sample['rate'] * est_hours)
    low = round(_pct(0.25) * est_hours)
    high = round(_pct(0.75) * est_hours)
    scope_note = '同类别'
    return (
        '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;'
        'padding:6px 10px;border-radius:var(--radius-sm);'
        'background:color-mix(in srgb, var(--color-info) 8%, transparent);'
        'font-size:0.82rem;color:var(--color-text-secondary);">'
        f'<span>💡 参考报价 ¥{suggest}（区间 ¥{low}~¥{high}，基于{scope_note}{sample["count"]}单）</span>'
        f'<button type="button" class="btn btn-sm" style="padding:2px 10px;font-size:0.78rem;" '
        f'onclick="applyQuoteSuggestion(this, {suggest})">采用</button>'
        '</div>'
    )


@app.route('/calendar')
def calendar_view():
    unscheduled = db.get_unscheduled_orders()
    # #45 R1：URL 显式 ?color= 优先（分享/书签），否则读用户偏好，最后回落按来源
    color_mode = request.args.get('color') or db.get_all_settings().get('calendar_color_mode', 'source')
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
        'commission_type': request.args.get('commission_type'),
        'search': request.args.get('search'),
        'archived': request.args.get('archived', '0') == '1',
        'page': _safe_int(request.args.get('page'), 1),
        'per_page': _safe_int(request.args.get('per_page'), 30),
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

    # HTMX 请求 → 只返回表格行（卡 156：boosted 导航除外——HX-Boosted 头表示整页 swap，
    # 返回 fragment 会替换整个 body，须返回完整页）
    if request.headers.get('HX-Request') and not request.headers.get('HX-Boosted'):
        return render_template('partials/order_rows.html', orders=orders)

    return render_template('orders/list.html', orders=orders, filters=filters, pagination=pagination)


@app.route('/orders/kanban')
def kanban_view():
    # P19-F11：看板无分页 UI，显式全量（per_page<=0）防默认 30 静默截断丢单
    orders = db.list_orders({'archived': False, 'per_page': 0})
    stages = db.get_choices('stage')
    columns = {stage: [] for stage in stages}
    for o in orders:
        _inject_stage_flow_parsed(o)  # Spec12：看板卡片需要快照阶段
        stage = o['current_stage']
        if stage in columns:
            columns[stage].append(o)
    return render_template('orders/kanban.html', columns=columns)


def _payments_ctx(order: dict) -> dict:
    """Spec 26 收款卡片共用上下文（详情整页与 panel 局部刷新同源；仅 installment 单调用）。"""
    payments = db.list_payments(order['id'])
    received = db.get_received_amount(order['id'])
    expected = max(round(float(order.get('actual_received') or 0) - received, 2), 0.0)
    # D4 只进不退不一致提示：状态已结算但 Σ笔 < 净额−0.01（删笔不回退状态的呈现层兑底）
    inconsistent = (order.get('payment_status') in db.get_paid_statuses()
                    and received < float(order.get('actual_received') or 0) - db.PAYMENT_EPSILON)
    return {'payments': payments, 'payments_received': received,
            'payments_expected': expected, 'payments_inconsistent': inconsistent}


@app.route('/orders/<int:order_id>')
def order_detail(order_id):
    order = db.get_order(order_id)
    if not order:
        return "订单不存在", 404
    _calc_pct_for_display(order)
    _inject_stage_flow_parsed(order)
    customer = db.get_customer(order['customer_id']) if order['customer_id'] else None
    images = db.get_order_images(order_id)
    # Spec 26：整单模式不渲染任何分期 UI（决策 4）——仅 installment 单注入收款上下文
    payments_ctx = _payments_ctx(order) if (order.get('payment_mode') or 'simple') == 'installment' else {}
    return render_template('orders/detail.html', order=order, customer=customer,
                           images=images, **payments_ctx)


@app.get('/orders/<int:order_id>/payments/panel')
def payments_panel(order_id):
    """Spec 26 收款卡片局部刷新（app.js refreshPaymentsCard fetch 本路由，
    与详情整页同源 partial；不用 hx-* 属性——卡片可被 innerHTML 替换）。"""
    order = db.get_order(order_id)
    if not order:
        return '订单不存在', 404
    if (order.get('payment_mode') or 'simple') != 'installment':
        return ''  # 整单模式无分期 UI（防御：切回整单后旧卡片不再渲染）
    return render_template('partials/_order_payments.html', order=order, **_payments_ctx(order))


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


def _inject_stage_flow_parsed(order):
    """Spec12：为模板注入 _stage_flow_parsed（本单阶段快照列表）。
    时间轴宏直接读取该键渲染；不修改 orders.stage_flow 原值。
    """
    if order is not None:
        order['_stage_flow_parsed'] = db.get_order_stage_flow(order)
    return order


@app.route('/orders/new')
def order_new():
    """新建订单 — 统一返回模态框表单（P18-F3：支持 ?template=<id> 预填）
    Spec 32 D6：无 HX-Request 直接访问 → 重定向首页 ?new=1 由前端自动开模态框（审计 B 类裸表单）"""
    if not request.headers.get('HX-Request'):
        # 直接访问裸表单无布局不可用；template 透传保预填链路
        params = {'new': 1}
        tid = request.args.get('template')
        if tid:
            params['template'] = tid
        return redirect(url_for('dashboard', **params))
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


@app.route('/orders/templates/<int:template_id>/delete', methods=['POST'])
def order_template_delete(template_id):
    """删除模板，返回 204 No Content（Spec 18 / 任务卡 78）。
    唯一调用方为 form_modal 的 deleteTpl()（fetch 忽略响应体）；
    页面内删除走 /templates/<id>/delete（302 回列表）。"""
    db.delete_order_template(template_id)
    return '', 204


# ── 模板管理完整页（Spec 18 / 任务卡 78 骨架、79 入口切换；
#    旧模态路由群 GET /orders/templates(/list|/new|<id>/edit) 与 POST /orders/templates(/<id>)
#    已于卡 79 按 spec §3.2 对照表移除，templates_modal.html / templates_list.html 同步删除）──

@app.route('/templates')
def templates_page():
    """模板管理 — 卡片网格完整页。"""
    return render_template('orders/templates_page.html',
                           templates=db.list_order_templates())


@app.route('/templates/new')
def templates_page_new():
    """新建模板 — 完整页表单（独立于订单，不写 orders 表）。"""
    return render_template('orders/templates_page_form.html', tpl=None,
                           customers=db.list_customers(), is_new=True)


@app.route('/templates/new', methods=['POST'])
def templates_page_create():
    """创建模板（普通表单 + 302，不依赖片段替换）。空名/全空格不落库。"""
    name = (request.form.get('name', '') or '').strip()
    if name:
        db.create_order_template(name, db._snapshot_template_data(request.form))
    return redirect('/templates')


@app.route('/templates/<int:template_id>/edit')
def templates_page_edit(template_id):
    """编辑模板 — 完整页表单（预填名称 + 字段快照）。"""
    tpl = db.get_order_template(template_id)
    if not tpl:
        return "模板不存在", 404
    return render_template('orders/templates_page_form.html', tpl=tpl,
                           customers=db.list_customers(), is_new=False)


@app.route('/templates/<int:template_id>/edit', methods=['POST'])
def templates_page_update(template_id):
    """全量更新模板（普通表单 + 302）。空名/全空格不落库。"""
    name = (request.form.get('name', '') or '').strip()
    if name:
        db.update_order_template(template_id, name, db._snapshot_template_data(request.form))
    return redirect('/templates')


@app.route('/templates/<int:template_id>/delete', methods=['POST'])
def templates_page_delete(template_id):
    """删除模板（页面内确认表单 + 302 回列表）。"""
    db.delete_order_template(template_id)
    return redirect('/templates')


# ═══════════════════════════════════════════════════════════
# 小工具平台（Spec 22：000 平台壳 / 002 回复模板 / 003 价目表）
# 隔离约定 D4：每工具独立模板 tools/<slug>.html + CSS .tool-<slug> 包裹
# + JS 函数 <slug>_ 前缀 + 路由 /tools/<slug>/ 前缀。
# ═══════════════════════════════════════════════════════════

# 工具注册表（D5：只做约定不做加载器；第三方规范见 .qoder/docs/tools-dev-guide.md）
_TOOL_REGISTRY_RAW = [
    {'slug': 'reply-templates', 'name': '回复模板', 'icon': 'message-square-text',
     'desc': '常用回复话术一键复制', 'group': '沟通'},
    {'slug': 'pricelist', 'name': '价目表', 'icon': 'receipt',
     'desc': '自定义价目自动排版成图', 'group': '沟通'},
    {'slug': 'receipt', 'name': '小票打印机', 'icon': 'printer',
     'desc': '制品清单生成复古小票', 'group': '沟通'},
]


def _build_tool_registry(raw: list) -> list:
    """启动校验：slug 缺失/重复 → logging.error 并跳过后者（不中断启动，spec §6）。"""
    seen = set()
    registry = []
    for tool in raw:
        slug = tool.get('slug')
        if not slug or slug in seen:
            logging.error('TOOL_REGISTRY slug 缺失或重复，跳过: %r', slug)
            continue
        seen.add(slug)
        registry.append({**tool, 'url': f'/tools/{slug}/'})
    return registry


TOOL_REGISTRY = _build_tool_registry(_TOOL_REGISTRY_RAW)
TOOL_MAP = {t['slug']: t for t in TOOL_REGISTRY}


def _tool_or_404(slug: str):
    """按 slug 取工具元数据；未注册 → abort 404（供模板统一使用）。"""
    tool = TOOL_MAP.get(slug)
    if not tool:
        from flask import abort
        abort(404)
    return tool


@app.route('/tools')
def tools_market():
    """小工具市场页（D1/D2）：卡片网格 + 分组 chip；搜索与最近使用由前端实现。"""
    groups = []
    for t in TOOL_REGISTRY:
        g = t.get('group') or ''
        if g and g not in groups:
            groups.append(g)
    return render_template('tools/index.html', tools=TOOL_REGISTRY, groups=groups)


# ── 002 回复模板（/tools/reply-templates/）──

def _reply_board_response(group: str = '', message: str = '', close_modal: bool = True):
    """写成功统一响应：重渲染 #rt-app 整区 + HX-Trigger（关模态/Toast）。"""
    resp = make_response(render_template(
        'tools/_reply_board.html',
        templates=db.list_reply_templates(group or None),
        groups=db.list_reply_groups(),
        current_group=group or ''))
    resp.headers['HX-Retarget'] = '#rt-app'
    resp.headers['HX-Reswap'] = 'innerHTML'
    triggers = {}
    if close_modal:
        triggers['closeCenterModal'] = {}
    if message:
        triggers['showToast'] = {'message': message, 'type': 'success'}
    if triggers:
        # 默认 ensure_ascii=True：HTTP 头仅允许 latin-1，中文需转 \uXXXX 转义（HTMX 解析 JSON 后还原）
        resp.headers['HX-Trigger'] = json.dumps(triggers)
    return resp


def _reply_form_error(form: dict, errors) -> tuple:
    """校验失败 → 表单片段内联错误（400，片段替换模态内容）。"""
    error_text = '；'.join(err.get('msg', '') for err in errors)
    resp = make_response(render_template(
        'tools/_reply_form.html', tpl=form, error_text=error_text,
        groups=[g['name'] for g in db.list_reply_groups()]))
    return resp, 400


@app.route('/tools/reply-templates/')
def reply_templates_page():
    """回复模板主视图：分组侧栏 + 卡片网格（?group= 服务端过滤）。"""
    group = (request.args.get('group') or '').strip()
    return render_template('tools/reply_templates.html',
                           tool=_tool_or_404('reply-templates'),
                           templates=db.list_reply_templates(group or None),
                           groups=db.list_reply_groups(),
                           current_group=group)


@app.route('/tools/reply-templates/new')
def reply_template_new():
    """新建表单模态片段。"""
    return render_template('tools/_reply_form.html', tpl=None, error_text='',
                           groups=[g['name'] for g in db.list_reply_groups()],
                           current_group=(request.args.get('group') or '').strip())


@app.route('/tools/reply-templates/<int:tid>/edit')
def reply_template_edit(tid):
    """编辑表单模态片段；记录不存在 → 404。"""
    tpl = db.get_reply_template(tid)
    if not tpl:
        return '回复模板不存在', 404
    return render_template('tools/_reply_form.html', tpl=tpl, error_text='',
                           groups=[g['name'] for g in db.list_reply_groups()])


@app.post('/tools/reply-templates/')
def reply_template_create():
    form = {k: request.form.get(k, '') for k in ('group_name', 'title', 'content')}
    try:
        data = ReplyTemplateIn(**form).model_dump()
    except ValidationError as e:
        return _reply_form_error(form, e.errors())
    db.create_reply_template(data['group_name'], data['title'], data['content'])
    return _reply_board_response(group=request.form.get('return_group') or '',
                                 message='模板已创建')


@app.post('/tools/reply-templates/<int:tid>/update')
def reply_template_update(tid):
    if not db.get_reply_template(tid):
        return '回复模板不存在', 404
    form = {k: request.form.get(k, '') for k in ('group_name', 'title', 'content')}
    try:
        data = ReplyTemplateIn(**form).model_dump()
    except ValidationError as e:
        form['id'] = tid
        return _reply_form_error(form, e.errors())
    db.update_reply_template(tid, data['group_name'], data['title'], data['content'])
    return _reply_board_response(group=request.form.get('return_group') or '',
                                 message='模板已保存')


@app.post('/tools/reply-templates/<int:tid>/delete')
def reply_template_delete(tid):
    if not db.delete_reply_template(tid):
        return '回复模板不存在', 404
    return _reply_board_response(group=request.form.get('return_group') or '',
                                 message='模板已删除', close_modal=False)


@app.route('/tools/reply-templates/groups')
def reply_groups_manage():
    """分组管理模态片段（重命名/删除）。"""
    return render_template('tools/_reply_groups.html', groups=db.list_reply_groups())


@app.post('/tools/reply-templates/group/rename')
def reply_group_rename():
    old = (request.form.get('old_name') or '').strip()
    new = (request.form.get('new_name') or '').strip()
    if not old or not new:
        return '分组名不能为空', 400
    if old != new:
        db.rename_reply_group(old, new)
    return _reply_board_response(message='分组已重命名')


@app.post('/tools/reply-templates/group/delete')
def reply_group_delete():
    name = (request.form.get('group_name') or '').strip()
    if not name or name == '未分组':
        return '该分组不可删除', 400
    db.delete_reply_group(name)
    return _reply_board_response(message='分组已删除，组内模板归入「未分组」')


# ── 003 价目表（/tools/pricelist/）──

def _pricelist_categories() -> list:
    """既有分类去重列表（按用户自定义排序序列，未列入的新分类追加末尾；供表单 datalist 与看板分组）。"""
    items = db.list_pricelist_items()
    cats = list(dict.fromkeys(i['category'] for i in items))
    order = db.get_pricelist_category_order()
    if order:
        ordered = [c for c in order if c in cats]
        remaining = [c for c in cats if c not in order]
        return ordered + remaining
    return cats


def _pricelist_board_response(message: str = '', close_modal: bool = True):
    """写成功统一响应：重渲染 #pl-app 整区 + HX-Trigger。"""
    resp = make_response(render_template(
        'tools/_pricelist_board.html',
        items=db.list_pricelist_items(),
        categories=_pricelist_categories(),
        meta=db.get_pricelist_meta(),
        img_limit=db.PRICELIST_IMAGE_LIMIT))
    resp.headers['HX-Retarget'] = '#pl-app'
    resp.headers['HX-Reswap'] = 'innerHTML'
    triggers = {}
    if close_modal:
        triggers['closeCenterModal'] = {}
    if message:
        triggers['showToast'] = {'message': message, 'type': 'success'}
    if triggers:
        # 同 _reply_board_response：HTTP 头禁止非 latin-1 字符
        resp.headers['HX-Trigger'] = json.dumps(triggers)
    return resp


def _pricelist_form_error(form: dict, errors) -> tuple:
    """校验失败 → 表单片段内联错误（400）。"""
    error_text = '；'.join(err.get('msg', '') for err in errors)
    resp = make_response(render_template(
        'tools/_pricelist_form.html', item=form, error_text=error_text,
        categories=_pricelist_categories()))
    return resp, 400


@app.route('/tools/pricelist/')
def pricelist_page():
    """价目表默认落地 = 预览菜单（2026-08-12 UX 改造：浏览模式优先）。"""
    return render_template('tools/pricelist_preview.html',
                           tool=_tool_or_404('pricelist'),
                           items=db.list_pricelist_items(),
                           meta=db.get_pricelist_meta())


@app.route('/tools/pricelist/edit')
def pricelist_edit_page():
    """价目表编辑模式：按分类分节 + 拖拽排序 + 例图管理。"""
    return render_template('tools/pricelist.html',
                           tool=_tool_or_404('pricelist'),
                           items=db.list_pricelist_items(),
                           categories=_pricelist_categories(),
                           meta=db.get_pricelist_meta(),
                           img_limit=db.PRICELIST_IMAGE_LIMIT)


@app.route('/tools/pricelist/board')
def pricelist_board_fragment():
    """编辑看板整区片段（例图上传/删除后前端 fetch 局部刷新用）。"""
    return render_template('tools/_pricelist_board.html',
                           items=db.list_pricelist_items(),
                           categories=_pricelist_categories(),
                           meta=db.get_pricelist_meta(),
                           img_limit=db.PRICELIST_IMAGE_LIMIT)


@app.route('/tools/pricelist/new')
def pricelist_item_new():
    return render_template('tools/_pricelist_form.html', item=None, error_text='',
                           categories=_pricelist_categories())


@app.route('/tools/pricelist/<int:iid>/edit')
def pricelist_item_edit(iid):
    item = db.get_pricelist_item(iid)
    if not item:
        return '价目项目不存在', 404
    return render_template('tools/_pricelist_form.html', item=item, error_text='',
                           categories=_pricelist_categories())


@app.post('/tools/pricelist/')
def pricelist_item_create():
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    form = {k: request.form.get(k, '') for k in ('category', 'name', 'price', 'price_max', 'unit', 'description')}
    try:
        data = PricelistItemIn(**form).model_dump()
    except ValidationError as e:
        return _pricelist_form_error(form, e.errors())
    db.create_pricelist_item(data)
    return _pricelist_board_response(message='项目已创建')


@app.post('/tools/pricelist/<int:iid>/update')
def pricelist_item_update(iid):
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    if not db.get_pricelist_item(iid):
        return '价目项目不存在', 404
    form = {k: request.form.get(k, '') for k in ('category', 'name', 'price', 'price_max', 'unit', 'description')}
    try:
        data = PricelistItemIn(**form).model_dump()
    except ValidationError as e:
        form['id'] = iid
        return _pricelist_form_error(form, e.errors())
    db.update_pricelist_item(iid, data)
    return _pricelist_board_response(message='项目已保存')


@app.post('/tools/pricelist/<int:iid>/delete')
def pricelist_item_delete(iid):
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    row = db.delete_pricelist_item(iid)
    if not row:
        return '价目项目不存在', 404
    # 连带清理例图目录（对齐订单图片删除逻辑）
    item_dir = os.path.join(UPLOAD_PRICELIST_DIR, str(iid))
    if os.path.isdir(item_dir):
        shutil.rmtree(item_dir, ignore_errors=True)
    return _pricelist_board_response(message='项目已删除', close_modal=False)


@app.post('/tools/pricelist/<int:iid>/upload-example')
def pricelist_upload_example(iid):
    """例图上传（2026-08-12 多例图）：每项目最多 3 张，追加 pricelist_images 记录。"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    if not db.get_pricelist_item(iid):
        return jsonify({'success': False, 'error': '项目不存在'}), 404
    if db.count_pricelist_images(iid) >= db.PRICELIST_IMAGE_LIMIT:
        return jsonify({'success': False,
                        'error': f'每个项目最多 {db.PRICELIST_IMAGE_LIMIT} 张例图'}), 400
    if 'image' not in request.files:
        return jsonify({'success': False, 'error': '未选择文件'}), 400
    file = request.files['image']
    if not file or not file.filename:
        return jsonify({'success': False, 'error': '空文件'}), 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        return jsonify({'success': False, 'error': f'不支持的格式: {ext}'}), 400
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > MAX_UPLOAD_SIZE:
        return jsonify({'success': False, 'error': f'文件过大 ({size // 1024 // 1024}MB)，最大 10MB'}), 400

    img_key = uuid.uuid4().hex[:12]
    try:
        result = process_tool_image(file, UPLOAD_PRICELIST_DIR, 'pricelist', iid, img_key)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error('价目表例图上传失败 #%d: %s', iid, e)
        return jsonify({'success': False, 'error': f'图片处理失败: {e}'}), 500

    image_id = db.add_pricelist_image(iid, result['image_path'])
    return jsonify({'success': True,
                    'image_id': image_id,
                    'thumb_url': result['thumb_url'],
                    'preview_url': result['image_url']})


@app.post('/tools/pricelist/images/<int:img_id>/delete')
def pricelist_image_delete(img_id):
    """删除单张例图（2026-08-12 多例图）：删 DB 记录 + 按 img_key 清三件套文件。"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    rec = db.get_pricelist_image(img_id)
    if not rec:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    db.delete_pricelist_image(img_id)
    if rec.get('image_path'):
        base = os.path.basename(rec['image_path'])  # preview_<key>.webp
        key = base[len('preview_'):].rsplit('.', 1)[0]
        item_dir = os.path.join(UPLOAD_PRICELIST_DIR, str(rec['item_id']))
        for f in glob.glob(os.path.join(item_dir, f'*_{key}.*')):
            try:
                os.remove(f)
            except OSError as e:
                logging.warning('价目表例图文件清理失败 %s: %s', f, e)
    return jsonify({'success': True})


@app.post('/tools/pricelist/images/reorder')
def pricelist_images_reorder():
    """例图拖拽排序：JSON {ids:[...]} → 204。"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    payload = request.get_json(silent=True) or {}
    ids = payload.get('ids') or []
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'ids 非法'}), 400
    db.reorder_pricelist_images(ids)
    return '', 204


@app.post('/tools/pricelist/reorder')
def pricelist_reorder():
    """拖拽排序：JSON {ids:[...]} → 204。"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    payload = request.get_json(silent=True) or {}
    ids = payload.get('ids') or []
    try:
        ids = [int(x) for x in ids]
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'ids 非法'}), 400
    db.reorder_pricelist(ids)
    return '', 204


@app.post('/tools/pricelist/categories/reorder')
def pricelist_categories_reorder():
    """分类拖拽排序：JSON {categories:[...]} → 204。Spec 28 Task 124。"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    payload = request.get_json(silent=True) or {}
    categories = payload.get('categories') or []
    if not isinstance(categories, list):
        return jsonify({'success': False, 'error': 'categories 必须是数组'}), 400
    # 前端校验禁止逗号，后端也过滤
    categories = [str(c).strip() for c in categories if isinstance(c, str) and ',' not in c]
    db.set_pricelist_category_order(categories)
    return '', 204


@app.post('/tools/pricelist/meta')
def pricelist_save_meta():
    """菜单元信息（标题/附注）存 settings 键 pricelist_meta。"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    title = (request.form.get('title') or '').strip()[:100]
    note = (request.form.get('note') or '').strip()[:500]
    db.update_settings({'pricelist_meta': json.dumps(
        {'title': title, 'note': note}, ensure_ascii=False)})
    return jsonify({'success': True})


@app.route('/tools/pricelist/preview')
def pricelist_preview_legacy():
    """旧预览链接兼容：预览已反转为默认落地页（2026-08-12 UX 改造）。"""
    return redirect('/tools/pricelist/', code=301)


# ═══════════════════════════════════════════════════════════
# Spec 23 小票打印机（D3 双模式：/ 预览默认页、/edit 编辑）
# ═══════════════════════════════════════════════════════════

def _receipt_bg_abs(rel_path: str) -> str:
    """小票背景相对路径 → 绝对路径（basename 防穿越，限定 bg 目录）。"""
    return os.path.join(UPLOAD_RECEIPT_DIR, 'bg', os.path.basename(rel_path or ''))


def _receipt_remove_bg_file(rel_path: str) -> None:
    """删背景文件（Windows 句柄瞬时占用时重试一次；失败记日志不阻断业务）。"""
    abs_path = _receipt_bg_abs(rel_path)
    for attempt in range(2):
        try:
            os.remove(abs_path)
            return
        except OSError as e:
            if attempt == 0:
                time.sleep(0.2)
                continue
            logging.warning('小票背景文件清理失败 %s: %s', rel_path, e)


@app.route('/tools/receipt/')
def receipt_preview_page():
    """小票预览/打印模式（D15 零编辑控件）：注入草稿 JSON + 模板列表（侧边栏快速切换），receipt.js 单源渲染。"""
    return render_template('tools/receipt.html',
                           tool=_tool_or_404('receipt'),
                           draft=db.get_receipt_draft(),
                           templates=db.list_receipt_templates())


@app.route('/tools/receipt/edit')
def receipt_edit_page():
    """小票编辑模式（D13 价目表只读注入 datalist + 模板列表 + Spec 24 单品倍率预设）。"""
    return render_template('tools/receipt_edit.html',
                           tool=_tool_or_404('receipt'),
                           draft=db.get_receipt_draft(),
                           pricelist=db.list_pricelist_items(),
                           templates=db.list_receipt_templates(),
                           mult_presets=db.get_receipt_mult_presets())


@app.post('/tools/receipt/draft')
def receipt_draft_save():
    """草稿整体保存（D4）：JSON → ReceiptDraftIn → 全删全插。"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '非法来源'}), 403
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'success': False, 'errors': [
            {'loc': [], 'msg': '请求体必须是 JSON 对象'}]}), 400
    try:
        data = ReceiptDraftIn(**payload).model_dump()
    except ValidationError as e:
        # errors() 内 ctx 含 ValueError 实例不可 JSON 序列化，只取 loc/msg
        errs = [{'loc': list(map(str, err.get('loc') or [])), 'msg': str(err.get('msg') or '')}
                for err in e.errors()]
        return jsonify({'success': False, 'errors': errs}), 400
    db.save_receipt_draft(data)
    return jsonify({'success': True})


@app.post('/tools/receipt/mult-presets')
def receipt_mult_presets_save():
    """Spec 24：保存单品倍率快捷预设列表（名称+倍率均可自定义，整体覆盖）。"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '非法来源'}), 403
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'success': False, 'error': '请求体必须是 JSON 对象'}), 400
    presets_raw = payload.get('presets')
    if not isinstance(presets_raw, list):
        return jsonify({'success': False, 'error': 'presets 必须是数组'}), 400
    try:
        presets = [ReceiptMultPresetIn(**p).model_dump() for p in presets_raw]
    except ValidationError as e:
        errs = [{'loc': list(map(str, err.get('loc') or [])), 'msg': str(err.get('msg') or '')}
                for err in e.errors()]
        return jsonify({'success': False, 'errors': errs}), 400
    db.save_receipt_mult_presets(presets)
    return jsonify({'success': True})


@app.post('/tools/receipt/upload-image')
def receipt_upload_image():
    """主图/footer 插图上传（target=main|footer）。

    gray=convert('L') / dither=convert('1') / color=保留原色，再走三级产物链；
    footer 插图（2026-08-13 用户需求 3）默认彩色，渲染在总计与感谢语之间。
    """
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '非法来源'}), 403
    target = request.form.get('target') or 'main'
    if target not in ('main', 'footer'):
        return jsonify({'success': False, 'error': 'target 必须是 main 或 footer'}), 400
    mode = request.form.get('mode') or ('dither' if target == 'main' else 'color')
    if mode not in ('gray', 'dither', 'color'):
        return jsonify({'success': False, 'error': 'mode 必须是 gray、dither 或 color'}), 400
    path_field = 'image_path' if target == 'main' else 'footer_image_path'
    mode_field = 'image_mode' if target == 'main' else 'footer_image_mode'
    if 'image' not in request.files:
        return jsonify({'success': False, 'error': '未选择文件'}), 400
    file = request.files['image']
    if not file or not file.filename:
        return jsonify({'success': False, 'error': '空文件'}), 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        return jsonify({'success': False, 'error': f'不支持的格式: {ext}'}), 400
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > MAX_UPLOAD_SIZE:
        return jsonify({'success': False, 'error': f'文件过大 ({size // 1024 // 1024}MB)，最大 10MB'}), 400
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(file.read()))
        if mode == 'gray':
            img = img.convert('L').convert('RGB')
        elif mode == 'dither':
            img = img.convert('1').convert('RGB')
        else:  # color：保留原色，仅统一色彩空间（PNG 透明底转白底）
            img = img.convert('RGB')
        out_ext = '.png' if ext == '.png' else '.jpg'
        buf = io.BytesIO()
        img.save(buf, 'PNG' if out_ext == '.png' else 'JPEG', quality=90)
        buf.seek(0)
        bw_fs = FileStorage(stream=buf, filename='receipt_img' + out_ext)
        # 替换旧图：先删旧三件套
        old_rel = db.get_receipt_draft()['style'].get(path_field) or ''
        if old_rel:
            old_key = os.path.basename(old_rel)[len('preview_'):].rsplit('.', 1)[0]
            for f in glob.glob(os.path.join(UPLOAD_RECEIPT_DIR, 'draft', f'*_{old_key}.*')):
                try:
                    os.remove(f)
                except OSError as e:
                    logging.warning('小票旧主图清理失败 %s: %s', f, e)
        img_key = uuid.uuid4().hex[:12]
        result = process_tool_image(bw_fs, UPLOAD_RECEIPT_DIR, 'receipt', 'draft', img_key)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.error('小票主图上传失败: %s', e)
        return jsonify({'success': False, 'error': f'图片处理失败: {e}'}), 500
    draft = db.get_receipt_draft()
    draft['style'][path_field] = result['image_path']
    draft['style'][mode_field] = mode
    db.save_receipt_draft(draft)
    return jsonify({'success': True, 'image_url': result['image_url'],
                    'thumb_url': result['thumb_url'], 'img_key': img_key, 'target': target})


@app.post('/tools/receipt/remove-image')
def receipt_remove_image():
    """删主图/footer 插图三件套 + draft 对应路径置空（target=main|footer，query 或 form 传参）。"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '非法来源'}), 403
    target = request.args.get('target') or request.form.get('target') or 'main'
    if target not in ('main', 'footer'):
        return jsonify({'success': False, 'error': 'target 必须是 main 或 footer'}), 400
    path_field = 'image_path' if target == 'main' else 'footer_image_path'
    draft = db.get_receipt_draft()
    old_rel = draft['style'].get(path_field) or ''
    if old_rel:
        old_key = os.path.basename(old_rel)[len('preview_'):].rsplit('.', 1)[0]
        for f in glob.glob(os.path.join(UPLOAD_RECEIPT_DIR, 'draft', f'*_{old_key}.*')):
            try:
                os.remove(f)
            except OSError as e:
                logging.warning('小票图片文件清理失败 %s: %s', f, e)
    draft['style'][path_field] = ''
    db.save_receipt_draft(draft)
    return jsonify({'success': True})


@app.post('/tools/receipt/upload-bg')
def receipt_upload_bg():
    """背景上传（D10：单文件，最长边 1600 缩放转 RGB，JPEG q85 / PNG 保持）。"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '非法来源'}), 403
    if 'image' not in request.files:
        return jsonify({'success': False, 'error': '未选择文件'}), 400
    file = request.files['image']
    if not file or not file.filename:
        return jsonify({'success': False, 'error': '空文件'}), 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        return jsonify({'success': False, 'error': f'不支持的格式: {ext}'}), 400
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > MAX_UPLOAD_SIZE:
        return jsonify({'success': False, 'error': f'文件过大 ({size // 1024 // 1024}MB)，最大 10MB'}), 400
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(file.read())).convert('RGB')
        w, h = img.size
        if max(w, h) > 1600:
            ratio = 1600 / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        bg_dir = os.path.join(UPLOAD_RECEIPT_DIR, 'bg')
        os.makedirs(bg_dir, exist_ok=True)
        bg_key = uuid.uuid4().hex[:8]
        if ext == '.png':
            fname = f'bg_{bg_key}.png'
            img.save(os.path.join(bg_dir, fname), 'PNG')
        else:
            fname = f'bg_{bg_key}.jpg'
            img.save(os.path.join(bg_dir, fname), 'JPEG', quality=85)
    except Exception as e:
        logger.error('小票背景上传失败: %s', e)
        return jsonify({'success': False, 'error': f'图片处理失败: {e}'}), 500
    # 旧背景：无模板引用则删文件（§3.7 引用检查）
    draft = db.get_receipt_draft()
    old_rel = draft['style'].get('bg_path') or ''
    if old_rel and not db.receipt_bg_referenced(os.path.basename(old_rel)):
        _receipt_remove_bg_file(old_rel)
    rel = f'receipt/bg/{fname}'
    draft['style']['bg_path'] = rel
    db.save_receipt_draft(draft)
    return jsonify({'success': True, 'bg_url': '/uploads/' + rel})


@app.post('/tools/receipt/remove-bg')
def receipt_remove_bg():
    """删背景文件（无模板引用时）+ bg_path 置空回退默认纹理。"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '非法来源'}), 403
    draft = db.get_receipt_draft()
    old_rel = draft['style'].get('bg_path') or ''
    if old_rel and not db.receipt_bg_referenced(os.path.basename(old_rel)):
        _receipt_remove_bg_file(old_rel)
    draft['style']['bg_path'] = ''
    db.save_receipt_draft(draft)
    return jsonify({'success': True})


@app.post('/tools/receipt/templates')
def receipt_template_create():
    """保存样式模板（D14：只存 style + 文案，丢弃制品/计算参数/日期/单号）。"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '非法来源'}), 403
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'success': False, 'error': '请求体必须是 JSON 对象'}), 400
    name = str(payload.get('name') or '').strip()
    if not name or len(name) > 50:
        return jsonify({'success': False, 'error': '模板名称必填（≤50 字）'}), 400
    cfg_in = payload.get('config') or {}
    try:
        style = ReceiptStyleIn(**(cfg_in.get('style') or {})).model_dump()
    except ValidationError:
        style = ReceiptStyleIn().model_dump()
    texts_in = cfg_in.get('texts') or {}
    texts = {k: str(texts_in.get(k) or '')[:200]
             for k in ('shop_name', 'subtitle', 'contact', 'footer')}
    tid = db.create_receipt_template(name, {'style': style, 'texts': texts})
    return jsonify({'success': True, 'id': tid})


@app.post('/tools/receipt/templates/<int:tid>/apply')
def receipt_template_apply(tid):
    """应用模板（D14：只合并 style+文案；背景文件缺失 → 回退默认 + bg_fallback 标记）。"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '非法来源'}), 403
    tpl = db.get_receipt_template(tid)
    if not tpl:
        return jsonify({'success': False, 'error': '模板不存在'}), 404
    cfg = tpl['config'] or {}
    draft = db.get_receipt_draft()
    style = dict(draft['style'])
    style.update(cfg.get('style') or {})
    bg_fallback = False
    bg_rel = style.get('bg_path') or ''
    if bg_rel and not os.path.isfile(_receipt_bg_abs(bg_rel)):
        style['bg_path'] = ''
        bg_fallback = True
    draft['style'] = style
    for k, v in (cfg.get('texts') or {}).items():
        if k in ('shop_name', 'subtitle', 'contact', 'footer'):
            draft['meta'][k] = str(v or '')
    db.save_receipt_draft(draft)
    return jsonify({'success': True, 'bg_fallback': bg_fallback})


@app.post('/tools/receipt/templates/<int:tid>/delete')
def receipt_template_delete(tid):
    """删模板（背景文件不联动删：可能被当前草稿引用）。"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '非法来源'}), 403
    if not db.delete_receipt_template(tid):
        return jsonify({'success': False, 'error': '模板不存在'}), 404
    return jsonify({'success': True})


@app.route('/orders/<int:order_id>/edit')
def order_edit(order_id):
    order = db.get_order(order_id)
    if not order:
        return "订单不存在", 404
    _calc_pct_for_display(order)
    _inject_stage_flow_parsed(order)
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
    return render_template('customers/form.html', customer=None, is_new=True,
                           vip_discount_presets=db.get_vip_discount_presets())  # Spec19


@app.route('/customers/<int:customer_id>/edit')
def customer_edit(customer_id):
    customer = db.get_customer(customer_id)
    if not customer:
        return "客户不存在", 404
    return render_template('customers/form.html', customer=customer, is_new=False,
                           vip_discount_presets=db.get_vip_discount_presets())  # Spec19


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
        # #40 P1：兼容精确时间模式 'YYYY-MM-DDTHH:MM'，取前 10 位按日期解析
        return date.today() > date.fromisoformat(str(ed).strip()[:10])
    except (TypeError, ValueError):
        logging.warning("_should_confirm_archive: 无法解析 scheduled_end=%r", ed)
        return False


def _needs_work_hours_prompt(order):
    """P20d：订单进入完成终态（非退单）且开关开、未录工时、未排除统计 → 弹补录工时窗。

    双路径共用：看板/快速切换 stage 端点 + 编辑表单 inline 保存。
    """
    if not order:
        return False
    try:
        enabled = db.get_all_settings().get('hourly_rate_enabled', '1') == '1'
    except Exception as e:
        logging.error("_needs_work_hours_prompt: 读取设置失败 %s", e)
        return False
    if not enabled:
        return False
    stage = order.get('current_stage') or ''
    if not db.is_terminal_stage(stage) or db.is_refund_stage(stage):
        return False
    if order.get('work_hours'):
        return False
    if order.get('exclude_hourly'):
        return False
    return True


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
    # Spec12：先单独解析/校验 stage_flow（JSON 字符串），失败立即 400
    stage_flow_raw = form_data.get('stage_flow')
    try:
        stage_flow_json = db.parse_stage_flow_from_form(stage_flow_raw)
    except ValueError as e:
        return f"阶段流程校验失败: {e}", 400
    # 校验通过的 JSON 字符串回填到 form_data；未传/空串时为 None → db 层用默认流程
    form_data['stage_flow'] = stage_flow_json
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
    # #40 P4：不存在的订单直接 404，而非静默"假装成功"
    if not db.get_order(order_id):
        return "订单不存在", 404

    form_data = dict(request.form)
    is_inline = form_data.pop('inline', None) == '1'
    is_modal = form_data.pop('modal', None) == '1'

    # customer_id 空字符串 → None
    if form_data.get('customer_id', '').strip() == '':
        form_data['customer_id'] = None
    # platform_fee_pct 空字符串 → None
    if form_data.get('platform_fee_pct', '').strip() == '':
        form_data['platform_fee_pct'] = None
    # Spec12：先单独解析/校验 stage_flow（JSON 字符串），失败立即 400
    # 与 create_order 区别：前端可能不传 stage_flow（表示不修改）→ 保持 form_data 不含该键
    if 'stage_flow' in form_data:
        stage_flow_raw = form_data.pop('stage_flow')
        try:
            stage_flow_json = db.parse_stage_flow_from_form(stage_flow_raw)
        except ValueError as e:
            return f"阶段流程校验失败: {e}", 400
        # 回填规范化后的 JSON；空串时 stage_flow_json=None → db 层把快照清空（回退默认流程）
        form_data['stage_flow'] = stage_flow_json
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

    # 移除 None 值，但保留 customer_id / platform_fee_pct / stage_flow / discount_pct
    # （None 表示清空/无手续费/清空流程快照/清空折扣，需传到 db 层；其他 None 字段视为未传）
    data = {k: v for k, v in data.items() if v is not None or k in ('customer_id', 'platform_fee_pct', 'stage_flow', 'discount_pct')}  # Spec19：discount_pct None = 不打折（D2）

    # Spec 26：update_order 返回 (ok, err)——防错守卫拒绝（金额冲突/切换保护/免收互斥）
    # 时整单未落库，这里转 400 提示（对齐本路由 stage_flow/ValidationError 的 400 风格）
    ok, err = db.update_order(order_id, data)
    if not ok:
        return err or '保存失败', 400

    # P18-F7：保存后判定是否需过期归档确认
    saved = db.get_order(order_id)
    needs_confirm = _needs_overdue_archive_confirm(saved)

    # 如果是内联编辑，返回刷新触发器
    if is_inline:
        resp = jsonify({'success': True, 'order_id': order_id})
        # P20d：完成终态未录工时 → 先弹补录窗；promptWorkHours 放第一个 key，
        # 其监听器先执行设 flag，orderUpdated 的归档确认链由前端串行接续（不并发）
        triggers = {}
        if _needs_work_hours_prompt(saved):
            triggers['promptWorkHours'] = {'id': order_id, 'name': saved.get('project_name') or ''}
        # 携带 archiveConfirm 让前端在抽屉关闭后打开归档确认
        triggers['orderUpdated'] = {'archiveConfirm': order_id} if needs_confirm else {}
        resp.headers['HX-Trigger'] = json.dumps(triggers)
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


# ── Spec 26 收款流水路由（阶段 2：JSON + HX-Trigger；详情时间线/编辑器 UI 归阶段 3）──

def _payments_resp(order_id: int, action: str, message: str, status: int = 200):
    """收款写操作成功响应：JSON + HX-Trigger（showToast 提示 + paymentsChanged
    供前端刷新详情/列表，监听器归阶段 3；HTTP 头仅 latin-1，json.dumps 默认转义）。"""
    resp = jsonify({'success': True, 'message': message})
    resp.headers['HX-Trigger'] = json.dumps({
        'showToast': message, 'paymentsChanged': {'id': order_id, 'action': action}})
    return resp, status


@app.post('/orders/<int:order_id>/payments')
def payment_add(order_id):
    """Spec 26：新增一笔收款（JSON → PaymentRecord 校验 → db.add_payment 单事务
    状态机：先校验模拟 Σ 后写库 → 收齐自动结算）。"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'success': False, 'error': '请求体必须是 JSON 对象'}), 400
    try:
        data = PaymentRecord(**payload).model_dump()
    except ValidationError as e:
        # errors() 内 ctx 含 ValueError 实例不可 JSON 序列化，只取 loc/msg（对齐 receipt 先例）
        errs = [{'loc': list(map(str, err.get('loc') or [])), 'msg': str(err.get('msg') or '')}
                for err in e.errors()]
        return jsonify({'success': False, 'errors': errs}), 400
    ok, err = db.add_payment(order_id, data)
    if not ok:
        # '订单不存在' 语义 404，其余校验拒绝（超额/笔数上限/金额无效）400
        code = 404 if '不存在' in (err or '') else 400
        return jsonify({'success': False, 'error': err or '保存失败'}), code
    return _payments_resp(order_id, 'add', err or '已记一笔收款')


@app.put('/orders/<int:order_id>/payments/<int:payment_id>')
def payment_update(order_id, payment_id):
    """Spec 26：修改一笔收款（替换口径校验；body 同 PaymentRecord）。"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'success': False, 'error': '请求体必须是 JSON 对象'}), 400
    try:
        data = PaymentRecord(**payload).model_dump()
    except ValidationError as e:
        errs = [{'loc': list(map(str, err.get('loc') or [])), 'msg': str(err.get('msg') or '')}
                for err in e.errors()]
        return jsonify({'success': False, 'errors': errs}), 400
    ok, err = db.update_payment(payment_id, data)
    if not ok:
        code = 404 if '不存在' in (err or '') else 400
        return jsonify({'success': False, 'error': err or '保存失败'}), code
    return _payments_resp(order_id, 'update', err or '收款已更新')


@app.delete('/orders/<int:order_id>/payments/<int:payment_id>')
def payment_delete(order_id, payment_id):
    """Spec 26：删除一笔收款（D4 只进不退：不回退状态、不撤销归档）。"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    ok, err = db.delete_payment(payment_id)
    if not ok:
        code = 404 if '不存在' in (err or '') else 400
        return jsonify({'success': False, 'error': err or '删除失败'}), code
    return _payments_resp(order_id, 'delete', '收款记录已删除')


# ── Spec 26 阶段 5 统计实验室（D8：开发版专用；双闸 + 白名单构建器，打包版不含此功能）──


def _stats_lab_enabled() -> bool:
    """双闸：data_dir 下 .dev 标记文件（优先，开发机常开）或 OIMIMO_DEV=1 环境变量。

    运行时每次现取 db.data_dir()，不固化导入期路径（测试可重定向隔离）。
    """
    return (os.path.exists(os.path.join(db.data_dir(), '.dev'))
            or os.environ.get('OIMIMO_DEV') == '1')


# 白名单字典：进入 SQL 的标识符/片段只允许出自此处（安全红线：禁止任意 SQL 字符串；
# 过滤值/时间值一律 ？ 参数绑定 —— AGENTS.md「SQL 参数化」强制规则）
STATS_LAB_SOURCES = {
    'orders': {
        'from': 'FROM orders',
        'columns': {  # SUM/AVG 可聚合的金额列
            'actual_received': 'actual_received',
            'income': 'income',
            'discounted_income': 'discounted_income',
        },
        'filters': {  # 等值过滤维度
            'payment_mode': 'payment_mode', 'current_stage': 'current_stage',
            'payment_status': 'payment_status', 'is_archived': 'is_archived',
            'source': 'source', 'commission_type': 'commission_type',
        },
        'times': {  # 时间范围归属字段
            'scheduled_end': 'scheduled_end', 'created_at': 'created_at',
        },
        'default_time': 'scheduled_end',
    },
    'payments': {  # 收款流水 JOIN 订单（到账事件数据源）
        'from': 'FROM order_payments p JOIN orders o ON p.order_id = o.id',
        'columns': {
            'amount': 'p.amount',
            'actual_received': 'o.actual_received',
            'income': 'o.income',
            'discounted_income': 'o.discounted_income',
        },
        'filters': {
            'payment_mode': 'o.payment_mode', 'current_stage': 'o.current_stage',
            'payment_status': 'o.payment_status', 'is_archived': 'o.is_archived',
            'source': 'o.source', 'commission_type': 'o.commission_type',
        },
        'times': {
            'paid_at': 'p.paid_at', 'scheduled_end': 'o.scheduled_end',
            'created_at': 'o.created_at',
        },
        'default_time': 'paid_at',
    },
    'customers': {  # 客户维度（消费汇总口径）
        'from': 'FROM customers',
        'columns': {
            'total_spent': 'total_spent',
            'purchase_count': 'purchase_count',
        },
        'filters': {
            'tags': 'tags',
        },
        'times': {
            'created_at': 'created_at',
        },
        'default_time': 'created_at',
    },
}
STATS_LAB_AGGS = {'SUM', 'COUNT', 'AVG'}
STATS_LAB_GROUPS = {'month', 'year', 'category', 'source', 'stage'}
_STATS_LAB_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _stats_lab_build_sql(cfg):
    """实验室配置 dict → 参数化 SQL（sql, params）。白名单外任何键值 → ValueError（路由转 400）。"""
    if not isinstance(cfg, dict):
        raise ValueError('配置必须是 JSON 对象')
    source = cfg.get('source')
    if source not in STATS_LAB_SOURCES:
        raise ValueError(f'未知数据源：{source!r}')
    spec = STATS_LAB_SOURCES[source]

    # 聚合表达式：COUNT 无需列；SUM/AVG 需源白名单金额列
    agg = str(cfg.get('agg') or '').upper()
    if agg not in STATS_LAB_AGGS:
        raise ValueError(f'未知聚合：{cfg.get("agg")!r}')
    if agg == 'COUNT':
        agg_expr = 'COUNT(*)'
    else:
        column = cfg.get('column')
        col = spec['columns'].get(column) if isinstance(column, str) else None
        if col is None:
            raise ValueError(f'数据源 {source!r} 不支持金额列：{column!r}')
        agg_expr = f'{agg}({col})'

    # 等值过滤（键查白名单得列名，值参数绑定）
    filters = cfg.get('filters') or {}
    if not isinstance(filters, dict):
        raise ValueError('filters 必须是 JSON 对象')
    where_parts, params = [], []
    for key, value in filters.items():
        col = spec['filters'].get(key)
        if col is None:
            raise ValueError(f'数据源 {source!r} 不支持过滤维度：{key!r}')
        where_parts.append(f'{col} = ?')
        params.append(value)

    # 时间范围（归属字段查源白名单，未指定用源默认；月/年分组也基于它）
    date_range = cfg.get('date_range') or {}
    if not isinstance(date_range, dict):
        raise ValueError('date_range 必须是 JSON 对象')
    time_field = date_range.get('field') or spec['default_time']
    time_col = spec['times'].get(time_field)
    if time_col is None:
        raise ValueError(f'数据源 {source!r} 不支持时间字段：{time_field!r}')
    for bound in ('start', 'end'):
        val = date_range.get(bound)
        if val is None:
            continue
        if not _STATS_LAB_DATE_RE.match(str(val)):
            raise ValueError(f'{bound} 必须是 YYYY-MM-DD 日期')
        op = '>=' if bound == 'start' else '<='
        where_parts.append(f'date({time_col}) {op} date(?)')
        params.append(str(val))

    # 分组（月/年基于时间归属字段；品类/来源为维度列）
    group_expr = ''
    group_by = cfg.get('group_by')
    if group_by is not None:
        if group_by not in STATS_LAB_GROUPS:
            raise ValueError(f'未知分组维度：{group_by!r}')
        if group_by == 'month':
            group_expr = f"strftime('%Y-%m', {time_col})"
        elif group_by == 'year':
            group_expr = f"strftime('%Y', {time_col})"
        elif group_by == 'category':
            group_expr = spec['filters'].get('commission_type', 'commission_type')
        elif group_by == 'stage':
            group_expr = spec['filters'].get('current_stage', 'current_stage')
        else:  # source
            group_expr = spec['filters'].get('source', 'source')

    sql = 'SELECT '
    if group_expr:
        sql += f'{group_expr} AS label, '
    sql += f'{agg_expr} AS value {spec["from"]}'
    if where_parts:
        sql += ' WHERE ' + ' AND '.join(where_parts)
    if group_expr:
        sql += ' GROUP BY label ORDER BY label'
    return sql, params


# ── Spec 27 Phase 6：NL 意图解析（关键词匹配；白名单兼容，输出可直传 _stats_lab_build_sql）──

NL_INTENT_MAP = [
    # ── 数据源切换 ──
    (r'客户|买家',                {'source': 'customers'}),
    # ── 时间分组 ──
    (r'每月|按月|月度|各月',     {'group_by': 'month'}),
    (r'每年|年度|全年',         {'group_by': 'year'}),
    (r'今年|本年',              {'group_by': 'year'}),
    (r'按来源',                 {'group_by': 'source'}),
    (r'按类别|按稿件类别',        {'group_by': 'category'}),
    (r'按阶段|按状态',           {'group_by': 'stage'}),
    # ── 聚合方式 ──
    (r'笔数|订单数|单数|数量',  {'agg': 'COUNT'}),
    (r'平均|均价|单均',         {'agg': 'AVG'}),
    # ── 金额列（SUM/AVG 时使用）──
    (r'实际到账|实收|到账|均价',  {'column': 'actual_received'}),
    (r'消费|花费|累计消费',      {'column': 'total_spent', 'source': 'customers'}),
    (r'下单次数|购买次数',       {'column': 'purchase_count', 'source': 'customers', 'agg': 'COUNT'}),
    (r'收入|总额|总收入|金额',  {'agg': 'SUM'}),
    # ── 订单状态过滤 ──
    (r'已结算|完成',            {'filters': {'current_stage': '完成'}}),
    (r'退单|取消|退款',         {'filters': {'current_stage': '退单'}}),
    (r'活跃|进行中|在制',       {'filters': {'is_archived': '0'}}),
    # ── 收款方式过滤 ──
    (r'分期',                   {'filters': {'payment_mode': 'installment'}}),
    (r'整单',                   {'filters': {'payment_mode': 'simple'}}),
    # ── 动态时间范围（提取数字）──
    (r'最近\s*(\d+)\s*个?\s*月', {'_time_range': ('last_months', 1)}),
    (r'(\d{4})\s*年',            {'_time_range': ('year', 1)}),
]
# 未知关键词列表（出现在用户文本中即报警告）
_NL_UNKNOWN_KEYWORDS = ['分析', '业务', '报告', '趋势', '对比', '预测']


def _parse_nl_query(text: str) -> dict:
    """自然语言 → 统计实验室配置 dict（与 _stats_lab_build_sql 入参格式兼容）。

    返回 {'config': {...}, 'warnings': [...]} ；config 默认 source=orders。
    """
    if not text or not text.strip():
        return {'config': None, 'warnings': ['请输入统计需求']}
    text = text.strip()

    # 合并后的部分配置（最终拼成 cfg dict）
    partial = {}
    filters = {}
    date_range = {}
    warnings = []
    agg_set = False
    col_set = False

    for pattern, mapping in NL_INTENT_MAP:
        m = re.search(pattern, text)
        if not m:
            continue
        # 动态时间范围
        if '_time_range' in mapping:
            kind, group_idx = mapping['_time_range']
            try:
                num = int(m.group(group_idx))
            except (IndexError, ValueError):
                continue
            if kind == 'last_months':
                date_range['_kind'] = 'last_months'
                date_range['_n'] = num
            else:  # year
                date_range['_kind'] = 'year'
                date_range['_year'] = num
            continue
        # 数据源
        if 'source' in mapping:
            partial.setdefault('source', mapping['source'])
        # 聚合
        if 'agg' in mapping:
            if not agg_set:
                partial['agg'] = mapping['agg']
                agg_set = True
        # 金额列
        if 'column' in mapping:
            if not col_set:
                partial['column'] = mapping['column']
                col_set = True
                partial.setdefault('agg', 'SUM')  # 指定列默认 SUM
                agg_set = True
        # 分组
        if 'group_by' in mapping:
            partial.setdefault('group_by', mapping['group_by'])
        # 过滤
        if 'filters' in mapping:
            filters.update(mapping['filters'])

    # 未知关键词检测
    for kw in _NL_UNKNOWN_KEYWORDS:
        if kw in text:
            warnings.append(f'未识别：{kw}')

    cfg = {'source': 'orders'}
    cfg.update(partial)
    if filters:
        cfg['filters'] = filters
    if date_range:
        cfg['date_range'] = _nl_time_to_date_range(date_range)
    cfg.setdefault('agg', 'SUM')
    # SUM/AVG 必须搭配金额列；未显式指定时按数据源回退默认列
    if cfg['agg'] in ('SUM', 'AVG') and 'column' not in cfg:
        _default_col = {'customers': 'total_spent'}.get(cfg['source'], 'income')
        cfg['column'] = _default_col
    return {'config': cfg, 'warnings': warnings}


def _nl_time_to_date_range(tr: dict) -> dict:
    """NL 解析出的 _kind/_n/_year → 白名单兼容的 date_range dict。"""
    from datetime import date, timedelta
    kind = tr.get('_kind')
    if kind == 'last_months':
        today = date.today()
        n = tr.get('_n', 3)
        start = today.replace(day=1)
        for _ in range(n - 1):
            start = (start - timedelta(days=1)).replace(day=1)
        return {'field': 'scheduled_end', 'start': start.isoformat(), 'end': today.isoformat()}
    if kind == 'year':
        y = tr.get('_year', date.today().year)
        return {'field': 'scheduled_end', 'start': f'{y}-01-01', 'end': f'{y}-12-31'}
    return {}


def _nl_build_human_desc(cfg: dict, warnings: list) -> str:
    """根据解析结果生成人类可读描述（纯函数，不依赖 request）。"""
    parts = []
    gb = cfg.get('group_by')
    _gb_labels = {'month': '按月统计', 'year': '按年统计',
                   'source': '按来源统计', 'category': '按类别统计', 'stage': '按阶段统计'}
    if gb in _gb_labels:
        parts.append(_gb_labels[gb])
    dr = cfg.get('date_range', {})
    if dr.get('start') and dr.get('end') and not gb:
        parts.append(f"{dr['start']} ~ {dr['end']} 期间")
    # 按数据源选择实体名称
    src = cfg.get('source', 'orders')
    if src == 'customers':
        parts.append('客户')
    else:
        flt = cfg.get('filters', {})
        stage = flt.get('current_stage')
        if stage == '完成':
            parts.append('已结算订单')
        elif stage == '退单':
            parts.append('退单订单')
        elif flt.get('is_archived') == '0':
            parts.append('活跃订单')
        else:
            parts.append('订单')
        if flt.get('payment_mode'):
            parts.append(f"（{flt['payment_mode']}）")
    agg = cfg.get('agg', 'SUM')
    col = cfg.get('column', '')
    col_name = {'actual_received': '实际到账', 'income': '收入',
                 'total_spent': '累计消费', 'purchase_count': '下单次数'}.get(col, col or '数据')
    if agg == 'COUNT':
        parts.append('的笔数（COUNT）')
    elif agg == 'AVG':
        parts.append(f'的{col_name}（AVG）')
    else:
        parts.append(f'的{col_name}（SUM）')
    desc = ''.join(parts)
    if warnings:
        desc += f' ⚠️ {";".join(warnings)}'
    return desc


@app.get('/stats-lab')
def stats_lab_page():
    """统计实验室页面（双闸未过 → 404；三栏 UI：配置/口径预览/Chart.js 实时预览）。

    白名单结构摘要注入模板（单一事实源：改 STATS_LAB_SOURCES 前端选项自动跟随）；
    打包版模板已被 build.spec excludes 剥离，极端情况（打包 + 手动开闸）下
    模板缺失仍优雅 404（第三道保险）。
    """
    from flask import abort  # 函数内局部导入（对齐工具页 abort 先例）
    from jinja2 import TemplateNotFound  # 仅捕获模板缺失，不吞模板真实错误
    if not _stats_lab_enabled():
        abort(404)
    lab_options = {
        'sources': {name: {'columns': list(spec['columns'].keys()),
                           'filters': list(spec['filters'].keys()),
                           'times': list(spec['times'].keys()),
                           'default_time': spec['default_time']}
                    for name, spec in STATS_LAB_SOURCES.items()},
    }
    try:
        return render_template('stats_lab.html', lab_options=lab_options)
    except TemplateNotFound:
        # 模板缺失（打包版 excludes）→ 与闸门关闭同语义 404
        abort(404)


@app.post('/api/stats-lab/query')
def stats_lab_query():
    """白名单查询构建器执行（JSON → 参数化 SQL；响应附 sql/params 供公式预览与调试）。"""
    if not _stats_lab_enabled():
        return jsonify({'success': False, 'error': '统计实验室未启用'}), 404
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    cfg = request.get_json(silent=True)
    try:
        sql, params = _stats_lab_build_sql(cfg)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    conn = db.get_db()
    try:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        logger.exception('统计实验室查询执行失败: %s', sql)
        return jsonify({'success': False, 'error': '查询执行失败'}), 500
    finally:
        conn.close()
    return jsonify({'success': True, 'config': cfg, 'sql': sql,
                    'params': params, 'rows': rows})


@app.get('/api/stats-lab/export')
def stats_lab_export():
    """当前配置回显为可下载 JSON（先过构建器校验；为未来开放用户自定义统计留内核）。"""
    if not _stats_lab_enabled():
        return jsonify({'success': False, 'error': '统计实验室未启用'}), 404
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    try:
        cfg = json.loads(request.args.get('config', ''))
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'config 参数必须是合法 JSON'}), 400
    try:
        _stats_lab_build_sql(cfg)  # 仅校验，不执行
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    resp = jsonify(cfg)
    resp.headers['Content-Disposition'] = 'attachment; filename="stats-lab-config.json"'
    return resp


@app.post('/api/stats-lab/parse')
def stats_lab_parse():
    """自然语言 → 统计实验室配置（关键词匹配；双闸守卫 + Origin 校验）。"""
    if not _stats_lab_enabled():
        return jsonify({'success': False, 'error': '统计实验室未启用'}), 404
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get('text'), str):
        return jsonify({'success': False, 'error': '请求体必须包含 text 字符串'}), 400
    result = _parse_nl_query(data['text'])
    cfg = result.get('config')
    warnings = result.get('warnings', [])
    human_desc = _nl_build_human_desc(cfg, warnings) if cfg else ''
    return jsonify({'success': True, 'config': cfg, 'warnings': warnings,
                    'human_desc': human_desc})


# ── Spec 27 task-117：统计实验室预设 CRUD ──


@app.get('/api/stats-lab/presets')
def stats_lab_presets_list():
    """获取全部预设列表（双闸守卫 + Origin 校验）。"""
    if not _stats_lab_enabled():
        return jsonify({'success': False, 'error': '统计实验室未启用'}), 404
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    return jsonify({'success': True, 'presets': db.get_stats_lab_presets()})


@app.post('/api/stats-lab/presets')
def stats_lab_presets_create():
    """新建预设（name + config）。"""
    if not _stats_lab_enabled():
        return jsonify({'success': False, 'error': '统计实验室未启用'}), 404
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': '请求体必须是 JSON 对象'}), 400
    name = (data.get('name') or '').strip()
    config = data.get('config')
    if not name:
        return jsonify({'success': False, 'error': '预设名称不能为空'}), 400
    if not isinstance(config, dict):
        return jsonify({'success': False, 'error': 'config 必须是 JSON 对象'}), 400
    # 白名单校验（确保 config 可执行，防止保存无效配置）
    try:
        _stats_lab_build_sql(config)
    except ValueError as e:
        return jsonify({'success': False, 'error': f'配置校验失败：{e}'}), 400
    presets = db.get_stats_lab_presets()
    preset = {'id': str(uuid.uuid4())[:8], 'name': name, 'config': config}
    presets.append(preset)
    db._save_stats_lab_presets(presets)
    return jsonify({'success': True, 'preset': preset})


@app.put('/api/stats-lab/presets/<preset_id>')
def stats_lab_presets_update(preset_id):
    """更新预设（可改名 / 改配置）。"""
    if not _stats_lab_enabled():
        return jsonify({'success': False, 'error': '统计实验室未启用'}), 404
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': '请求体必须是 JSON 对象'}), 400
    presets = db.get_stats_lab_presets()
    target = next((p for p in presets if p.get('id') == preset_id), None)
    if not target:
        return jsonify({'success': False, 'error': '预设不存在'}), 404
    if 'name' in data:
        name = (data['name'] or '').strip()
        if not name:
            return jsonify({'success': False, 'error': '预设名称不能为空'}), 400
        target['name'] = name
    if 'config' in data:
        config = data['config']
        if not isinstance(config, dict):
            return jsonify({'success': False, 'error': 'config 必须是 JSON 对象'}), 400
        try:
            _stats_lab_build_sql(config)
        except ValueError as e:
            return jsonify({'success': False, 'error': f'配置校验失败：{e}'}), 400
        target['config'] = config
    db._save_stats_lab_presets(presets)
    return jsonify({'success': True, 'preset': target})


@app.delete('/api/stats-lab/presets/<preset_id>')
def stats_lab_presets_delete(preset_id):
    """删除预设。"""
    if not _stats_lab_enabled():
        return jsonify({'success': False, 'error': '统计实验室未启用'}), 404
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    presets = db.get_stats_lab_presets()
    new_presets = [p for p in presets if p.get('id') != preset_id]
    if len(new_presets) == len(presets):
        return jsonify({'success': False, 'error': '预设不存在'}), 404
    db._save_stats_lab_presets(new_presets)
    return jsonify({'success': True})


@app.post('/orders/<int:order_id>/delete')
def delete_order(order_id):
    db.delete_order(order_id)
    # 卡 156：boosted 表单提交（HX-Boosted）走 redirect 分支，仅显式 htmx 请求返回 JSON
    if request.headers.get('HX-Request') and not request.headers.get('HX-Boosted'):
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
    order = db.get_order(order_id)
    if not order:
        return jsonify({'error': '订单不存在'}), 404
    # Spec12：校验「本单快照阶段 + 退单」，而非全局 stage 选择列表
    # （自定义阶段名只在该订单快照中有效；快照外阶段拒绝，避免跨流程误切）
    allowed = set(db.get_order_stage_names(order)) | {db.get_refund_stage()}
    if new_stage not in allowed:
        return jsonify({'error': f'阶段「{new_stage}」不在本单流程中，请先在编辑页修改流程'}), 400
    db.update_stage(order_id, new_stage)
    order = db.get_order(order_id)
    resp = make_response(render_template('partials/kanban_card.html', order=order))
    # P20d：切到完成终态且未录工时 → 前端弹补录窗（看板走原生 fetch，需手动解析该头）
    if _needs_work_hours_prompt(order):
        resp.headers['HX-Trigger'] = json.dumps(
            {'promptWorkHours': {'id': order_id, 'name': order.get('project_name') or ''}})
    return resp


@app.route('/orders/<int:order_id>/work-hours', methods=['GET', 'POST'])
def order_work_hours(order_id):
    """P20d：完成补录工时弹窗。GET 渲染弹窗，POST 落库（action=save/exclude）。"""
    order = db.get_order(order_id)
    if not order:
        return jsonify({'error': '订单不存在'}), 404
    if request.method == 'GET':
        return render_template('partials/work_hours_prompt.html', order=order)

    action = request.form.get('action', 'save')
    if action == 'exclude':
        db.set_order_exclude_hourly(order_id)
        msg = '已设为不参与时薪统计'
    else:
        try:
            hours = float(request.form.get('work_hours') or 0)
        except (TypeError, ValueError):
            return '<p style="color:var(--color-danger);">工时格式无效，请输入数字</p>', 400
        if hours <= 0 or hours > 10000:
            return '<p style="color:var(--color-danger);">工时需在 0~10000 小时之间</p>', 400
        db.update_order_work_hours(order_id, hours)
        msg = f'已记录工时 {hours:g} 小时'
    return (
        '<div style="text-align:center;padding:24px 8px;">'
        f'<div style="font-size:2rem;">✓</div><p>{msg}</p></div>'
        '<script>setTimeout(function(){closeCenterModal();}, 600);</script>'
    )


def _normalize_sched_dt(value):
    """#52：规范化日历/甘特图拖拽传来的排期时间串。

    甘特图 on_date_change 把 Date 对象 JSON.stringify 成 UTC ISO 串
    （如 2026-08-02T16:00:00.000Z），日历非全天事件带 +08:00 偏移，
    这类值入库后编辑表单的 datetime-local 输入框无法渲染（显示空白）。
    统一转本地时间：整天边界（00:00 / 23:59）输出 YYYY-MM-DD，
    其余输出 YYYY-MM-DDTHH:MM；解析失败原样返回并记日志。
    """
    if not value or 'T' not in value:
        return value
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        logging.error('reschedule: 无法解析排期时间 %r', value)
        return value
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    if (dt.hour, dt.minute) in ((0, 0), (23, 59)):
        return dt.strftime('%Y-%m-%d')
    return dt.strftime('%Y-%m-%dT%H:%M')


@app.post('/orders/<int:order_id>/reschedule')
def reschedule_order(order_id):
    data = request.get_json()
    start = _normalize_sched_dt(data.get('start', ''))
    end = _normalize_sched_dt(data.get('end', start))
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
    skipped = 0
    try:
        if action == 'stage':
            # Spec12：value 必须是「被选订单快照 + 退单」中的有效阶段；
            # 具体跳过逻辑由 db.batch_update_stage 按每单快照判定，此处仅做非空校验
            if not value:
                return jsonify({'success': False, 'error': '未指定阶段'}), 400
            result = db.batch_update_stage(ids, value)              # P19-F5 整批单事务
            count, skipped = result['count'], result['skipped']
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

    resp = {'success': True, 'count': count}
    if skipped:
        resp['skipped'] = skipped
        resp['skip_ids'] = result.get('skip_ids', [])
        resp['warning'] = f'已更新 {count} 单，{skipped} 单因流程中无该阶段已跳过'
    return jsonify(resp)


@app.post('/customers')
def create_customer():
    try:
        data = CustomerCreate(**request.form).model_dump()
    except ValidationError as e:
        return f"数据校验失败: {e.errors()}", 400
    _normalize_customer_tags(data)
    try:
        customer_id = db.create_customer(data)
    except ValueError as e:
        # #40 P3：重名客户返 400 友好提示，不再 500
        logging.error("create_customer 路由失败: %s", e)
        return f"创建失败: {e}", 400
    return redirect(url_for('customer_detail', customer_id=customer_id))


@app.post('/customers/<int:customer_id>/edit')
def update_customer(customer_id):
    try:
        data = CustomerUpdate(**request.form).model_dump()
    except ValidationError as e:
        return f"数据校验失败: {e.errors()}", 400
    # Spec19：表单显式清空折扣 → 落库 NULL（model_dump 后为 None 会被下方过滤掉，需先放行）
    discount_cleared = 'discount_pct' in request.form and not str(request.form['discount_pct']).strip()
    data = {k: v for k, v in data.items() if v is not None and v != ''}
    if discount_cleared:
        data['discount_pct'] = None
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
        'commission_type': request.args.get('commission_type'),
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

    try:
        cid = db.create_customer({
            'name': name,
            'platform_url': request.form.get('quick_platform', ''),
            'preferences': request.form.get('quick_preferences', ''),
            'notes': request.form.get('quick_notes', ''),
        })
    except ValueError as e:
        # #40 P3：重名客户返 400 HTMX 友好提示，不再 500
        logging.error("api_quick_create_customer 失败: %s", e)
        return f'<span style="color:var(--color-danger);font-size:0.82rem;">{e}</span>', 400

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
    # #45 R2：「阶段色」分组已移除，阶段配色统一由「着色模式·按阶段」面板管理
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

    # #42：source/commission 是可自定义列表，色板行改为遍历实际 choices（含自定义项），
    # 默认色从 CALENDAR_PALETTES 取，取不到兜底灰（与 get_orders_for_calendar 的 default_color 一致）
    # #45 R2：stage 着色面板只显示流程中的活跃阶段（不含 orders 表历史遗留的孤儿阶段）
    # 收集所有流程中的阶段名 + 系统终态（已完成/已取消）
    _flow_stage_seen, _flow_stages = set(), []
    for _flow in db.get_stage_flows():
        for _s in _flow.get('stages', []):
            _n = _s.get('name', '') if isinstance(_s, dict) else ''
            if _n and _n not in _flow_stage_seen:
                _flow_stage_seen.add(_n)
                _flow_stages.append(_n)
    # 系统终态阶段始终保留（即使不在任何流程中）—— 使用动态名称而非硬编码
    for _sys in (db.get_done_stage(), db.get_refund_stage()):
        if _sys not in _flow_stage_seen:
            _flow_stages.append(_sys)
    # #42+#48+#50：source/commission 着色面板数据源必须与稿件类别/来源管理列表同步，
    # 不再使用 get_choices 的 auto-discover（会从 orders 表拉回已重命名的旧值）。
    # 追加：①有自定义颜色配置的孤儿值（保留颜色投资）②orders 表中仍活跃的孤儿值（防删除后丢失）
    def _synced_labels(settings_key, choice_type, prefix):
        """返回与管理列表同步的标签列表 + 有颜色配置或仍在 orders 中使用的孤儿值。"""
        raw = all_settings.get(settings_key, '')
        base = [x.strip() for x in raw.split(',') if x.strip()]
        if not base:
            base = db.get_choices(choice_type)  # 回退到注册表默认
        base_set = set(base)
        reg = db.CHOICE_REGISTRY.get(choice_type, {})
        field = reg.get('field', '')
        # 获取 orders 表中实际使用的值
        active_values = set()
        if field:
            try:
                conn = db.get_db()
                rows = conn.execute(
                    f"SELECT DISTINCT {field} FROM orders WHERE {field} IS NOT NULL AND {field} != ''"
                ).fetchall()
                conn.close()
                active_values = {r[0] for r in rows}
            except Exception:
                pass
        # ① 有自定义颜色配置 AND 仍在 orders 表中使用的孤儿值
        for key in all_settings:
            if key.startswith(prefix):
                orphan = key[len(prefix):]
                if orphan and orphan not in base_set and orphan in active_values:
                    base.append(orphan)
                    base_set.add(orphan)
        # ② orders 表中仍活跃但不在管理列表中的孤儿值（防止用户删除后着色/管理区丢失）
        for val in sorted(active_values):
            if val and val not in base_set:
                base.append(val)
                base_set.add(val)
        return base

    custom_mode_labels = {
        'source': _synced_labels('source_list', 'source', 'cal_source_'),
        'commission': _synced_labels('commission_type_list', 'commission_type', 'cal_commission_'),
        'stage': _flow_stages,
    }

    cal_palettes = {}
    for mode in cal_modes:
        mid = mode['id']
        prefix = f'cal_{mid}_'
        items = []
        defaults = default_palettes.get(mid, {})
        labels = custom_mode_labels.get(mid) or list(defaults.keys())
        for label in labels:
            default_color = defaults.get(label, '#b0b0aa')
            items.append({
                'key': f'{prefix}{label}',
                'label': label,
                'value': all_settings.get(f'{prefix}{label}', default_color),
                'default': default_color,
            })
        cal_palettes[mid] = items

    # #50：管理区与着色面板使用同一份同步列表，防止稿件类别/来源被删除后管理区为空
    commission_labels = custom_mode_labels['commission']
    source_labels = custom_mode_labels['source']
    # #50：稿件类别/来源在订单中的使用计数，用于前端删除保护
    commission_active_counts = {}
    source_active_counts = {}
    try:
        _conn = db.get_db()
        for r in _conn.execute(
            "SELECT commission_type, COUNT(*) AS cnt FROM orders "
            "WHERE commission_type IS NOT NULL AND commission_type != '' "
            "GROUP BY commission_type"
        ).fetchall():
            commission_active_counts[r['commission_type']] = r['cnt']
        for r in _conn.execute(
            "SELECT source, COUNT(*) AS cnt FROM orders "
            "WHERE source IS NOT NULL AND source != '' "
            "GROUP BY source"
        ).fetchall():
            source_active_counts[r['source']] = r['cnt']
        _conn.close()
    except Exception as e:
        logging.error('统计订单使用计数失败: %s', e)

    # 来源管理
    source_list = db.get_source_list()
    platform_sources = db.get_platform_sources()
    font_size = all_settings.get('font_size', '16px')
    font_family = all_settings.get('font_family', 'system')

    # P16j：自定义 CSS 主题（导入并存为预设）
    custom_themes = _load_custom_themes(all_settings)
    active_custom_theme = all_settings.get('active_custom_theme', '')
    theme_err = request.args.get('theme_err', '')

    # #45 R5：配色预设（整套 cal_* 配色命名保存/切换）；#47 记录使用中预设
    palette_presets = _load_palette_presets(all_settings)
    palette_err = request.args.get('palette_err', '')
    palette_active_id = all_settings.get('palette_active_id', '')

    return render_template('settings.html',
                           custom_themes=custom_themes,
                           active_custom_theme=active_custom_theme,
                           theme_err=theme_err,
                           palette_presets=palette_presets,
                           palette_err=palette_err,
                           palette_active_id=palette_active_id,
                           preset_themes=preset_themes,
                           groups=groups,
                           cal_modes=cal_modes,
                           cal_palettes=cal_palettes,
                           commission_labels=commission_labels,
                           commission_active_counts=commission_active_counts,
                           source_labels=source_labels,
                           source_active_counts=source_active_counts,
                           source_list=source_list,
                           platform_sources=platform_sources,
                           vip_discount_presets=db.get_vip_discount_presets(),  # Spec19
                           font_size=font_size,
                           font_family=font_family,
                           all_settings=all_settings,
                           db_path=db.DB_PATH,
                           data_recovery_needed=db.check_data_recovery_needed(),
                           backup_dir=db.get_backup_dir(),
                           default_backup_dir=db.DEFAULT_BACKUP_DIR,
                           backup_count=len(db.get_backup_list()),
                           feed_url=_build_feed_url(all_settings.get('feed_token', '')))  # Spec20 卡 86


@app.post('/settings')
def save_settings():
    data = dict(request.form)

    # Spec19 VIP 折扣预设：逗号分隔 → 校验每项 ∈ (0,100] → JSON 数组落库；
    # 全部非法时丢弃该键（保留原值），防整表设置保存被单个卡片拖垮
    if 'vip_discount_presets' in data:
        raw = data.pop('vip_discount_presets')
        presets = []
        for item in raw.split(','):
            item = item.strip()
            if not item:
                continue
            try:
                f = float(item)
            except ValueError:
                continue
            if 0 < f <= 100:
                presets.append(int(f) if f == int(f) else f)
        if presets:
            data['vip_discount_presets'] = json.dumps(presets)
        else:
            logging.warning('vip_discount_presets 保存跳过：无合法预设值（每项须 ∈ (0,100]）')

    # 来源管理
    if 'source_list_input' in data:
        data['source_list'] = data['source_list_input']
        del data['source_list_input']
    platform_keys = [k for k in data if k.startswith('platform_')]
    platform_vals = [data[k] for k in platform_keys]
    data['platform_sources'] = ','.join(platform_vals)
    for k in platform_keys:
        del data[k]
    # Spec 28 phase-14（task-132）：来源删除确认清单（D9 确认对话框产出，逗号分隔）
    confirmed_deletes = [x.strip() for x in
                         data.pop('confirmed_source_deletes', '').split(',') if x.strip()]

    # Spec20 卡 86：开关开启且 token 为空 → 自动生成（32 位 hex），随下方事务落库
    if data.get('feed_enabled') == '1' and not data.get('feed_token') \
            and not db.get_all_settings().get('feed_token'):
        data['feed_token'] = secrets.token_hex(16)

    # 重命名时同步更新 orders 表（收款状态已锁定，不需要同步）
    # P19-F5：重命名 + settings 更新包同一事务，中途失败整体回滚
    with db.transaction() as conn:
        db.sync_choice_renames(conn, data, 'commission_type_list', 'commission_type')
        renamed_src = db.sync_choice_renames(conn, data, 'source_list', 'source')
        # Spec 28 phase-14（task-132）：来源删除处理（D9——被删来源有引用须确认，
        # 确认后 orders.source 置空；未确认的被引用来源服务端保留防误删）
        amended_list, amended_plats = db.apply_source_deletions(
            conn, data.get('source_list'), data.get('platform_sources'), confirmed_deletes)
        if amended_list is not None:
            data['source_list'] = amended_list
        if amended_plats is not None:
            data['platform_sources'] = amended_plats
        # task-133 实测修复：被删来源的 cal_source_ 输入在日历面板另一处 DOM，
        # 不随来源项删除而移除，会在 update_settings 时复活刚清理的键——
        # 按最终 source_list 剔除孤儿 cal_source_/default_fee_ 表单键（顺带清旧孤儿）
        _final_sources = {x for x in (data.get('source_list') or '').split(',') if x}
        for _k in [k for k in data
                   if k.startswith(('cal_source_', 'default_fee_'))
                   and k.split('_', 2)[2] not in _final_sources
                   and k.split('_', 2)[2]]:
            del data[_k]
        db.update_settings(data, conn=conn)
        # P19-F9：来源重命名级联——新名称对应费率写入受影响订单快照并按快照重算（同事务）
        for _old, (_new, _ids) in renamed_src.items():
            db.resnapshot_fee_for_renamed_source(conn, _ids, _new)
    # Spec20：feed 开启即时生效（幂等，重复保存不双拉）；关闭需重启（spec §6 口径）
    if data.get('feed_enabled') == '1':
        start_feed_server()
    return redirect(url_for('settings_page'))


@app.post('/settings/feed-token/rotate')
def rotate_feed_token():
    """Spec20 卡 86 T20.8：重新生成 feed token。端点只认 settings 当前 token（无缓存），
    旋转后旧 URL 立即失效；页面刷新显示新 URL。"""
    new_token = secrets.token_hex(16)
    try:
        db.update_settings({'feed_token': new_token})
    except Exception as e:
        logging.error("rotate_feed_token 失败：%s", e)
        return jsonify({'success': False, 'error': '令牌更新失败，请重试'}), 500
    return jsonify({'success': True, 'token': new_token, 'url': _build_feed_url(new_token)})


@app.post('/settings/commission-merge')
def merge_commission_types_route():
    """类别合并：将多个旧类别统一为新名称，同步更新订单 + 设置列表 + 颜色配置。"""
    data = request.get_json(silent=True) or {}
    old_names = data.get('old_names', [])
    new_name = (data.get('new_name') or '').strip()
    if not old_names or not new_name:
        return jsonify({'success': False, 'error': '请选择要合并的类别并输入目标名称'}), 400
    if len(old_names) < 2:
        return jsonify({'success': False, 'error': '至少选择 2 个类别才能合并'}), 400
    try:
        result = db.merge_commission_types(old_names, new_name)
    except Exception as e:
        logging.error('类别合并失败: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500
    return jsonify({
        'success': True,
        'message': f'已将 {result["total"]} 单合并为「{new_name}」',
        'merged': result['merged'],
    })


@app.post('/settings/source-merge')
def merge_sources_route():
    """来源合并（Spec 28 task-132）：多个旧来源统一为新名称，同步订单 +
    source_list + 平台标记 + 费率配置 + 日历颜色 + 费率快照级联（单事务）。
    UI 接线归 task-133。"""
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源校验失败'}), 403
    data = request.get_json(silent=True) or {}
    old_names = data.get('old_names', [])
    new_name = (data.get('new_name') or '').strip()
    if not isinstance(old_names, list) or not old_names or not new_name:
        return jsonify({'success': False, 'error': '请选择要合并的来源并输入目标名称'}), 400
    cleaned = [o.strip() for o in old_names if isinstance(o, str) and o.strip()]
    if not cleaned:
        return jsonify({'success': False, 'error': '请选择要合并的来源并输入目标名称'}), 400
    if new_name in cleaned:
        return jsonify({'success': False, 'error': '目标名称不能与被合并来源相同'}), 400
    try:
        result = db.merge_sources(cleaned, new_name)
    except Exception as e:
        logging.error('来源合并失败: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500
    message = f'已将 {result["total"]} 单合并为「{new_name}」'
    resp = jsonify({'success': True, 'message': message, 'merged': result['merged']})
    # HX-Trigger json.dumps 默认 ensure_ascii → latin-1 安全（历史坑已备案）
    resp.headers['HX-Trigger'] = json.dumps({'showToast': message, 'refreshList': True})
    return resp


@app.post('/settings/stage-flows')
def save_stage_flows_route():
    """Spec12：保存阶段流程预设（JSON body）。成功返回 200 + Toast，失败返回 400。"""
    # 优先取 JSON body；为兼容 HTMX form fallback 也读 request.form['flows']（前端可任选）
    if request.is_json:
        flows = request.get_json(silent=True) or []
    else:
        raw = request.form.get('flows', '')
        try:
            flows = json.loads(raw) if raw else []
        except Exception:
            flows = None
    if not isinstance(flows, list):
        return jsonify({'error': 'flows 必须是数组'}), 400
    if not flows:
        return jsonify({'error': '至少需要一条流程'}), 400
    try:
        db.save_stage_flows(flows)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': f'保存失败: {e}'}), 500
    resp = jsonify({'success': True, 'count': len(flows)})
    resp.headers['HX-Trigger'] = json.dumps({'showToast': f'已保存 {len(flows)} 条流程预设（仅影响之后创建的订单）'})
    return resp


def _sync_choices_to_orders(data, settings_key, order_field):
    """兼容包装（P19-F5 起实现迁移至 db.sync_choice_renames；请优先在事务中调用后者）"""
    with db.transaction() as conn:
        return db.sync_choice_renames(conn, data, settings_key, order_field)


@app.post('/settings/reset')
def reset_settings_route():
    db.reset_settings()
    return redirect(url_for('settings_page'))


# ── #45 R1 日历着色模式偏好（微端点：避开 save_settings 对 platform_sources 的无条件重写） ──
@app.post('/settings/color-mode')
def save_color_mode():
    mode = (request.form.get('mode') or '').strip()
    if mode in ('source', 'stage', 'ddl', 'payment', 'commission'):
        db.update_settings({'calendar_color_mode': mode})
    return ('', 204)


# ── P21a 收入页自定义布局（微端点：同样避开 save_settings 对 platform_sources 的重写） ──
@app.post('/settings/income-layout')
def save_income_layout():
    raw = (request.form.get('layout') or '').strip()
    if raw == '':
        db.update_settings({'income_layout': ''})   # 空串 = 重置回模板默认
        return ('', 204)
    if len(raw) > 8000:
        return ('layout too large', 400)
    try:
        nodes = json.loads(raw)
    except ValueError:
        logging.error('P21a 布局保存失败：非法 JSON（长度 %d）', len(raw))
        return ('invalid json', 400)
    if not isinstance(nodes, list) or not nodes:
        return ('invalid layout', 400)
    clean = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        bid = str(n.get('id') or '').strip()
        if not bid or len(bid) > 40:
            continue
        try:
            clean.append({
                'id': bid,
                'x': max(0, min(11, int(n.get('x') or 0))),
                'y': max(0, min(999, int(n.get('y') or 0))),
                'w': max(1, min(12, int(n.get('w') or 1))),
                'h': max(1, min(200, int(n.get('h') or 1))),
            })
        except (TypeError, ValueError):
            continue
    if not clean:
        return ('invalid layout', 400)
    db.update_settings({'income_layout': json.dumps(clean, ensure_ascii=False)})
    return ('', 204)


# ── P22b 数据备份管理（微端点：JSON 响应，页面 fetch 直读） ──
@app.get('/settings/backups')
def get_backups():
    try:
        return jsonify({'success': True, 'backups': db.get_backup_list()})
    except Exception as e:
        logging.error('获取备份列表失败: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.post('/settings/backup')
def create_backup():
    try:
        ok, msg = db.create_manual_backup()
        if ok:
            return jsonify({'success': True, 'message': msg})
        return jsonify({'success': False, 'error': msg}), 500
    except Exception as e:
        logging.error('创建备份失败: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.post('/settings/backup-dir')
def save_backup_dir():
    """设置备份目录：校验路径存在且可写，保存到 settings 表。"""
    path = (request.form.get('path') or '').strip()
    ok, msg = db.set_backup_dir(path)
    return jsonify({'success': ok, 'message': msg})


@app.post('/settings/restore/<filename>')
def restore_backup_route(filename):
    try:
        ok, msg = db.restore_backup(filename)
        if ok:
            return jsonify({'success': True, 'message': msg})
        return jsonify({'success': False, 'error': msg}), 500
    except Exception as e:
        logging.error('恢复备份失败: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.post('/settings/backup/delete/<filename>')
def delete_backup_route(filename):
    try:
        ok, msg = db.delete_backup(filename)
        if ok:
            return jsonify({'success': True, 'message': msg})
        return jsonify({'success': False, 'error': msg}), 400
    except Exception as e:
        logging.error('删除备份失败: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


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


# ── #45 R5 配色预设：保存 / 选用 / 删除（仿 P16j 自定义主题三端点） ──
def _load_palette_presets(settings: dict):
    """解析 settings['palette_presets']（JSON 列表 [{id,name,colors}]），异常回退空列表。"""
    raw = settings.get('palette_presets', '')
    if not raw:
        return []
    try:
        presets = json.loads(raw)
        return [p for p in presets
                if isinstance(p, dict) and p.get('id') and isinstance(p.get('colors'), dict)]
    except Exception as e:
        logging.error(f'palette_presets 解析失败: {e}')
        return []


@app.post('/settings/palette/save')
def save_palette_preset():
    """把当前全部 cal_* 配色（五种模式所有标签色）打包命名保存为一套预设。"""
    name = (request.form.get('palette_name') or '').strip()
    if not name:
        return redirect(url_for('settings_page', palette_err='请填写预设名称'))
    settings = db.get_all_settings()
    # 只收 cal_ 前缀配色键；模式偏好 calendar_color_mode 是行为设置，不属于配色方案
    colors = {k: v for k, v in settings.items() if k.startswith('cal_')}
    presets = _load_palette_presets(settings)
    pid = 'palette_%d' % int(time.time() * 1000)
    presets.append({'id': pid, 'name': name, 'colors': colors})
    # #47：新存预设即当前配色，标记为使用中
    db.update_settings({
        'palette_presets': json.dumps(presets, ensure_ascii=False),
        'palette_active_id': pid,
    })
    return redirect(url_for('settings_page'))


@app.post('/settings/palette/apply')
def apply_palette_preset():
    """选用一套配色预设：整套覆盖写回当前 cal_* 值，立即生效。"""
    pid = request.form.get('palette_id') or ''
    settings = db.get_all_settings()
    for p in _load_palette_presets(settings):
        if p.get('id') == pid:
            updates = dict(p['colors'])
            updates['palette_active_id'] = pid  # #47：记录使用中预设
            db.update_settings(updates)
            break
    return redirect(url_for('settings_page'))


@app.post('/settings/palette/update')
def update_palette_preset():
    """#47：把当前已保存的 cal_* 配色覆盖写回指定预设（名称不变，原地更新）。"""
    pid = request.form.get('palette_id') or ''
    settings = db.get_all_settings()
    presets = _load_palette_presets(settings)
    colors = {k: v for k, v in settings.items() if k.startswith('cal_')}
    for p in presets:
        if p.get('id') == pid:
            p['colors'] = colors
            db.update_settings({
                'palette_presets': json.dumps(presets, ensure_ascii=False),
                'palette_active_id': pid,
            })
            break
    return redirect(url_for('settings_page'))


@app.post('/settings/palette/delete')
def delete_palette_preset():
    """删除一套配色预设（不影响当前生效颜色）。"""
    pid = request.form.get('palette_id') or ''
    settings = db.get_all_settings()
    presets = [p for p in _load_palette_presets(settings) if p.get('id') != pid]
    updates = {'palette_presets': json.dumps(presets, ensure_ascii=False)}
    if settings.get('palette_active_id') == pid:
        updates['palette_active_id'] = ''  # #47：删除使用中预设时清除标记
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

    # 尝试匹配已有客户：①平台 ID（稳定标识，客户改名不会误建重复客户）
    #                 ②名字精确匹配（无平台 ID 时兜底） ③未命中 → 新建
    customer_id = None
    matched_by = None          # 'platform_id' | 'name' | 'new'（随响应返回，插件端展示）
    renamed_from = None        # 检测到改名时记录旧名
    mhs_profile_url = f'https://www.mihuashi.com/profiles/{customer_mhs_id}' if customer_mhs_id else ''
    if mhs_profile_url:
        cust = db.get_customer_by_platform_url(mhs_profile_url)
        if cust:
            customer_id = cust['id']
            matched_by = 'platform_id'
            # 改名处理：抓取名字与库中不同 → 更新主名字，旧名字归档到 former_names
            if customer_name and customer_name != cust['name']:
                old_name = cust['name']
                names = [n.strip() for n in (cust.get('former_names') or '').split(',') if n.strip()]
                if old_name not in names:
                    names.insert(0, old_name)
                try:
                    db.update_customer(customer_id, {
                        'name': customer_name,
                        'former_names': ', '.join(names[:10]),  # 最多保留最近 10 个曾用名
                    })
                    renamed_from = old_name
                except Exception as e:
                    # 名字 UNIQUE 冲突等情况：保留原名，不阻塞订单导入
                    logging.warning("客户改名归档跳过：customer_id=%s old=%r new=%r err=%s",
                                    customer_id, old_name, customer_name, e)
    if not customer_id and customer_name:
        customers = db.list_customers(search=customer_name)
        for c in customers:
            if c['name'] == customer_name:
                customer_id = c['id']
                matched_by = 'name'
                break
        if not customer_id:
            try:
                cdata = {'name': customer_name}
                if mhs_profile_url:
                    cdata['platform_url'] = mhs_profile_url
                customer_id = db.create_customer(cdata)
                matched_by = 'new'
            except Exception as e:
                logging.warning("导入建客户失败：name=%r err=%s", customer_name, e)
    # 已有客户缺平台链接时补齐
    if customer_id and mhs_profile_url and matched_by != 'platform_id':
        try:
            cust = db.get_customer(customer_id)
            if cust and not cust.get('platform_url'):
                db.update_customer(customer_id, {'platform_url': mhs_profile_url})
        except Exception as e:
            logging.warning("老客户补平台链接失败：customer_id=%s err=%s", customer_id, e)

    # Spec19 §3.6：导入路径带出 VIP 客户折扣快照（订单级快照 D3，导入单无折扣输入框）
    import_discount = None
    if customer_id:
        try:
            if matched_by == 'platform_id' and cust:
                import_discount = cust.get('discount_pct')  # 平台 ID 匹配时 cust 已含全列
            else:
                _c = db.get_customer(customer_id)
                import_discount = _c.get('discount_pct') if _c else None
        except Exception as e:
            logging.warning("导入读取客户折扣失败：customer_id=%s err=%s", customer_id, e)

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
    if import_discount is not None:
        order_data['discount_pct'] = import_discount  # Spec19：非 NULL 才带出（NULL = 不打折，走默认路径）

    try:
        order_id = db.create_order(order_data)
        return jsonify({
            'success': True,
            'order_id': order_id,
            'project_name': project_name,
            'customer_matched_by': matched_by,
            'customer_renamed_from': renamed_from,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════
# 日历订阅 ICS feed（Spec20：卡 84 生成 + 端点；卡 85 第二监听 + 端口守卫；设置卡片属卡 86）
# ═══════════════════════════════════════════════════════════

def _ics_escape(text: str) -> str:
    """RFC 5545 TEXT 转义（D1）：反斜杠先行，再 ; , 与换行。"""
    return (str(text or '')
            .replace('\\', '\\\\')
            .replace(';', '\\;')
            .replace(',', '\\,')
            .replace('\r\n', '\n').replace('\r', '\n')
            .replace('\n', '\\n'))


def _ics_dt(s, end_of_day=False):
    """排期日期时间 → ICS 本地浮动时间（D7：无 Z 无 TZID）。

    兼容 'YYYY-MM-DD' 与 'YYYY-MM-DDTHH:MM'（#40 P1 精确时间模式）；
    纯日期：起点默认 000000、终点默认 235959。解析失败返回 None。
    """
    if not s:
        return None
    m = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2}))?', str(s).strip())
    if not m:
        return None
    date_part = f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
    if m.group(4) is not None:
        return f"{date_part}T{int(m.group(4)):02d}{m.group(5)}00"
    return f"{date_part}T235959" if end_of_day else f"{date_part}T000000"


def _build_ics(rows=None) -> str:
    """Spec20：生成未归档订单的 ICS 文本（rows 可复用外部已查结果，None 时自查）。

    D1 手写文本零依赖；D6 有排期→时间段事件 / 仅 page_deadline→VALUE=DATE 全日 / 都无→不输出；
    D8 UID = order-<id>@oimimo、SEQUENCE = updated_at 转 Unix 秒（失败降级 0）。
    Spec 28 Task 131：全日事件补 DTEND（RFC 5545 exclusive end = 次日）+ X-WR-CALNAME。
    Spec 29 Task 134（D3=PT15M）：头部补刷新建议双属性——REFRESH-INTERVAL（RFC 7986）
    + X-PUBLISHED-TTL（业界事实标准，Outlook 确定遵循）；Apple 对新订阅可能采纳、不追溯已订。
    行折行 75 字节暂不做（卡 86 T7 iOS 实测有问题再补）。
    """
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//oimimo scheduler//CN',
             'X-WR-CALNAME:oimimo 排单表',
             'REFRESH-INTERVAL;VALUE=DURATION:PT15M',
             'X-PUBLISHED-TTL:PT15M']
    dtstamp = datetime.now().strftime('%Y%m%dT%H%M%S')
    if rows is None:
        rows = db.list_feed_orders()
    for o in rows:
        start = _ics_dt(o.get('scheduled_start'))
        end = _ics_dt(o.get('scheduled_end'), end_of_day=True)
        allday = None
        if not start:
            # 无排期：仅截止日 → 全日事件；都无 → 不输出（D6）
            m = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})', str(o.get('page_deadline') or '').strip())
            if not m:
                continue
            allday = f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"
            # Task 131：全日事件 DTEND 为 exclusive end（DTSTART + 1 天）
            allday_end = (date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                          + timedelta(days=1)).strftime('%Y%m%d')
        seq = 0
        try:
            seq = int(datetime.strptime(str(o.get('updated_at') or ''), '%Y-%m-%d %H:%M:%S').timestamp())
        except (ValueError, TypeError):
            pass  # D8：解析失败降级 0，不阻塞 feed
        lines.append('BEGIN:VEVENT')
        lines.append(f"UID:order-{o['id']}@oimimo")
        lines.append(f"DTSTAMP:{dtstamp}")
        if start:
            lines.append(f"DTSTART:{start}")
            lines.append(f"DTEND:{end or start}")
        else:
            lines.append(f"DTSTART;VALUE=DATE:{allday}")
            lines.append(f"DTEND;VALUE=DATE:{allday_end}")
        summary = f"[{o.get('current_stage') or '未定'}] {o.get('project_name') or '未命名'}"
        if o.get('customer_name'):
            summary += f" · {o['customer_name']}"
        lines.append(f"SUMMARY:{_ics_escape(summary)}")
        desc = f"来源 {o.get('source') or '-'} · 实收 {float(o.get('actual_received') or 0):.2f}"
        notes = str(o.get('notes') or '').strip()[:100]
        if notes:
            desc += f" · {notes}"
        lines.append(f"DESCRIPTION:{_ics_escape(desc)}")
        lines.append(f"SEQUENCE:{seq}")
        lines.append('END:VEVENT')
    lines.append('END:VCALENDAR')
    return '\r\n'.join(lines) + '\r\n'


@app.route('/api/feed/orders.ics')
def api_feed_orders_ics():
    """Spec20 日历订阅端点（D4）：feed_enabled=1 且 ?token= 匹配 feed_token → ICS；否则 403。

    不走 _is_local_origin()——日历 App 直连无 Origin，token 即鉴权（import 类来源校验不受影响）。
    主端口可直访（便于本机调试）；1097 第二监听与端口守卫属卡 85。
    Spec 29 Task 134（D4）：ETag/304 条件请求 + Cache-Control: no-cache（客户端每次
    校验新鲜度，变了才拉全文）+ 一行访问日志（观测客户端来没来拉）。403 分支不加
    缓存头不记日志（不泄露任何信息）。
    """
    settings = db.get_all_settings()
    token = (settings.get('feed_token') or '').strip()
    if settings.get('feed_enabled', '0') != '1' or not token \
            or request.args.get('token', '') != token:
        # spec §3 A.4：403 也记一行（区分「客户端没来拉」vs「token 失效拉失败」）；
        # 日志仅落本地，不加 ETag/Cache-Control 响应头（客户端侧零信息泄露）。
        logging.info('feed 403 token-arg=%s… ua=%s',
                     (request.args.get('token') or '')[:4],
                     (request.headers.get('User-Agent') or '')[:60])
        return jsonify({'success': False, 'error': '订阅未开启或 token 无效'}), 403
    # ETag 取订单数据指纹（id + updated_at）而非 ICS 全文本 md5：DTSTAMP 每次生成都变，
    # 全文本指纹会让 ETag 永不命中、304 形同虚设；数据指纹在订单未变时稳定（spec §3 骨架修正，§7 备案）。
    rows = db.list_feed_orders()
    payload = ';'.join(f"{r['id']}@{r.get('updated_at') or ''}" for r in rows)
    etag = 'W/"%s"' % hashlib.md5(payload.encode('utf-8')).hexdigest()
    ua = (request.headers.get('User-Agent') or '')[:60]
    if request.headers.get('If-None-Match') == etag:
        logging.info('feed 304 token=%s… ua=%s', token[:4], ua)
        resp = make_response('', 304)
    else:
        logging.info('feed 200 token=%s… ua=%s', token[:4], ua)
        resp = make_response(_build_ics(rows))
        resp.headers['Content-Type'] = 'text/calendar; charset=utf-8'
        resp.headers['Content-Disposition'] = 'inline; filename="oimimo.ics"'
    resp.headers['ETag'] = etag
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


# ── Spec20 卡 85：第二监听 0.0.0.0:1097（局域网订阅通道）+ 端口守卫 ──

FEED_PORT = 1097  # D2：第二监听端口（写死集中，便于后改）

_feed_proc = None
_feed_thread = None  # frozen 模式降级线程引用
_feed_server_lock = threading.Lock()

# 子进程实际执行的代码：独立进程只服务 0.0.0.0:FEED_PORT。
# 端口被占（如 dev 热重载时旧 feed 进程尚未退出）→ 自重试接管。
_FEED_CHILD_CODE = f"""
import sys, time
sys.argv = ['feed']
import db as _db
_db.init_db()
import app as a
while True:
    try:
        a.app.run(host='0.0.0.0', port={FEED_PORT}, debug=False, use_reloader=False)
        break
    except OSError:
        time.sleep(2)
"""


def start_feed_server():
    """Spec20 D2/D5：feed_enabled=1 时启动第二监听（幂等）。

    - feed_enabled=0 → 完全不监听（最小暴露面），直接返回
    - 开发模式（源码）：独立子进程。线程与主服务共享 os.environ，清除
      WERKZEUG_* 会连带破坏主服务自身的 reloader 判定（误判非 reloader 上下文
      而二次嵌套启动）；子进程环境变量剔除 WERKZEUG_* 后也避免复用主服务监听 fd
    - 打包模式（frozen exe）：exe 无法执行 -c 代码，降级同进程守护线程
      （此时无 reloader、无 WERKZEUG_* 污染，线程方案安全）
    - 幂等：已有存活的 feed 进程/线程则不重复拉起
    - 关闭 feed 需重启进程（spec §6 热切换口径：开启即时生效，关闭不做热停）
    """
    global _feed_proc, _feed_thread
    with _feed_server_lock:
        if _feed_proc is not None and _feed_proc.poll() is None:
            return  # 已在运行（子进程模式）
        if _feed_thread is not None and _feed_thread.is_alive():
            return  # 已在运行（frozen 线程模式）
        try:
            if db.get_all_settings().get('feed_enabled', '0') != '1':
                return
        except Exception as e:
            logging.error("start_feed_server: 读取 feed_enabled 失败 %s", e)
            return
        if getattr(sys, 'frozen', False):
            def _run():
                try:
                    app.run(host='0.0.0.0', port=FEED_PORT, debug=False, use_reloader=False)
                except Exception as e:
                    logging.error("feed 第二监听 :%s 启动失败（端口被占？）%s", FEED_PORT, e)
            _feed_thread = threading.Thread(target=_run, name='feed-server', daemon=True)
            _feed_thread.start()
        else:
            env = {k: v for k, v in os.environ.items()
                   if k not in ('WERKZEUG_RUN_MAIN', 'WERKZEUG_SERVER_FD')}
            try:
                _feed_proc = subprocess.Popen(
                    [sys.executable, '-c', _FEED_CHILD_CODE], env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                logging.error("feed 第二监听拉起失败：%s", e)
                return
        logging.info("feed 第二监听拉起中：0.0.0.0:%s（仅放行 /api/feed/*）", FEED_PORT)


@atexit.register
def _stop_feed_server():
    """主进程退出时连带终止 feed 子进程（dev 热重载/launcher 关闭都走这里）。"""
    if _feed_proc is not None and _feed_proc.poll() is None:
        _feed_proc.terminate()


@app.before_request
def _feed_port_guard():
    """Spec20 D3：1097 第二监听只放行 /api/feed/*，页面/表单/静态资源永远到不了局域网。

    主端口全放行（含 feed，便于本机调试）；feed 端点自身仍过 token 校验（双层独立）。
    """
    if request.environ.get('SERVER_PORT') == str(FEED_PORT) \
            and not request.path.startswith('/api/feed/'):
        return jsonify({'success': False, 'error': '本端口仅提供日历订阅服务'}), 403


def _detect_lan_ip():
    """Spec20 卡 86：探测本机局域网出口 IP（UDP 连 8.8.8.8 不真发包，仅取路由出口）。

    失败（无网卡/离线）返回 None，模板显示 `<电脑IP>` 占位文案。
    """
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            s.settimeout(0.5)
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def _build_feed_url(token):
    """Spec20 卡 86：组装订阅 URL。LAN IP 探测失败 → `<电脑IP>` 占位。"""
    ip = _detect_lan_ip() or '<电脑IP>'
    return f'http://{ip}:{FEED_PORT}/api/feed/orders.ics?token={token}'


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


@app.route('/api/v1/health')
def api_health():
    """#41 健康检查：返回运行状态与当前版本（插件/外部工具探活用）"""
    return jsonify({'success': True, 'data': {'status': 'ok', 'version': APP_VERSION}})


@app.route('/api/log-error', methods=['POST'])
def api_log_error():
    """接收前端错误日志"""
    data = request.get_json(silent=True) or {}
    print(f"[FRONTEND ERROR] {data.get('time', '')} {data.get('type', '')}: {data.get('message', '')}",
          flush=True)
    return jsonify({'success': True})


@app.post('/api/maintenance/cleanup-images')
def cleanup_orphan_images():
    """Spec 30 D8：扫描 uploads/ 下图片文件，对比 DB 记录，删除孤立文件。

    安全措施：
    - 仅扫描 uploads/orders/ 和 uploads/pricelist/
    - 仅删除 original_*/preview_*/thumb_* 命名模式文件
    - 操作记日志，返回删除数量和释放空间
    """
    if not _is_local_origin():
        return jsonify({'success': False, 'error': '来源被拒绝'}), 403
    TRIPLE_RE = re.compile(r'^(original|preview|thumb)_')
    # 1. 收集 DB 中全部合法预览图路径（相对于 uploads/）
    valid_preview = set()
    conn = db.get_db()
    try:
        for row in conn.execute('SELECT image_url FROM order_images WHERE image_url IS NOT NULL'):
            # image_url 可能为 /uploads/orders/... 或 orders/...，统一去除前缀
            p = row[0].lstrip('/')
            if p.startswith('uploads/'):
                p = p[len('uploads/'):]
            valid_preview.add(p)
        for row in conn.execute('SELECT image_path FROM order_images WHERE image_path IS NOT NULL'):
            valid_preview.add(row[0])
        # orders 表封面图列
        for row in conn.execute('SELECT image_url FROM orders WHERE image_url IS NOT NULL'):
            p = row[0].lstrip('/')
            if p.startswith('uploads/'):
                p = p[len('uploads/'):]
            valid_preview.add(p)
        for row in conn.execute('SELECT image_path FROM orders WHERE image_path IS NOT NULL'):
            valid_preview.add(row[0])
        for row in conn.execute('SELECT image_path FROM pricelist_images WHERE image_path IS NOT NULL'):
            valid_preview.add(row[0])
    finally:
        conn.close()
    # 2. 从预览路径派生三件套完整路径集
    valid_files = set()
    for preview_rel in valid_preview:
        # 统一使用 / 分隔（DB 存储路径始终为 / 格式，跨平台兼容）
        preview_rel = preview_rel.replace('\\', '/')
        valid_files.add(preview_rel)
        # 用 / 分割而非 os.path.split（Windows 上 os.path.split 只认 \）
        idx = preview_rel.rfind('/')
        if idx >= 0:
            d = preview_rel[:idx]
            base = preview_rel[idx + 1:]
        else:
            d = ''
            base = preview_rel
        if base.startswith('preview_'):
            suffix = base[len('preview_'):]
            stem = suffix.rsplit('.', 1)[0]
            thumb_path = (d + '/thumb_' + suffix) if d else ('thumb_' + suffix)
            valid_files.add(thumb_path)
            original_dir = os.path.join(UPLOAD_DIR, d.replace('/', os.sep)) if d else UPLOAD_DIR
            for f in glob.glob(os.path.join(original_dir, 'original_' + stem + '.*')):
                valid_files.add(os.path.relpath(f, UPLOAD_DIR).replace('\\', '/'))
    # 3. 遍历磁盘，收集三件套命名文件
    orphan_files = []
    for scan_sub in ['orders', 'pricelist']:
        scan_dir = os.path.join(UPLOAD_DIR, scan_sub)
        if not os.path.isdir(scan_dir):
            continue
        for root, _dirs, files in os.walk(scan_dir):
            for fname in files:
                if not TRIPLE_RE.match(fname):
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, UPLOAD_DIR).replace('\\', '/')
                if rel not in valid_files:
                    orphan_files.append(fpath)
    # 4. 删除孤立文件
    deleted = 0
    freed_bytes = 0
    for fpath in orphan_files:
        try:
            sz = os.path.getsize(fpath)
            os.remove(fpath)
            deleted += 1
            freed_bytes += sz
            logging.info('[cleanup] deleted orphan: %s (%d bytes)', fpath, sz)
        except Exception as e:
            logging.error('[cleanup] failed to delete %s: %s', fpath, e)
    freed_mb = round(freed_bytes / (1024 * 1024), 2)
    logging.info('[cleanup] done: deleted=%d, freed=%.2f MB', deleted, freed_mb)
    return jsonify({'deleted': deleted, 'freed_mb': freed_mb})


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
    # Spec20 卡 85：feed 第二监听。debug=True 会带 reloader——监控（父）进程与
    # 服务（子）进程都会执行本块，仅真实服务进程（WERKZEUG_RUN_MAIN=true）拉起，
    # 防双绑 1097；若未来关 reloader，则直接拉起。
    _DEBUG = True
    if not _DEBUG or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        start_feed_server()
    app.run(debug=_DEBUG, host='127.0.0.1', port=port)
