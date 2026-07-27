"""排单工具 — 数据库层 (sqlite3)

连接管理、建表、CRUD 查询函数。
"""

import sqlite3
import os
import sys
import json
import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)


def data_dir() -> str:
    """数据目录：orders.db / exports/ 等可写文件所在处。
    PyInstaller 冻结时为 exe 所在目录（持久化）；开发时为 db.py 所在目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


DB_PATH = os.path.join(data_dir(), 'orders.db')


def get_db() -> sqlite3.Connection:
    """获取数据库连接（row_factory + 外键约束）

    P19-F10：journal_mode=WAL 为库级持久设置（写入 DB header），进程启动 init_db
    一次性设置即可，不再每连接重复执行；foreign_keys 是连接级非持久 pragma，
    必须每连接设置，否则外键约束（SET NULL/CASCADE）静默失效，故保留。
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction():
    """写入事务（P19-F5）：单连接，正常退出 commit、异常 rollback、始终 close。

    多步写入（插入+重算、重命名+设置更新、批量操作）必须包在同一事务里，
    中途失败整体回滚，不留半成品。业务函数通过 `conn=None` 参数复用事务连接。
    """
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """建表 + 索引（幂等）"""
    conn = get_db()
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;

        CREATE TABLE IF NOT EXISTS customers (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL UNIQUE,
            platform_url    TEXT,
            preferences     TEXT,
            notes           TEXT,
            tags            TEXT,
            total_spent     REAL    NOT NULL DEFAULT 0.0,
            purchase_count  INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS orders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id     INTEGER,
            project_name    TEXT    NOT NULL,
            source          TEXT    NOT NULL DEFAULT '米画师',
            is_commercial   INTEGER NOT NULL DEFAULT 0,
            commission_type TEXT,
            current_stage   TEXT    NOT NULL DEFAULT '待开始',
            ddl_status      TEXT    NOT NULL DEFAULT '正常',
            deposit         REAL    NOT NULL DEFAULT 0.0,
            balance         REAL    NOT NULL DEFAULT 0.0,
            platform_fee    REAL    NOT NULL DEFAULT 0.0,
            platform_fee_pct REAL,                          -- 手续费率快照 %（P19-F9：落库不回算，与设置页费率脱钩）
            income          REAL    NOT NULL DEFAULT 0.0,
            actual_received  REAL   NOT NULL DEFAULT 0.0,
            payment_status  TEXT    NOT NULL DEFAULT '未收款',
            is_archived     INTEGER NOT NULL DEFAULT 0,
            is_repeat       INTEGER NOT NULL DEFAULT 0,
            repeat_count    INTEGER NOT NULL DEFAULT 0,
            notes           TEXT,
            custom_color    TEXT,                             -- 日历自定义颜色 #RRGGBB
            platform_url    TEXT,                             -- 米画师项目链接
            page_deadline   TEXT,                             -- 页面截稿日（参考）
            image_url       TEXT,                             -- 作品预览图 URL（/uploads/orders/<id>/preview.webp）
            image_path      TEXT,                             -- 原图相对路径（orders/<id>/original<ext>）
            has_image       INTEGER NOT NULL DEFAULT 0,       -- 是否有作品图片
            completed_at    TEXT,                             -- 实际完成归档时间（P15a 统计口径）
            is_overdue      INTEGER NOT NULL DEFAULT 0,       -- 是否逾期完成（P15b）
            scheduled_start TEXT,
            scheduled_end   TEXT,
            sort_order      INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_orders_customer    ON orders(customer_id);
        CREATE INDEX IF NOT EXISTS idx_orders_stage       ON orders(current_stage);
        CREATE INDEX IF NOT EXISTS idx_orders_scheduled   ON orders(scheduled_start, scheduled_end);
        CREATE INDEX IF NOT EXISTS idx_orders_sort        ON orders(sort_order);
        CREATE INDEX IF NOT EXISTS idx_orders_archived    ON orders(is_archived);
        -- P19-F10：统计高频 WHERE 列索引（收入按 payment_status、归档月统计按 completed_at）；
        -- is_archived 已有单列索引，本地行数量级下复合索引收益不明显，取舍不加（注释备案）。
        CREATE INDEX IF NOT EXISTS idx_orders_completed_at   ON orders(completed_at);
        CREATE INDEX IF NOT EXISTS idx_orders_payment_status ON orders(payment_status);

        -- 订单多图表（P15d 一对多；orders 单图三列保留为封面兼容）
        CREATE TABLE IF NOT EXISTS order_images (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id    INTEGER NOT NULL,
            image_url   TEXT,                                 -- 预览图路径 /uploads/orders/<id>/imgs/<key>/preview.webp
            image_path  TEXT,                                 -- 原图相对路径 orders/<id>/imgs/<key>/original<ext>
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_order_images_order ON order_images(order_id, sort_order);

        -- 设置表（键值对存储主题色等配置）
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- 订单模板表（P18-F3：可复用字段快照，不污染 orders 表）
        CREATE TABLE IF NOT EXISTS order_templates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            data_json   TEXT    NOT NULL,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
        );
    """)

    # 写入默认设置（仅当不存在时）
    _ensure_default_settings(conn)

    # 迁移：为旧数据库补齐后续新增列（新建库已在 CREATE TABLE 中定义，此处幂等）
    for col in ('custom_color', 'platform_url', 'page_deadline', 'image_url', 'image_path'):
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} TEXT")
        except Exception:
            pass  # 列已存在
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN has_image INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass  # 列已存在
    # P19-F4 幽灵收款状态归一：历史默认值「待收款」及数字/空串污染 → 锁定值「未收款」。幂等。
    try:
        conn.execute("UPDATE orders SET payment_status='未收款' WHERE payment_status IN ('待收款','0','') OR payment_status = 0 OR payment_status IS NULL")
    except Exception:
        pass
    # P15a 迁移：完成归档时间与逾期标记
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN completed_at TEXT")
    except Exception:
        pass  # 列已存在
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN is_overdue INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass  # 列已存在
    # P19-F1 历史回填：已归档但缺完成时间的旧记录，scheduled_end 优先、updated_at 兜底；
    # 并同步回填 is_overdue（完成日晚于排期截止日 → 1）。幂等：仅处理 completed_at IS NULL / is_overdue=0 的行。
    try:
        conn.execute("""
            UPDATE orders
            SET completed_at = COALESCE(NULLIF(scheduled_end, ''), substr(updated_at, 1, 10))
            WHERE is_archived = 1 AND completed_at IS NULL
        """)
        conn.execute("""
            UPDATE orders
            SET is_overdue = 1
            WHERE is_archived = 1 AND is_overdue = 0
              AND scheduled_end IS NOT NULL AND scheduled_end != ''
              AND substr(completed_at, 1, 10) > scheduled_end
        """)
    except Exception:
        pass
    for col in ('tags',):
        try:
            conn.execute(f"ALTER TABLE customers ADD COLUMN {col} TEXT")
        except Exception:
            pass

    # P15d 回填：已有单图（has_image=1 且有 image_url）但 order_images 尚无记录的订单，
    # 将其封面图作为首图导入多图表（幂等：仅当该订单在 order_images 中无记录时插入）
    try:
        conn.execute("""
            INSERT INTO order_images (order_id, image_url, image_path, sort_order)
            SELECT o.id, o.image_url, o.image_path, 0
            FROM orders o
            WHERE o.has_image = 1 AND o.image_url IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM order_images oi WHERE oi.order_id = o.id)
        """)
    except Exception:
        pass

    # P19-F8 复购派生视图化：读取路径改查询时计算（_apply_repeat_for_rows），写入路径停更；
    # 两列暂留不删（spec 11 §7，两个版本后评估删除），此处按新口径（同客户他单、排除退单
    # 终态、排除本单）幂等回填，防旧版本回滚读到腐烂值。
    try:
        _refund = get_refund_stage()
        conn.execute("""
            UPDATE orders SET
              repeat_count = (SELECT COUNT(*) FROM orders o2
                              WHERE o2.customer_id = orders.customer_id
                                AND o2.id != orders.id AND o2.current_stage != ?),
              is_repeat = CASE WHEN (SELECT COUNT(*) FROM orders o2
                              WHERE o2.customer_id = orders.customer_id
                                AND o2.id != orders.id AND o2.current_stage != ?) > 0
                          THEN 1 ELSE 0 END
        """, (_refund, _refund))
    except Exception:
        pass

    # P19-F9 费率快照：新增 orders.platform_fee_pct 并按当前来源费率回填。
    # 口径：平台来源 → 设置键 default_fee_<source>（缺省 5.0，与订单表单默认值一致）；
    #       直接来源/无来源 → 0（无手续费语义）。幂等：仅处理 platform_fee_pct IS NULL 的行
    #       （列可 NULL，回填一次性覆盖全库；对账 SQL 见 spec 11 §6）。
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN platform_fee_pct REAL")
    except Exception:
        pass  # 列已存在
    try:
        _fee_kv = {
            r[0]: r[1] for r in conn.execute(
                "SELECT key, value FROM settings WHERE key LIKE 'default_fee_%' OR key = 'platform_sources'"
            ).fetchall()
        }
        _plats = {x.strip() for x in (_fee_kv.get('platform_sources') or '米画师,B站工坊,画加').split(',') if x.strip()}
        # 顺序：先按平台来源费率回填 NULL 行，再把剩余 NULL（直接来源/无来源）兜底置 0。
        for _src in _plats:
            try:
                _pct = float(_fee_kv.get(f'default_fee_{_src}', '5') or 5)
            except (TypeError, ValueError):
                _pct = 5.0
            conn.execute(
                "UPDATE orders SET platform_fee_pct = ? WHERE platform_fee_pct IS NULL AND source = ?",
                (_pct, _src)
            )
        conn.execute("UPDATE orders SET platform_fee_pct = 0 WHERE platform_fee_pct IS NULL")
    except Exception:
        pass

    conn.commit()
    conn.close()
    # 迁移：累计消费口径变更（仅统计已归档+已完成+已结算订单），一次性重算全部客户
    try:
        _mig_conn = get_db()
        _cids = [r[0] for r in _mig_conn.execute("SELECT id FROM customers").fetchall()]
        for _cid in _cids:
            _row = _mig_conn.execute(
                """SELECT COUNT(*) as cnt, COALESCE(SUM(actual_received), 0) as total
                   FROM orders
                   WHERE customer_id = ?
                     AND is_archived = 1
                     AND current_stage = '完成'
                     AND payment_status = '已结算'""",
                (_cid,)
            ).fetchone()
            _mig_conn.execute(
                "UPDATE customers SET total_spent = ?, purchase_count = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                (_row['total'], _row['cnt'], _cid)
            )
        _mig_conn.commit()
        _mig_conn.close()
    except Exception:
        pass
    # P19-F10：迁移可能改写 orders 数据（auto-discover 来源），启动期统一失效 choices 缓存
    _invalidate_choices_cache()
    # P20-F14：图片引用一致性自愈（用户绕过应用手动删 uploads 文件后，
    # order_images 记录与 orders 封面三列残留 → 画廊失效链接；启动时自动对齐）
    repair_image_consistency()

CHOICE_REGISTRY = {
    'stage': {
        'editable': False,  # 系统级，不暴露给设置页
        'default': '待开始,色稿,线稿,细化,收尾,完成,退单',
        'field': 'current_stage',  # orders 表中的字段名
    },
    'ddl': {
        'editable': False,
        'default': '正常,即将到期,🔴逾期,已完成✅,已退单',
        'field': 'ddl_status',
    },
    'payment': {
        'editable': False,  # 系统锁定，不可自定义
        'default': '已收定金,未收款,已结算,欠款,免收',
        'field': 'payment_status',
    },
    'source': {
        'editable': True,
        'default': '米画师,B站工坊,画加,微信,QQ,其他',
        'field': 'source',
    },
    'commission_type': {
        'editable': True,
        'default': '单人半身,色稿大头,双人横插,立绘,场景插画,Q版,服设,厚涂头像',
        'field': 'commission_type',
    },
}

# 需要特殊处理的"已收款"状态（用于收入统计 SQL）
PAID_STATUSES = {'已结算'}

# P19-F1 归档判定的收款状态集合：与收入统计拆分——
# 免收单「要归档但不计收入」，故 PAID_STATUSES 保持收入统计专用不动，归档判定用本集合。
ARCHIVE_PAID_STATUSES = {'已结算', '免收'}


# P19-F10 choices 进程内缓存：一次渲染 inject_constants 连调 5 类 get_choices，
# 每类含 settings 读取 + auto-discover 两条 SQL，列表/看板页逐请求线性放大（诊断 P1）。
# 单用户本地场景模块级 dict 即够：无跨进程竞争；Flask 单进程多线程下最坏情况是
# 并发各自重建一次缓存，结果一致，故无锁取舍（注释备案）。
_CHOICES_CACHE: dict = {}


def _invalidate_choices_cache():
    """choices 缓存统一失效入口（P19-F10）。

    挂载点：update_settings（写设置唯一入口）、sync_choice_renames（选项重命名改
    orders 字段值）、init_db（迁移改写数据）、create_order/create_order_with_template/
    update_order（订单写入可能带来 auto-discover 新值）。delete_order 不挂（值残留
    无害，下次任一失效点自然清理）。
    """
    _CHOICES_CACHE.clear()


def get_choices(choice_type):
    """统一读取选择列表：settings 表 ∪ 注册表 defaults（合并保序去重）+ auto-discover 追加。

    P19-F4 合并语义：不再 `result or fallback` 二选一——
    - base：settings 有自定义列表则用之（用户可删默认值），否则用注册表 defaults；
    - auto-discover：orders 表中出现的非默认值且不在 base 中的新值追加到末尾；
    - 任何情况下返回值都 ⊇ defaults（settings 未自定义时），stage/ddl 等系统列表
      不会因 orders 表出现非标准值而丢失标准选项。
    """
    reg = CHOICE_REGISTRY.get(choice_type, {})
    fallback = [x.strip() for x in reg.get('default', '').split(',') if x.strip()]

    # P19-F10：命中进程内缓存直接返回副本（防调用方原地修改污染缓存）
    cached = _CHOICES_CACHE.get(choice_type)
    if cached is not None:
        return list(cached)

    # 1. settings 表（用户自定义）；无自定义 → 注册表 defaults 作为 base
    try:
        settings = get_all_settings()
        raw = settings.get(f'{choice_type}_list', '')
        result = [x.strip() for x in raw.split(',') if x.strip()]
    except Exception:
        result = []
    base = result if result else list(fallback)

    # 2. auto-discover：只添加「用户手动输入的、不在注册表默认列表中」的新值
    #    即：用户在订单表单里手打了一个新类别 → orders 表有 → 自动加入下拉
    #    但不会把用户从 settings 中删除的旧值加回来
    field = reg.get('field')
    default_set = set(fallback)
    if field:
        try:
            conn = get_db()
            rows = conn.execute(
                f"SELECT DISTINCT {field} FROM orders WHERE {field} IS NOT NULL AND {field} != ''"
            ).fetchall()
            conn.close()
            for r in rows:
                v = r[0] if isinstance(r, tuple) else r[field]
                # 只添加不在默认列表中且不在 base 中的新值
                if v and v not in base and v not in default_set:
                    base.append(v)
        except Exception:
            pass

    # 3. 合并保序去重（base 可能来自 settings 含重复/空项的极端情况）
    seen, merged = set(), []
    for v in base:
        if v and v not in seen:
            seen.add(v)
            merged.append(v)
    _CHOICES_CACHE[choice_type] = merged
    return list(merged)


def get_paid_statuses():
    """已收款状态（系统锁定）"""
    return PAID_STATUSES


def get_archive_paid_statuses():
    """归档判定的收款状态（系统锁定，P19-F1：含免收；与收入统计集合拆分）"""
    return ARCHIVE_PAID_STATUSES


# P19-F2 终态元数据：位置魔法（stages[-2]/stages[-1]、ddl[0..4]）的确定性替代。
# stage/ddl 注册表均 editable=False（系统锁定，标准值固定）；get_choices auto-discover
# 从 orders 表追加的非标准阶段/DDL 值不在本表注册，一律视为非终态，不影响判定。
STAGE_META = {
    '待开始': {'terminal': False, 'kind': None,     'progress': 0},
    '色稿':   {'terminal': False, 'kind': None,     'progress': 20},
    '线稿':   {'terminal': False, 'kind': None,     'progress': 40},
    '细化':   {'terminal': False, 'kind': None,     'progress': 60},
    '收尾':   {'terminal': False, 'kind': None,     'progress': 80},
    '完成':   {'terminal': True,  'kind': 'done',   'progress': 100},
    '退单':   {'terminal': True,  'kind': 'refund', 'progress': 100},
}

DDL_STATUS_META = {
    '正常':    {'kind': 'normal'},
    '即将到期': {'kind': 'due'},
    '🔴逾期':  {'kind': 'overdue'},
    '已完成✅': {'kind': 'done'},
    '已退单':  {'kind': 'refund'},
}


def is_terminal_stage(stage: str) -> bool:
    """是否终态阶段（完成/退单）；auto-discover 的非标准阶段一律非终态"""
    return bool(STAGE_META.get(stage, {}).get('terminal'))


def is_refund_stage(stage: str) -> bool:
    """是否退单类终态"""
    return STAGE_META.get(stage, {}).get('kind') == 'refund'


def get_done_stage() -> str:
    """完成类终态的显示值"""
    for name, m in STAGE_META.items():
        if m.get('kind') == 'done':
            return name
    return '完成'


def get_refund_stage() -> str:
    """退单类终态的显示值"""
    for name, m in STAGE_META.items():
        if m.get('kind') == 'refund':
            return name
    return '退单'


def get_terminal_stages() -> set:
    """终态阶段集合（供 SQL NOT IN / 成员判定用）"""
    return {name for name, m in STAGE_META.items() if m.get('terminal')}


def get_stage_progress(stage: str) -> int:
    """阶段进度百分比（甘特/看板显示用）；未知阶段 0"""
    return int(STAGE_META.get(stage, {}).get('progress', 0))


def get_ddl_status(kind: str) -> str:
    """按语义 kind 取 DDL 状态显示值（normal/due/overdue/done/refund）"""
    for label, m in DDL_STATUS_META.items():
        if m.get('kind') == kind:
            return label
    return '正常'


def _paid_status_sql(field='payment_status'):
    """生成已收款状态的 SQL IN 条件 + 参数列表"""
    paid = get_paid_statuses()
    placeholders = ','.join(['?' for _ in paid])
    return f"{field} IN ({placeholders})", list(paid)


def _ensure_default_settings(conn):
    """确保 settings 表中有默认值"""
    defaults = {
        'theme_bg': '#f9f9f7',
        'theme_surface': '#fcfcfb',
        'theme_sidebar': '#f5f4f1',
        'theme_text': '#0b0b0b',
        'theme_text_secondary': '#52514e',
        'theme_border': '#e1e0d9',
        'theme_accent': '#0b0b0b',
        'theme_link': '#2a78d6',
        'theme_success': '#0ca30c',
        'theme_warning': '#fab219',
        'theme_danger': '#d03b3b',
        'stage_pending': '#898781',
        'stage_sketch': '#2a78d6',
        'stage_lineart': '#1baf7a',
        'stage_detail': '#eda100',
        'stage_finish': '#4a3aa7',
        'stage_completed': '#008300',
        'stage_cancelled': '#e34948',
        # 外观设置
        'font_size': '16px',
        'font_family': 'system',
        # 来源属性：哪些是平台来源（有手续费）
        'source_list': '米画师,B站工坊,画加,微信,QQ,其他',
        'platform_sources': '米画师,B站工坊,画加',
        'payment_status_list': '已收定金,未收款,已结算,欠款,免收',
        'commission_type_list': '单人半身,色稿大头,双人横插,立绘,场景插画,Q版,服设,厚涂头像',
        # Calendar color palettes (per mode) — validated against dataviz reference
        'cal_source_米画师': '#2a78d6',
        'cal_source_B站工坊': '#1baf7a',
        'cal_source_微信': '#eda100',
        'cal_source_其他': '#008300',
        'cal_stage_待开始': '#898781',
        'cal_stage_色稿': '#2a78d6',
        'cal_stage_线稿': '#1baf7a',
        'cal_stage_细化': '#eda100',
        'cal_stage_收尾': '#4a3aa7',
        'cal_stage_完成': '#008300',
        'cal_stage_退单': '#e34948',
        'cal_ddl_正常': '#0ca30c',
        'cal_ddl_即将到期': '#fab219',
        'cal_ddl_🔴逾期': '#d03b3b',
        'cal_ddl_已完成✅': '#898781',
        'cal_payment_已收定金': '#2a78d6',
        'cal_payment_未收款': '#fab219',
        'cal_payment_已结算': '#0ca30c',
        'cal_payment_欠款': '#d03b3b',
        'cal_payment_免收': '#008300',
    }
    for k, v in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
        )


# ═══════════════════════════════════════════════════════════
# 设置 CRUD
# ═══════════════════════════════════════════════════════════

def get_all_settings() -> dict:
    """获取所有设置，返回 {key: value}"""
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r['key']: r['value'] for r in rows}


def update_settings(data: dict, conn=None) -> None:
    """批量更新设置；传入事务连接则不 commit（P19-F5：重命名+设置同事务）。"""
    own = conn is None
    if own:
        conn = get_db()
    for k, v in data.items():
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v)
        )
    if own:
        conn.commit()
        conn.close()
    # P19-F10：写设置后统一失效 choices 缓存（source_list/commission_type_list 等即时生效）
    _invalidate_choices_cache()


def sync_choice_renames(conn, data: dict, settings_key: str, order_field: str) -> dict:
    """设置页改选择列表时，把 orders 表中「去 emoji 后同名」的旧值重命名为新值（P19-F5 纳入事务）。

    仅处理模糊匹配（去非文字字符后相等）的重命名；完全不匹配的旧值保留
    （由 get_choices auto-discover 继续展示）。在传入事务连接上执行，不 commit。
    P19-F9 唯一性护栏：去 emoji 后的目标名在新清单中**唯一**才合并；
    多候选（两个不同来源去 emoji 后同名）跳过并记日志，防订单来源字段被批量误并。
    返回 {旧值: (新值, [受影响订单 id])}，供调用方做级联（如来源费率快照刷新）。
    """
    renamed: dict = {}
    if settings_key not in data:
        return renamed
    import re
    new_values = {x.strip() for x in data[settings_key].split(',') if x.strip()}
    rows = conn.execute(
        f"SELECT DISTINCT {order_field} FROM orders WHERE {order_field} IS NOT NULL AND {order_field} != ''"
    ).fetchall()
    for row in rows:
        old_val = row[0]
        if not old_val or old_val in new_values:
            continue
        old_clean = re.sub(r'[^\w一-鿿]', '', old_val)
        matches = [nv for nv in new_values
                   if re.sub(r'[^\w一-鿿]', '', nv) == old_clean and nv != old_val]
        if len(matches) > 1:
            logger.warning('P19-F9 重命名护栏：%r 去 emoji 后匹配多个候选 %r，跳过合并', old_val, matches)
            continue
        if len(matches) == 1:
            nv = matches[0]
            ids = [r[0] for r in conn.execute(
                f"SELECT id FROM orders WHERE {order_field} = ?", (old_val,)
            ).fetchall()]
            conn.execute(
                f"UPDATE orders SET {order_field} = ?, updated_at = datetime('now','localtime') WHERE {order_field} = ?",
                (nv, old_val)
            )
            renamed[old_val] = (nv, ids)
    if renamed:
        # P19-F10：重命名改写 orders 字段值（auto-discover 来源），失效缓存
        _invalidate_choices_cache()
    return renamed


def get_default_fee_for_source(source: str) -> float:
    """P19-F9：来源默认费率 %（设置键 default_fee_<source>，缺省 5.0，与订单表单默认值一致）；
    非平台来源/空来源 → 0.0（无手续费语义）。读已提交设置（自开连接），供写入管线使用。"""
    if not source or source not in get_platform_sources():
        return 0.0
    try:
        return float(get_all_settings().get(f'default_fee_{source}', '5') or 5)
    except (TypeError, ValueError):
        return 5.0


def resnapshot_fee_for_renamed_source(conn, order_ids: list, new_source: str) -> int:
    """P19-F9 来源重命名级联：受影响订单 pct 快照刷新为新来源默认费率，
    并按快照重算 platform_fee / actual_received（income 不变），涉及客户统计同步重算。
    在传入事务连接上执行（与重命名/设置更新同事务，卡55 单管线）。
    只处理本次重命名实际波及的订单 id——原本就是新名称的订单不动（历史快照不改写）。
    """
    if not order_ids:
        return 0
    row = conn.execute("SELECT value FROM settings WHERE key = 'platform_sources'").fetchone()
    platforms = {x.strip() for x in (row[0] if row else '米画师,B站工坊,画加').split(',') if x.strip()}
    pct = 0.0
    if new_source in platforms:
        fee_row = conn.execute("SELECT value FROM settings WHERE key = ?", (f'default_fee_{new_source}',)).fetchone()
        try:
            pct = float((fee_row[0] if fee_row else '5') or 5)
        except (TypeError, ValueError):
            pct = 5.0
    marks = ','.join('?' * len(order_ids))
    rows = conn.execute(
        f"SELECT id, customer_id, deposit, balance FROM orders WHERE id IN ({marks})",
        list(order_ids)
    ).fetchall()
    cids = set()
    for r in rows:
        income = float(r['deposit'] or 0) + float(r['balance'] or 0)
        fee = round(income * pct / 100, 2) if pct > 0 else 0.0
        conn.execute(
            "UPDATE orders SET platform_fee_pct = ?, platform_fee = ?, actual_received = ?, "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (pct, fee, round(income - fee, 2), r['id'])
        )
        if r['customer_id']:
            cids.add(r['customer_id'])
    for cid in cids:
        recalc_customer_stats(cid, conn=conn)
    return len(rows)


# 订单模板可复用字段白名单（P18-F3：排除日期与金额派生）
ORDER_TEMPLATE_FIELDS = (
    'customer_id', 'project_name', 'source', 'is_commercial',
    'commission_type', 'current_stage', 'payment_status',
    'platform_url', 'deposit', 'balance', 'platform_fee_pct', 'notes',
)


def _snapshot_template_data(form: dict) -> dict:
    """从表单提取可复用字段快照（仅保留白名单中非空值）。"""
    snap = {}
    for k in ORDER_TEMPLATE_FIELDS:
        v = form.get(k)
        if v is None:
            continue
        if isinstance(v, str) and v.strip() == '':
            continue
        snap[k] = v
    return snap


def create_order_template(name: str, data: dict) -> int:
    """保存订单模板：name + 可复用字段快照(JSON)。返回模板 ID。"""
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO order_templates (name, data_json) VALUES (?, ?)",
        (name, json.dumps(data, ensure_ascii=False))
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def _parse_template_row(row) -> dict:
    d = dict(row)
    try:
        d['data'] = json.loads(d.get('data_json') or '{}')
    except Exception:
        d['data'] = {}
    return d


def list_order_templates() -> list[dict]:
    """模板列表（含解析后的 data），按创建时间倒序。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, data_json, created_at FROM order_templates ORDER BY created_at DESC, id DESC"
    ).fetchall()
    conn.close()
    return [_parse_template_row(r) for r in rows]


def get_order_template(template_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT id, name, data_json, created_at FROM order_templates WHERE id = ?",
        (template_id,)
    ).fetchone()
    conn.close()
    return _parse_template_row(row) if row else None


def delete_order_template(template_id: int) -> None:
    conn = get_db()
    conn.execute("DELETE FROM order_templates WHERE id = ?", (template_id,))
    conn.commit()
    conn.close()


def update_order_template(template_id: int, name: str, data: dict) -> None:
    """全量更新模板：名称 + 可复用字段快照(JSON)。"""
    conn = get_db()
    conn.execute(
        "UPDATE order_templates SET name = ?, data_json = ? WHERE id = ?",
        (name, json.dumps(data, ensure_ascii=False), template_id)
    )
    conn.commit()
    conn.close()


def get_source_list() -> list[str]:
    """获取所有来源列表（从设置读取）"""
    try:
        s = get_all_settings()
        raw = s.get('source_list', '米画师,B站工坊,画加,微信,QQ,其他')
        return [x.strip() for x in raw.split(',') if x.strip()]
    except Exception:
        return ['米画师', 'B站工坊', '画加', '微信', 'QQ', '其他']


def get_platform_sources() -> set[str]:
    """获取平台来源集合（从设置读取）"""
    try:
        s = get_all_settings()
        raw = s.get('platform_sources', '米画师,B站工坊,画加')
        return {x.strip() for x in raw.split(',') if x.strip()}
    except Exception:
        return {'米画师', 'B站工坊', '画加'}


def reset_settings() -> None:
    """重置所有设置为默认值"""
    conn = get_db()
    conn.execute("DELETE FROM settings")
    conn.commit()
    _ensure_default_settings(conn)
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════
# 订单 CRUD
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
# 归档写入唯一入口（P19-F1）
# 约定：全库禁止裸写 is_archived=1 的 UPDATE 或字典赋值，
#       DB 直写一律经 set_archived()，管线字典一律经 _apply_archive_to_data()。
# completed_at / is_overdue 为事件快照：已归档单不回算（spec 11 §4）。
# ═══════════════════════════════════════════════════════════


def _calc_is_overdue_flag(scheduled_end, completed_at: str) -> int:
    """逾期完成判定：有排期截止日且 completed_at 晚于该日 → 1。"""
    if not scheduled_end or not completed_at:
        return 0
    try:
        return 1 if completed_at[:10] > str(scheduled_end)[:10] else 0
    except (TypeError, IndexError):
        return 0


def _apply_archive_to_data(data: dict, completed_at: str | None = None) -> dict:
    """管线字典级归档（唯一入口的内存形态）。

    completed_at 优先级：data 已有值（已归档单快照不回算）→ 参数（用户确认归属月）→ 今日。
    已归档单（data 已带 completed_at）的 is_overdue 同样保留快照不重算。
    """
    existing_completed = data.get('completed_at')
    final_completed = existing_completed or completed_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    data['is_archived'] = 1
    data['completed_at'] = final_completed
    if existing_completed:
        data['is_overdue'] = int(data.get('is_overdue') or 0)
    else:
        data['is_overdue'] = _calc_is_overdue_flag(data.get('scheduled_end'), final_completed)
    return data


def set_archived(conn, order_id: int, archived: bool, completed_at: str | None = None, is_overdue: int | None = None) -> None:
    """归档写入唯一入口（DB 直写形态）。

    归档：completed_at 参数优先（用户确认归属月）→ 已有快照 → 今日；
         is_overdue 显式传入则用传入值（P15b/P18-F7 确认链），缺省按 scheduled_end 与 completed_at 比较。
    取消归档：completed_at / is_overdue 同步清空。
    """
    if archived:
        row = conn.execute(
            "SELECT scheduled_end, completed_at, is_overdue FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if row and row['completed_at'] and completed_at is None:
            # 已归档单：completed_at 快照不回算；is_overdue 缺省同样保留快照
            final_completed = row['completed_at']
            overdue_flag = int(row['is_overdue'] or 0) if is_overdue is None else (1 if is_overdue else 0)
        else:
            final_completed = completed_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            overdue_flag = (1 if is_overdue else 0) if is_overdue is not None \
                else _calc_is_overdue_flag(row['scheduled_end'] if row else None, final_completed)
        conn.execute(
            "UPDATE orders SET is_archived = 1, completed_at = ?, is_overdue = ?, "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (final_completed, overdue_flag, order_id))
    else:
        conn.execute(
            "UPDATE orders SET is_archived = 0, completed_at = NULL, is_overdue = 0, "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (order_id,))


def _auto_calc_ddl_status(data: dict):
    """根据日期和阶段自动计算 DDL 状态 + 自动归档（动态选择）"""
    from datetime import date as dt_date
    today = dt_date.today()
    stage = data.get('current_stage', '')
    end_str = data.get('scheduled_end', '')
    payment = data.get('payment_status', '')

    paid = get_archive_paid_statuses()  # P19-F1：归档判定集合（含免收），与收入统计拆分

    # P19-F2：DDL/阶段取值改走元数据（取代 ddl[0..4]、stages[-2]/stages[-1] 位置魔法）
    ddl_normal = get_ddl_status('normal')
    ddl_due = get_ddl_status('due')
    ddl_overdue = get_ddl_status('overdue')
    ddl_done = get_ddl_status('done')
    ddl_cancelled = get_ddl_status('refund')
    stage_done = get_done_stage()
    stage_cancelled = get_refund_stage()

    # 完成 + 已结算/免收 → 自动归档（经唯一入口，写 completed_at/is_overdue）
    if stage == stage_done and payment in paid:
        data['ddl_status'] = ddl_done
        return _apply_archive_to_data(data)

    if stage == stage_done:
        data['ddl_status'] = ddl_done
        return data

    # 退单 + 已结算/免收 → 自动归档（免费退单不计入收入但需归档整理）
    if stage == stage_cancelled and payment in paid:
        data['ddl_status'] = ddl_cancelled
        return _apply_archive_to_data(data)

    if stage == stage_cancelled:
        data['ddl_status'] = ddl_cancelled
        return data

    if end_str and end_str.strip():
        try:
            end_date = dt_date.fromisoformat(end_str)
            diff = (end_date - today).days
            if diff < 0:
                data['ddl_status'] = ddl_overdue
            elif diff <= 3:
                data['ddl_status'] = ddl_due
            else:
                data['ddl_status'] = ddl_normal
        except (ValueError, TypeError):
            data['ddl_status'] = ddl_normal
    else:
        data['ddl_status'] = ddl_normal

    return data


def _calc_financials(data: dict) -> dict:
    """自动计算 income、platform_fee、actual_received

    支持两种输入：
    - platform_fee_pct: 百分比（如 5 表示 5%），自动计算 platform_fee
    - platform_fee: 直接金额（兼容旧数据）

    直接来源（微信/QQ/其他）无手续费，platform_fee 强制为 0。
    P19-F9：platform_fee_pct 为订单级快照——随单落库、重算只读订单自身快照，
    与设置页当前费率脱钩（修 C5：设置页改费率静默改写历史财务）。
    """
    deposit = float(data.get('deposit', 0) or 0)
    balance = float(data.get('balance', 0) or 0)
    income = deposit + balance
    data['income'] = income

    source = data.get('source', '')
    is_platform = source in get_platform_sources()

    if not is_platform:
        # 直接来源：无手续费
        data['platform_fee'] = 0.0
    else:
        # P19-F9：pct 快照保留在 data 中随单落库（不再 pop）。
        # pct>0 → 按快照重算手续费；pct 空/0 → 保留传入 platform_fee（兼容直填金额旧路径）。
        pct = data.get('platform_fee_pct')
        if pct is not None and float(pct or 0) > 0:
            data['platform_fee'] = round(income * float(pct) / 100, 2)
        else:
            data['platform_fee'] = float(data.get('platform_fee', 0) or 0)

    data['actual_received'] = round(data['income'] - data['platform_fee'], 2)
    return data


# P19-F8：写入时复购检测 _auto_detect_repeat 已删除（旧口径含退单且写入即腐烂，诊断 A3）。
# 复购标记改为查询时计算，见 _apply_repeat_for_rows（_refresh_ddl_for_rows 旁）；
# 落库两列暂留仅作旧版本回滚兜底，由 init_db 幂等回填维持新口径。



def create_order(data: dict) -> int:
    """创建订单，返回新 ID（P19-F5：财务→DDL/归档→插入→客户重算 单事务；
    P19-F8 起复购改查询时计算，不再写入落库）。"""
    with transaction() as conn:
        data = _calc_financials(data)
        # 自动计算 DDL 状态 + 自动归档
        data = _auto_calc_ddl_status(data)
        columns = [
            'customer_id', 'project_name', 'source', 'is_commercial',
            'commission_type', 'current_stage', 'ddl_status',
            'deposit', 'balance', 'platform_fee', 'platform_fee_pct', 'income', 'actual_received',
            'payment_status', 'is_archived',
            'notes', 'custom_color', 'platform_url', 'page_deadline', 'scheduled_start', 'scheduled_end', 'sort_order',
            'completed_at', 'is_overdue'
        ]
        nullable = {'customer_id', 'commission_type', 'notes', 'custom_color',
                    'platform_url', 'page_deadline', 'scheduled_start', 'scheduled_end', 'completed_at'}
        # P19-F4 文本列缺省写列 DEFAULT/''，禁止写数字 0（修复幽灵值来源之一）
        text_defaults = {'project_name': '', 'source': '米画师', 'current_stage': '待开始',
                         'ddl_status': '正常', 'payment_status': '未收款'}
        row = {}
        for k in columns:
            val = data.get(k)
            if val is None or val == '':
                if k in nullable:
                    val = None
                elif k in text_defaults:
                    val = text_defaults[k]
                else:
                    val = 0
            row[k] = val
        row['is_commercial'] = int(row['is_commercial'] or 0)
        row['is_archived'] = int(row['is_archived'] or 0)
        row['is_overdue'] = int(row['is_overdue'] or 0)
        row['sort_order'] = int(row['sort_order'] or 0)

        placeholders = ', '.join(['?' for _ in columns])
        cols_str = ', '.join(columns)
        cur = conn.execute(f"INSERT INTO orders ({cols_str}) VALUES ({placeholders})",
                           [row[c] for c in columns])
        order_id = cur.lastrowid

        # 更新客户统计（同事务）
        if row['customer_id']:
            recalc_customer_stats(row['customer_id'], conn=conn)

    # P19-F10：新订单可能带来 auto-discover 新值（手打来源/类别），失效缓存
    _invalidate_choices_cache()
    return order_id


def create_order_with_template(data: dict, template_name: str | None = None,
                               template_data: dict | None = None) -> int:
    """创建订单（可选同时保存模板）—— P19-F5 单事务：插入+recalc+模板入库 全成或全败。"""
    if not template_name:
        return create_order(data)
    with transaction() as conn:
        # 与 create_order 同管线，但复用同一事务连接
        data = _calc_financials(data)
        data = _auto_calc_ddl_status(data)
        columns = [
            'customer_id', 'project_name', 'source', 'is_commercial',
            'commission_type', 'current_stage', 'ddl_status',
            'deposit', 'balance', 'platform_fee', 'platform_fee_pct', 'income', 'actual_received',
            'payment_status', 'is_archived',
            'notes', 'custom_color', 'platform_url', 'page_deadline', 'scheduled_start', 'scheduled_end', 'sort_order',
            'completed_at', 'is_overdue'
        ]
        nullable = {'customer_id', 'commission_type', 'notes', 'custom_color',
                    'platform_url', 'page_deadline', 'scheduled_start', 'scheduled_end', 'completed_at'}
        text_defaults = {'project_name': '', 'source': '米画师', 'current_stage': '待开始',
                         'ddl_status': '正常', 'payment_status': '未收款'}
        row = {}
        for k in columns:
            val = data.get(k)
            if val is None or val == '':
                if k in nullable:
                    val = None
                elif k in text_defaults:
                    val = text_defaults[k]
                else:
                    val = 0
            row[k] = val
        row['is_commercial'] = int(row['is_commercial'] or 0)
        row['is_archived'] = int(row['is_archived'] or 0)
        row['is_overdue'] = int(row['is_overdue'] or 0)
        row['sort_order'] = int(row['sort_order'] or 0)
        placeholders = ', '.join(['?' for _ in columns])
        cols_str = ', '.join(columns)
        cur = conn.execute(f"INSERT INTO orders ({cols_str}) VALUES ({placeholders})",
                           [row[c] for c in columns])
        order_id = cur.lastrowid
        if row['customer_id']:
            recalc_customer_stats(row['customer_id'], conn=conn)
        # 模板快照同事务入库
        conn.execute(
            "INSERT INTO order_templates (name, data_json) VALUES (?, ?)",
            (template_name, json.dumps(template_data or {}, ensure_ascii=False))
        )
    # P19-F10：新订单可能带来 auto-discover 新值，失效缓存
    _invalidate_choices_cache()
    return order_id


def get_order(order_id: int, conn=None) -> dict | None:
    """获取单个订单；传入事务连接则复用（P19-F5）。复购标记查询时计算（P19-F8）。"""
    own = conn is None
    if own:
        conn = get_db()
    row = conn.execute(
        """SELECT o.*, c.name as customer_name
           FROM orders o
           LEFT JOIN customers c ON o.customer_id = c.id
           WHERE o.id = ?""",
        (order_id,)
    ).fetchone()
    result = _apply_repeat_for_rows([dict(row)], conn=conn)[0] if row else None
    if own:
        conn.close()
    return result


# update_order 金额/财务触发字段（P19-F5 单管线判定）
_MONEY_FIELDS = {'deposit', 'balance', 'platform_fee', 'platform_fee_pct', 'source'}
_DDL_TRIGGER_FIELDS = {'current_stage', 'scheduled_end', 'payment_status'}


def update_order(order_id: int, data: dict) -> bool:
    """更新订单（P19-F5 单管线 + 单事务）。

    流程：读旧单 → merge → financials（金额/来源/客户变化时）
    → ddl/archive（阶段/日期/收款变化或财务已重算时，归档经 _auto_calc_ddl_status 唯一入口）
    → 单次 UPDATE → 对 {新客户, 旧客户} 去重 recalc。全部在同一事务连接上。
    消除旧版 money 分支与 customer 分支的重复执行；修 A5（换客户旧客户不刷新）。
    P19-F8：repeat 环节移除，复购标记改查询时计算（_apply_repeat_for_rows），两列不再写入。
    """
    if not data:
        return False

    keys = set(data.keys())
    cust_changed = 'customer_id' in keys
    needs_financials = bool(_MONEY_FIELDS & keys) or cust_changed
    needs_ddl = bool(_DDL_TRIGGER_FIELDS & keys) or needs_financials

    with transaction() as conn:
        existing = get_order(order_id, conn=conn)
        if not existing:
            return False
        old_cid = existing.get('customer_id')

        merged = {**existing, **data}
        if needs_financials:
            # P19-F9 快照规则：显式提交 platform_fee_pct → 用之并随单落库；
            # 仅切换来源未给 pct → 按新来源默认费率刷新快照；
            # 两者皆无 → merged 继承 existing 快照原样保留（未显式变更不改写）。
            if 'source' in keys and 'platform_fee_pct' not in keys:
                merged['platform_fee_pct'] = get_default_fee_for_source(merged.get('source') or '')
            merged = _calc_financials(merged)
        if needs_ddl:
            # DDL 重算 + 「终态+已结算/免收」自动归档（内部经 _apply_archive_to_data）
            merged = _auto_calc_ddl_status(merged)

        merged['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # 过滤掉非 orders 表的列（如 JOIN 带来的 customer_name、查询时计算的 repeat 两列）
        order_columns = {
            'id', 'customer_id', 'project_name', 'source', 'is_commercial',
            'commission_type', 'current_stage', 'ddl_status',
            'deposit', 'balance', 'platform_fee', 'platform_fee_pct', 'income', 'actual_received',
            'payment_status', 'is_archived',
            'notes', 'custom_color', 'platform_url', 'page_deadline', 'scheduled_start', 'scheduled_end',
            'image_url', 'image_path', 'has_image',
            'completed_at', 'is_overdue',
            'sort_order', 'created_at', 'updated_at'
        }
        # 未触发任何管线时仅写传入字段（部分更新语义保留）；触发过管线则写全 merged
        payload = {k: v for k, v in merged.items() if k in order_columns} \
            if (needs_financials or needs_ddl) else {k: v for k, v in data.items() if k in order_columns}
        payload['updated_at'] = merged['updated_at']
        payload.pop('id', None)

        set_clause = ', '.join([f"{k} = ?" for k in payload.keys()])
        conn.execute(f"UPDATE orders SET {set_clause} WHERE id = ?",
                     list(payload.values()) + [order_id])

        # 客户统计：新旧客户集去重 recalc（修 A5：换客户旧客户也要刷新）
        new_cid = payload.get('customer_id', old_cid)
        for cid in {old_cid, new_cid}:
            if cid:
                recalc_customer_stats(cid, conn=conn)

    # P19-F10：订单更新可能带来 auto-discover 新值，失效缓存
    _invalidate_choices_cache()
    return True


def recompute_order(order_id: int) -> bool:
    """P17a 按现有字段用最新算法重算并保存派生状态。

    场景：历史订单按录入当时算法落库，阶段算法演进后出现不一致
    （应归档未归档、应逾期未标）。本函数取订单现有字段重跑
    `_calc_financials` → `_auto_calc_ddl_status`
    管线，只回写派生字段（财务/DDL/归档），
    **不改用户手填内容**（阶段、金额、日期、来源等原样保留）。
    归档沿用非破坏性约定（只置 1 从不置 0）。
    P19-F5：读+重算+写+recalc 收拢为单事务。
    P19-F8：复购不再回写（查询时计算，_apply_repeat_for_rows）。
    """
    with transaction() as conn:
        existing = get_order(order_id, conn=conn)
        if not existing:
            return False

        # 以现有字段为输入重跑管线（P19-F9：platform_fee_pct 快照随单落库，手续费按订单自身快照重算）
        data = dict(existing)
        data = _calc_financials(data)
        data = _auto_calc_ddl_status(data)

        # 只回写派生字段，用户手填内容保持不变
        derived = {
            'ddl_status': data.get('ddl_status'),
            'income': float(data.get('income') or 0),
            'platform_fee': float(data.get('platform_fee') or 0),
            'actual_received': float(data.get('actual_received') or 0),
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        # 归档非破坏性：仅在算法判定为归档时置 1，从不取消现有归档；
        # completed_at/is_overdue 经管线唯一入口（已归档单保留快照，缺失才回填）
        if int(data.get('is_archived') or 0) == 1:
            derived['is_archived'] = 1
            derived['completed_at'] = data.get('completed_at')
            derived['is_overdue'] = int(data.get('is_overdue') or 0)

        set_clause = ', '.join([f"{k} = ?" for k in derived.keys()])
        conn.execute(f"UPDATE orders SET {set_clause} WHERE id = ?",
                     list(derived.values()) + [order_id])
        if existing.get('customer_id'):
            recalc_customer_stats(existing['customer_id'], conn=conn)
    return True


def delete_order(order_id: int) -> bool:
    """删除订单（P19-F5：删除+客户重算单事务）"""
    with transaction() as conn:
        row = conn.execute("SELECT customer_id FROM orders WHERE id = ?", (order_id,)).fetchone()
        conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        if row and row['customer_id']:
            recalc_customer_stats(row['customer_id'], conn=conn)
    return True


# ═══════════════════════════════════════════════════════════
# 订单多图（P15d order_images 一对多）
# ═══════════════════════════════════════════════════════════

def get_order_images(order_id: int) -> list[dict]:
    """获取订单全部图片，按 sort_order/id 升序。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM order_images WHERE order_id = ? ORDER BY sort_order, id",
        (order_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_order_image(order_id: int, image_url: str, image_path: str) -> int:
    """追加一条订单图片记录，sort_order 取当前最大值+1，返回新记录 id。"""
    conn = get_db()
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next FROM order_images WHERE order_id = ?",
        (order_id,)).fetchone()
    next_sort = row['next'] if row else 0
    cur = conn.execute(
        "INSERT INTO order_images (order_id, image_url, image_path, sort_order) VALUES (?, ?, ?, ?)",
        (order_id, image_url, image_path, next_sort))
    conn.commit()
    image_id = cur.lastrowid
    conn.close()
    return image_id


def get_order_image(image_id: int) -> dict | None:
    """按 id 获取单条订单图片记录。"""
    conn = get_db()
    row = conn.execute("SELECT * FROM order_images WHERE id = ?", (image_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def remove_order_image(image_id: int) -> dict | None:
    """删除一条订单图片记录，返回被删记录（供调用方清理磁盘文件）。"""
    conn = get_db()
    row = conn.execute("SELECT * FROM order_images WHERE id = ?", (image_id,)).fetchone()
    if row:
        conn.execute("DELETE FROM order_images WHERE id = ?", (image_id,))
        conn.commit()
    conn.close()
    return dict(row) if row else None


def repair_image_consistency() -> dict:
    """图片引用一致性自愈（P20-F14）：启动时调用，幂等。

    场景：用户绕过应用直接删 uploads 下文件，或历史脏标记残留时，
    画廊仅凭 orders 封面三列出图，会展示失效链接。此处：
    1. 删除 order_images 中文件已缺失（或 url/path 双空）的记录；
    2. 封面候选订单重算封面三列——多图表有有效记录取首图；
       无记录则仅当自身 url/path 仍指向有效目标时保留，否则清空。
    返回 {'removed_images', 'cleared_covers', 'fixed_covers'} 计数供日志。
    """
    upload_dir = os.path.join(data_dir(), 'uploads')

    def _url_file_exists(url: str) -> bool:
        if url and url.startswith('/uploads/'):
            return os.path.isfile(os.path.join(upload_dir, url[len('/uploads/'):]))
        return False

    stats = {'removed_images': 0, 'cleared_covers': 0, 'fixed_covers': 0}
    with transaction() as conn:
        # 1. 清理失效 order_images 记录（path 非空但文件缺失；或 url/path 双空）
        dead_ids, affected = [], set()
        for r in conn.execute("SELECT id, order_id, image_url, image_path FROM order_images").fetchall():
            url, path = r['image_url'] or '', r['image_path'] or ''
            if path:
                if not os.path.isfile(os.path.join(upload_dir, path)):
                    dead_ids.append(r['id'])
                    affected.add(r['order_id'])
            elif not url:
                dead_ids.append(r['id'])
                affected.add(r['order_id'])
            # path 空但 url 非空：无法校验（外链或旧数据），保留
        for iid in dead_ids:
            conn.execute("DELETE FROM order_images WHERE id = ?", (iid,))
        stats['removed_images'] = len(dead_ids)

        # 2. 封面候选订单重算（含被删记录波及的订单）
        candidates = {r['id'] for r in conn.execute(
            "SELECT id FROM orders WHERE has_image = 1 "
            "OR (image_url IS NOT NULL AND image_url != '') "
            "OR (image_path IS NOT NULL AND image_path != '')").fetchall()}
        candidates |= affected
        for oid in candidates:
            first = conn.execute(
                "SELECT image_url, image_path FROM order_images WHERE order_id = ? "
                "ORDER BY sort_order, id LIMIT 1", (oid,)).fetchone()
            if first:
                conn.execute(
                    "UPDATE orders SET image_url = ?, image_path = ?, has_image = 1 WHERE id = ?",
                    (first['image_url'], first['image_path'], oid))
                stats['fixed_covers'] += 1
                continue
            # 多图表无记录：自身封面仍指向有效目标才保留，否则清空三列
            row = conn.execute(
                "SELECT image_url, image_path FROM orders WHERE id = ?", (oid,)).fetchone()
            if not row:
                continue
            url, path = row['image_url'] or '', row['image_path'] or ''
            keep = False
            if path and os.path.isfile(os.path.join(upload_dir, path)):
                keep = True
            elif url and not url.startswith('/uploads/'):
                keep = True  # 外部链接，无法校验，信任保留
            elif _url_file_exists(url):
                keep = True
            if keep:
                # 封面有效但 has_image 标记错误的，顺手纠正
                conn.execute("UPDATE orders SET has_image = 1 WHERE id = ? AND has_image != 1", (oid,))
                continue
            conn.execute(
                "UPDATE orders SET image_url = NULL, image_path = NULL, has_image = 0 WHERE id = ?",
                (oid,))
            stats['cleared_covers'] += 1
    if stats['removed_images'] or stats['cleared_covers'] or stats['fixed_covers']:
        logger.info('repair_image_consistency: %s', stats)
    return stats


def archive_order(order_id: int, completed_at: str | None = None, is_overdue: int = 0) -> bool:
    """切换归档状态。

    归档时（is_archived 0→1）记录 completed_at：
        - completed_at 为 None 时用当前时间（区间内完成 / 提前确认“是” / 逾期确认“是”）
        - 显式传入时采用该值（逾期“旧记录导入”按预设截止日期）
    is_overdue: 归档时是否标记为逾期工作（P15b）。
    取消归档时清空 completed_at 与 is_overdue。
    """
    with transaction() as conn:
        row = conn.execute("SELECT is_archived FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row:
            new_val = 0 if row['is_archived'] else 1
            # P19-F1：内部对齐到唯一入口 set_archived；确认链传入的 completed_at/is_overdue 原样生效
            set_archived(conn, order_id, new_val == 1, completed_at=completed_at, is_overdue=is_overdue)
    return True


def set_order_archived(order_id: int, archived: bool) -> bool:
    """直接归档开关（批量操作用）：经 set_archived 唯一入口，自动写/清 completed_at 与 is_overdue。"""
    with transaction() as conn:
        set_archived(conn, order_id, archived)
    return True


def _refresh_ddl_for_rows(rows: list[dict]) -> list[dict]:
    """读取时刷新 DDL 状态（处理时间推移导致的逾期）"""
    terminal = get_terminal_stages()  # P19-F2：终态元数据（取代位置魔法）
    for r in rows:
        if r.get('current_stage') in terminal:
            continue  # 终态不变
        r = _auto_calc_ddl_status(r)
    return rows


def _apply_repeat_for_rows(rows: list[dict], conn=None) -> list[dict]:
    """P19-F8 复购标记查询时计算（派生视图化，取代写入时落库）。

    口径（与统计口径字典一致）：repeat_count = 同 customer_id 他单数（排除退单终态、
    排除本单）；is_repeat = 1 if repeat_count > 0 else 0；customer_id 为空 → 0/0。
    实现：批量一次 GROUP BY 查询 + Python 覆盖（与 _refresh_ddl_for_rows 同构）；
    不走 SELECT o.* + 同名列覆盖——sqlite3.Row 重复列名取第一列，会读到落库旧值。
    落库 is_repeat/repeat_count 两列暂留不删（spec 11 §7，两个版本后评估删除），
    仅作旧版本回滚兜底；本函数为全部读取路径的唯一事实来源。
    """
    if not rows:
        return rows
    own = conn is None
    if own:
        conn = get_db()
    cids = {r.get('customer_id') for r in rows if r.get('customer_id')}
    counts: dict = {}
    if cids:
        placeholders = ','.join('?' for _ in cids)
        q = (f"SELECT customer_id, COUNT(*) FROM orders "
             f"WHERE customer_id IN ({placeholders}) AND current_stage != ? "
             f"GROUP BY customer_id")
        for cid, cnt in conn.execute(q, [*cids, get_refund_stage()]).fetchall():
            counts[cid] = cnt
    if own:
        conn.close()
    for r in rows:
        cid = r.get('customer_id')
        cnt = counts.get(cid, 0) if cid else 0
        # counts 统计同客户非退单订单数（含本单）；本单非退单时减 1 排除本单
        if cid and not is_refund_stage(r.get('current_stage') or ''):
            cnt -= 1
        r['repeat_count'] = cnt
        r['is_repeat'] = 1 if cnt > 0 else 0
    return rows


# ═══════════════════════════════════════════════════════════
# 查询
# ═══════════════════════════════════════════════════════════

# P13b G3 列排序：排序键公式
#   数值列直排；文本/日期列 NULL 排最后；来源/阶段/收款按注册表顺序 CASE WHEN 映射
#   已知限制：SQLite 中文按 Unicode 码点排序，不做拼音排序
_ORDER_SORT_COLUMNS = {
    'id': ('num', 'o.id'),
    'income': ('num', 'o.income'),
    'actual_received': ('num', 'o.actual_received'),
    'customer': ('text', 'c.name'),
    'project_name': ('text', 'o.project_name'),
    'commission_type': ('text', 'o.commission_type'),
    'source': ('choice', 'source', 'o.source'),
    'stage': ('choice', 'stage', 'o.current_stage'),
    'payment': ('choice', 'payment_status', 'o.payment_status'),
    'ddl': ('date', 'o.scheduled_end'),
    'scheduled': ('date', 'o.scheduled_start'),
}

_ORDER_SORT_DEFAULT = "o.sort_order ASC, o.scheduled_start ASC"


def _order_sort_clause(sort: str, direction: str) -> tuple:
    """构造订单 ORDER BY 子句，返回 (clause, extra_params)。"""
    if not sort or sort not in _ORDER_SORT_COLUMNS or direction not in ('asc', 'desc'):
        return _ORDER_SORT_DEFAULT, []
    spec = _ORDER_SORT_COLUMNS[sort]
    dir_sql = 'DESC' if direction == 'desc' else 'ASC'
    kind = spec[0]
    if kind == 'num':
        return f"{spec[1]} {dir_sql}, {_ORDER_SORT_DEFAULT}", []
    if kind in ('text', 'date'):
        col = spec[1]
        return f"{col} IS NULL ASC, {col} {dir_sql}, {_ORDER_SORT_DEFAULT}", []
    # choice：注册表顺序 CASE WHEN 映射，未匹配/NULL 排最后
    _, choice_type, col = spec
    choices = get_choices(choice_type)
    cases = ' '.join(f"WHEN ? THEN {i}" for i in range(len(choices)))
    expr = f"CASE {col} {cases} ELSE {len(choices)} END"
    return f"{col} IS NULL ASC, {expr} {dir_sql}, {_ORDER_SORT_DEFAULT}", list(choices)


def list_orders(filters: dict | None = None) -> list[dict]:
    """查询订单列表，支持多种筛选 + 分页"""
    filters = filters or {}
    conn = get_db()
    where = []
    params = []

    if filters.get('stage'):
        where.append("o.current_stage = ?")
        params.append(filters['stage'])
    if filters.get('source'):
        where.append("o.source = ?")
        params.append(filters['source'])
    if filters.get('status'):
        where.append("o.ddl_status = ?")
        params.append(filters['status'])
    if filters.get('payment_status'):
        where.append("o.payment_status = ?")
        params.append(filters['payment_status'])
    if filters.get('search'):
        where.append("(o.project_name LIKE ? OR c.name LIKE ?)")
        params.extend([f"%{filters['search']}%", f"%{filters['search']}%"])
    # P16e：按订单结束时间（scheduled_end）范围筛选，任一端为空按单边开区间
    if filters.get('end_from'):
        where.append("o.scheduled_end >= ?")
        params.append(filters['end_from'])
    if filters.get('end_to'):
        where.append("o.scheduled_end <= ?")
        params.append(filters['end_to'])
    if filters.get('customer_id'):
        where.append("o.customer_id = ?")
        params.append(filters['customer_id'])
    if filters.get('archived') is not None:
        where.append("o.is_archived = ?")
        params.append(1 if filters['archived'] else 0)
    else:
        where.append("o.is_archived = 0")  # 默认只看活跃
    if filters.get('week') == 'current':
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        where.append("o.scheduled_start <= ? AND o.scheduled_end >= ?")
        params.extend([sunday.isoformat(), monday.isoformat()])

    where_clause = ' AND '.join(where)
    base_sql = f"""FROM orders o
              LEFT JOIN customers c ON o.customer_id = c.id
              WHERE {where_clause}"""

    # 分页参数（P19-F11：per_page<=0 表示全量——看板/主页本周等无分页 UI 的视图
    # 防默认 30 静默截断丢单；SQLite LIMIT -1 = 无限制）
    page = int(filters.get('page', 1))
    per_page = int(filters.get('per_page', 30))
    if per_page <= 0:
        per_page = -1
        offset = 0
    else:
        offset = (page - 1) * per_page

    # 查询总数
    count_row = conn.execute(f"SELECT COUNT(*) {base_sql}", params).fetchone()
    total = count_row[0] if count_row else 0

    # 查询当前页（P13b G3：sort/dir 三态排序，默认 sort_order ASC, scheduled_start ASC）
    order_clause, order_params = _order_sort_clause(filters.get('sort'), filters.get('dir'))
    # P15c：本周排单默认按 DDL（scheduled_end）升序，NULL 排最后（未显式指定 sort 时）
    if filters.get('week') == 'current' and not filters.get('sort'):
        order_clause, order_params = "o.scheduled_end IS NULL ASC, o.scheduled_end ASC, o.sort_order ASC", []
    sql = f"""SELECT o.*, c.name as customer_name {base_sql}
              ORDER BY {order_clause}
              LIMIT ? OFFSET ?"""
    rows = conn.execute(sql, params + order_params + [per_page, offset]).fetchall()
    result = _apply_repeat_for_rows([dict(r) for r in rows], conn=conn)  # P19-F8 查询时计算复购
    conn.close()

    result = _refresh_ddl_for_rows(result)
    # 将 total 挂在第一个结果的特殊标记上，由调用方提取
    if result:
        result[0]['_total'] = total
        result[0]['_page'] = page
        result[0]['_per_page'] = per_page
    # 空结果直接返回空列表（不再包装成假订单）
    return result


def get_orders_for_gantt() -> list[dict]:
    """时间线全量：所有带排期的订单（含已归档的已完成/退单、未开始），返回甘特图格式。

    仅要求 scheduled_start/scheduled_end 齐全（时间线条形需要起止锚点）；
    不再按 is_archived 过滤 —— 已完成/退单会静默归档，过滤会导致它们从时间线消失。
    状态维度改由前端「显示全部 / 仅进行中」开关控制。
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT o.*, c.name as customer_name
        FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.id
        WHERE o.scheduled_start IS NOT NULL
          AND o.scheduled_end IS NOT NULL
        ORDER BY o.scheduled_start ASC
    """).fetchall()
    conn.close()

    stage_progress = {name: m['progress'] for name, m in STAGE_META.items()}  # P19-F2：进度取自元数据
    stage_class = {
        '待开始': 'stage-pending', '色稿': 'stage-sketch',
        '线稿': 'stage-lineart', '细化': 'stage-detail',
        '收尾': 'stage-finish', '完成': 'stage-completed',
        '退单': 'stage-cancelled'
    }

    result = []
    for r in rows:
        r = dict(r)
        classes = [stage_class.get(r['current_stage'], '')]
        if r['ddl_status'] == '🔴逾期':
            classes.append('overdue')
        if r['is_archived']:
            classes.append('archived')

        result.append({
            'id': str(r['id']),
            'name': f"{r['customer_name'] or ''}-{r['project_name']}" if r['customer_name'] else r['project_name'],
            'start': r['scheduled_start'] or '',
            'end': r['scheduled_end'] or '',
            'progress': stage_progress.get(r['current_stage'], 0),
            'dependencies': '',
            'custom_class': ' '.join(c for c in classes if c),
            'current_stage': r['current_stage'],
            'is_archived': r['is_archived'],
        })

    return result


# 日历着色调色板
CALENDAR_PALETTES = {
    'stage': {
        '待开始': '#b0b0aa', '色稿': '#6b8eb8', '线稿': '#5b9e9e',
        '细化': '#c49b4a', '收尾': '#8b7ec8', '完成': '#5b9e6b',
        '退单': '#c4756b'
    },
    'source': {
        # Validated categorical — slots 1-4
        '米画师': '#2a78d6', 'B站工坊': '#1baf7a',
        '微信': '#eda100', '其他': '#008300'
    },
    'ddl': {
        # Status-adjacent — good/warning/critical + muted
        '正常': '#0ca30c', '即将到期': '#fab219',
        '🔴逾期': '#d03b3b', '已完成✅': '#898781'
    },
    'payment': {
        '已收定金': '#2a78d6', '未收款': '#fab219', '已结算': '#0ca30c',
        '欠款': '#d03b3b', '免收': '#008300'
    },
    'commission': {
        # ≥8 categories → fold into "Other" per dataviz rule
        '单人半身': '#2a78d6', '色稿大头': '#1baf7a', '双人横插': '#eda100',
        '立绘': '#008300', '场景插画': '#4a3aa7', 'Q版': '#e34948',
        '服设': '#e87ba4', '厚涂头像': '#eb6834'
    },
}

# 模式 → 取值字段
CALENDAR_COLOR_FIELDS = {
    'stage': 'current_stage',
    'source': 'source',
    'ddl': 'ddl_status',
    'payment': 'payment_status',
    'commission': 'commission_type',
}


def _calendar_filter_sql(filters: dict | None) -> tuple:
    """P13b F1 级联筛选：阶段/来源/类别/客户/收款状态 → (where 片段列表, params)"""
    where, params = [], []
    filters = filters or {}
    if filters.get('stage'):
        where.append("o.current_stage = ?")
        params.append(filters['stage'])
    if filters.get('source'):
        where.append("o.source = ?")
        params.append(filters['source'])
    if filters.get('commission_type'):
        where.append("o.commission_type = ?")
        params.append(filters['commission_type'])
    if filters.get('customer_id'):
        where.append("o.customer_id = ?")
        params.append(filters['customer_id'])
    if filters.get('payment_status'):
        where.append("o.payment_status = ?")
        params.append(filters['payment_status'])
    return where, params


def get_orders_for_calendar(color_mode: str = 'source', filters: dict | None = None,
                            show_archived: bool = False) -> list[dict]:
    """有日期的订单，返回 FullCalendar 事件格式。
    color_mode: stage | source | ddl | payment | commission
    优先级：custom_color > settings 自定义 > 默认调色板 > 灰色
    filters: P13b F1 级联筛选（阶段/来源/类别/客户/收款状态）
    show_archived: P16d 日历归档显隐开关。False（默认）仅取 is_archived=0；
                   True 纳入已归档（已完成+退单）项目。
    """
    # 合并默认调色板 + 用户自定义设置
    base_palette = dict(CALENDAR_PALETTES.get(color_mode, CALENDAR_PALETTES['stage']))
    prefix = f'cal_{color_mode}_'
    try:
        all_settings = get_all_settings()
        for k, v in all_settings.items():
            if k.startswith(prefix):
                label = k[len(prefix):]
                base_palette[label] = v
    except Exception:
        pass

    field = CALENDAR_COLOR_FIELDS.get(color_mode, 'current_stage')
    default_color = '#b0b0aa'

    conn = get_db()
    filter_where, filter_params = _calendar_filter_sql(filters)
    extra = (' AND ' + ' AND '.join(filter_where)) if filter_where else ''
    # P16d: 默认隐藏已归档；开关开启时不限制 is_archived
    archived_where = '' if show_archived else 'o.is_archived = 0 AND '
    rows = conn.execute(f"""
        SELECT o.*, c.name as customer_name
        FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.id
        WHERE {archived_where}o.scheduled_start IS NOT NULL{extra}
        ORDER BY o.scheduled_start ASC
    """, filter_params).fetchall()
    conn.close()

    result = []
    for r in rows:
        r = dict(r)
        end = r['scheduled_end'] or r['scheduled_start']
        if end and 'T' not in str(end):
            # 纯日期格式：FullCalendar 全天事件 end +1 天（exclusive）
            from datetime import datetime as dt
            try:
                end_dt = dt.strptime(end, '%Y-%m-%d')
                end = (end_dt + timedelta(days=1)).strftime('%Y-%m-%d')
            except (ValueError, TypeError):
                pass
        # datetime 格式（含 T）：FullCalendar 定时事件，end 直接使用原始值

        # 颜色优先级：custom_color > settings palette > default
        color = r.get('custom_color') or base_palette.get(r.get(field, ''), default_color)

        result.append({
            'id': str(r['id']),
            'title': f"{r['customer_name'] or ''}-{r['project_name']}" if r['customer_name'] else r['project_name'],
            'start': r['scheduled_start'],
            'end': end,
            'backgroundColor': color,
            'textColor': '#fff',
            'borderColor': color,
            'extendedProps': {
                'customer_name': r['customer_name'],
                'stage': r['current_stage'],
                'source': r['source'],
                'ddl': r['ddl_status'],
                'payment': r['payment_status'],
                'commission': r['commission_type'],
                'income': r['income'],
                'overdue': r['ddl_status'] == '🔴逾期',
                'custom_color': r.get('custom_color'),
                'color_mode': color_mode,
            }
        })
    return result


def set_order_custom_color(order_id: int, color: str | None) -> bool:
    """设置订单的自定义颜色（空字符串 = 清除）"""
    conn = get_db()
    conn.execute("UPDATE orders SET custom_color = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                 (color if color else None, order_id))
    conn.commit()
    conn.close()
    return True


def get_unscheduled_orders(filters: dict | None = None, show_archived: bool = False) -> list[dict]:
    """scheduled_start IS NULL 的订单（P13b F1：支持级联筛选）

    show_archived: P16d 与日历归档开关同步。False（默认）仅取 is_archived=0；
                   True 纳入已归档的未排期项。
    """
    conn = get_db()
    filter_where, filter_params = _calendar_filter_sql(filters)
    extra = (' AND ' + ' AND '.join(filter_where)) if filter_where else ''
    archived_where = '' if show_archived else 'o.is_archived = 0 AND '
    rows = conn.execute(f"""
        SELECT o.*, c.name as customer_name
        FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.id
        WHERE {archived_where}o.scheduled_start IS NULL{extra}
        ORDER BY o.sort_order ASC, o.created_at DESC
    """, filter_params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_overdue_orders() -> list[dict]:
    """逾期订单列表 — P19-F3 起改调口径字典 metric_overdue_orders（卡片/明细同源）"""
    return metric_overdue_orders()


def get_all_orders() -> list[dict]:
    """全部订单（含已归档）+ 客户名，按 sort_order、id 排序，供导出用。

    不复用 list_orders：后者默认只取活跃订单（archived=0），且对非终态订单
    做 _refresh_ddl_for_rows 内存改写 ddl_status；导出快照需取库内原值、含归档。
    复购标记查询时计算（P19-F8），不落库旧值。
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT o.*, c.name as customer_name
        FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.id
        ORDER BY o.sort_order ASC, o.id ASC
    """).fetchall()
    result = _apply_repeat_for_rows([dict(r) for r in rows], conn=conn)
    conn.close()
    return result


def list_gallery_page(filters: dict | None = None, offset: int = 0, limit: int = 24) -> tuple[list[dict], int]:
    """画廊分页查询（P13c）：返回 (当前批次, 总数)。

    筛选参数：stage / source / customer（客户名称）/ type（类别）。
    排序保持 updated_at DESC，与旧版全量查询一致。
    """
    # P20-F14：画廊出图完全依赖 image_url；has_image=1 但 url 为空的脏行必然破图，不再放行
    where = ["(o.image_url IS NOT NULL AND o.image_url != '')"]
    params: list = []
    if filters:
        if filters.get('stage'):
            where.append("o.current_stage = ?")
            params.append(filters['stage'])
        if filters.get('source'):
            where.append("o.source = ?")
            params.append(filters['source'])
        if filters.get('customer'):
            where.append("c.name = ?")
            params.append(filters['customer'])
        if filters.get('type'):
            where.append("o.commission_type = ?")
            params.append(filters['type'])
    where_sql = " AND ".join(where)
    conn = get_db()
    total = conn.execute(
        f"""SELECT COUNT(*) FROM orders o
            LEFT JOIN customers c ON o.customer_id = c.id
            WHERE {where_sql}""",
        params
    ).fetchone()[0]
    rows = conn.execute(
        f"""SELECT o.id, o.project_name, o.current_stage, o.source, o.commission_type,
                   o.income, o.image_url, o.image_path, c.name as customer_name
            FROM orders o
            LEFT JOIN customers c ON o.customer_id = c.id
            WHERE {where_sql}
            ORDER BY o.updated_at DESC
            LIMIT ? OFFSET ?""",
        params + [limit, offset]
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows], total


# ═══════════════════════════════════════════════════════════
# 批量操作
# ═══════════════════════════════════════════════════════════


def update_stage(order_id: int, new_stage: str) -> bool:
    """更新订单阶段（P19-F5：不再绕道 update_order；读+DDL 重算+条件归档 单事务，拖拽 ≤2 连接）。

    阶段变化不改金额/客户，无需 recalc 客户统计；归档经管线内部唯一入口。"""
    with transaction() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            return False
        data = dict(row)
        data['current_stage'] = new_stage
        data = _auto_calc_ddl_status(data)  # DDL + 「终态+已结算/免收」自动归档
        conn.execute(
            """UPDATE orders SET current_stage = ?, ddl_status = ?, is_archived = ?,
               completed_at = ?, is_overdue = ?, updated_at = datetime('now','localtime')
               WHERE id = ?""",
            (new_stage, data.get('ddl_status'), int(data.get('is_archived') or 0),
             data.get('completed_at'), int(data.get('is_overdue') or 0), order_id))
    return True


def reschedule_order(order_id: int, start: str, end: str) -> bool:
    """更新订单排期（P19-F5：日期+DDL 重算单事务；不触发归档字段，保持旧语义）。"""
    with transaction() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            return False
        data = dict(row)
        data['scheduled_start'] = start
        data['scheduled_end'] = end
        data = _auto_calc_ddl_status(data)
        conn.execute(
            """UPDATE orders SET scheduled_start = ?, scheduled_end = ?, ddl_status = ?,
               updated_at = datetime('now','localtime') WHERE id = ?""",
            (start, end, data.get('ddl_status'), order_id))
    return True


# ═══════════════════════════════════════════════════════════
# 批量操作（P19-F5：逐条多事务 → 整批单事务）
# ═══════════════════════════════════════════════════════════

def batch_update_stage(order_ids: list[int], new_stage: str) -> int:
    """批量改阶段：整批单事务，每单走 DDL+条件归档管线。返回成功数。"""
    count = 0
    with transaction() as conn:
        for oid in order_ids:
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
            if not row:
                continue
            data = dict(row)
            data['current_stage'] = new_stage
            data = _auto_calc_ddl_status(data)
            conn.execute(
                """UPDATE orders SET current_stage = ?, ddl_status = ?, is_archived = ?,
                   completed_at = ?, is_overdue = ?, updated_at = datetime('now','localtime')
                   WHERE id = ?""",
                (new_stage, data.get('ddl_status'), int(data.get('is_archived') or 0),
                 data.get('completed_at'), int(data.get('is_overdue') or 0), oid))
            count += 1
    return count


def batch_set_archived(order_ids: list[int], archived: bool) -> int:
    """批量归档/取消归档：整批单事务，全部经 set_archived 唯一入口。"""
    count = 0
    with transaction() as conn:
        for oid in order_ids:
            row = conn.execute("SELECT id FROM orders WHERE id = ?", (oid,)).fetchone()
            if not row:
                continue
            set_archived(conn, oid, archived)
            count += 1
    return count


def batch_update_field(order_ids: list[int], updates: dict) -> int:
    """批量字段更新（source/payment_status/ddl_status/is_commercial）：整批单事务。

    每单走单管线（merge→financials→ddl/archive→UPDATE），客户集去重 recalc。"""
    keys = set(updates.keys())
    needs_financials = bool(_MONEY_FIELDS & keys)
    needs_ddl = bool(_DDL_TRIGGER_FIELDS & keys) or needs_financials
    count = 0
    cids = set()
    with transaction() as conn:
        for oid in order_ids:
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
            if not row:
                continue
            merged = {**dict(row), **updates}
            if needs_financials:
                merged = _calc_financials(merged)
            if needs_ddl:
                merged = _auto_calc_ddl_status(merged)
            payload = {
                'ddl_status': merged.get('ddl_status'),
                'income': float(merged.get('income') or 0),
                'platform_fee': float(merged.get('platform_fee') or 0),
                'actual_received': float(merged.get('actual_received') or 0),
            }
            for k, v in updates.items():
                payload[k] = v
            if int(merged.get('is_archived') or 0) == 1:
                payload['is_archived'] = 1
                payload['completed_at'] = merged.get('completed_at')
                payload['is_overdue'] = int(merged.get('is_overdue') or 0)
            set_clause = ', '.join([f"{k} = ?" for k in payload.keys()])
            conn.execute(
                f"UPDATE orders SET {set_clause}, updated_at = datetime('now','localtime') WHERE id = ?",
                list(payload.values()) + [oid])
            if row['customer_id']:
                cids.add(row['customer_id'])
            count += 1
        for cid in cids:
            recalc_customer_stats(cid, conn=conn)
    return count


def batch_delete_orders(order_ids: list[int]) -> int:
    """批量删除：整批单事务，客户集去重 recalc。"""
    count = 0
    cids = set()
    with transaction() as conn:
        for oid in order_ids:
            row = conn.execute("SELECT customer_id FROM orders WHERE id = ?", (oid,)).fetchone()
            if not row:
                continue
            conn.execute("DELETE FROM orders WHERE id = ?", (oid,))
            if row['customer_id']:
                cids.add(row['customer_id'])
            count += 1
        for cid in cids:
            recalc_customer_stats(cid, conn=conn)
    return count


def batch_recompute_orders(order_ids: list[int]) -> int:
    """批量重算（P17a）：整批单事务，每单走 recompute 管线。"""
    count = 0
    cids = set()
    with transaction() as conn:
        for oid in order_ids:
            existing = get_order(oid, conn=conn)
            if not existing:
                continue
            data = dict(existing)
            data = _calc_financials(data)
            data = _auto_calc_ddl_status(data)
            derived = {
                'ddl_status': data.get('ddl_status'),
                'income': float(data.get('income') or 0),
                'platform_fee': float(data.get('platform_fee') or 0),
                'actual_received': float(data.get('actual_received') or 0),
            }
            if int(data.get('is_archived') or 0) == 1:
                derived['is_archived'] = 1
                derived['completed_at'] = data.get('completed_at')
                derived['is_overdue'] = int(data.get('is_overdue') or 0)
            set_clause = ', '.join([f"{k} = ?" for k in derived.keys()])
            conn.execute(
                f"UPDATE orders SET {set_clause}, updated_at = datetime('now','localtime') WHERE id = ?",
                list(derived.values()) + [oid])
            if existing.get('customer_id'):
                cids.add(existing['customer_id'])
            count += 1
        for cid in cids:
            recalc_customer_stats(cid, conn=conn)
    return count


# ═══════════════════════════════════════════════════════════
# 统计
# ═══════════════════════════════════════════════════════════

def _shift_months(d: date, months: int) -> date:
    """日期按月平移（天数钳制到目标月月末）"""
    import calendar
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _pct_change(cur: float, prev: float) -> int:
    """环比百分比：prev 为 0 时，cur>0 记 100，否则记 0"""
    if prev and prev > 0:
        return round((cur - prev) / prev * 100)
    return 100 if cur > 0 else 0


# 预设范围 → 环比平移月数 / 对比文案
_PRESET_SHIFT = {'month': (-1, 'vs 上月'), 'quarter': (-3, 'vs 上季度'), 'year': (-12, 'vs 去年')}
_PRESET_LABEL = {'month': '本月', 'quarter': '本季度', 'year': '本年', 'all': '全部', 'custom': '自定义'}


# ═══════════════════════════════════════════════════════════
# 统计口径字典（P19-F3）— 每个统计指标全库唯一定义，主页/看板/客户页同源调用
# ═══════════════════════════════════════════════════════════
#
# 统一口径（主页与收入看板一致）：
#   所有收入/完成统计统一按「排期月份」（scheduled_end）归属。
# 规则：
#   - 金额字段一律 actual_received（净额）；毛额 income 仅在品类分布分列并标注「毛」。
#   - 已实现收入/完成数：is_archived=1 且 终态=完成（非退单），按 scheduled_end 归属。
#   - 活跃/预计/逾期：is_archived=0 且 非终态。
#   - 客户 total_spent：该客户全部订单 SUM(actual_received)（含进行中/未收款/退单），页面明示。
#   - 复购标记（派生视图，P19-F8）：repeat_count=同 customer_id 他单数（排除退单终态、排除本单），
#     is_repeat=(repeat_count>0)；查询时计算（_apply_repeat_for_rows），落库两列暂留仅作回滚兜底。

def _metric_conn(conn):
    """内部：传入连接则复用，否则自建。返回 (conn, should_close)。"""
    return (conn, False) if conn is not None else (get_db(), True)


def _safe_iso(s):
    """P19-F6 内部容错：任意输入 → date；空/非法 → None（不抛异常，杜绝 500）。"""
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _period_end_date(preset: str, today_d: date) -> date:
    """返回预设周期的末日：month→月末, quarter→季末, year→年末；其余回落到今天。"""
    import calendar
    if preset == 'month':
        return date(today_d.year, today_d.month, calendar.monthrange(today_d.year, today_d.month)[1])
    elif preset == 'quarter':
        q_end_month = (today_d.month - 1) // 3 * 3 + 3
        return date(today_d.year, q_end_month, calendar.monthrange(today_d.year, q_end_month)[1])
    elif preset == 'year':
        return date(today_d.year, 12, 31)
    return today_d


def _in_range_sql(date_expr: str, start, end, params: list) -> str:
    """「date_expr 落在 [start, end]」SQL 片段（end 按次日开区间）；start/end 为 None 不限。"""
    conds = [f"{date_expr} IS NOT NULL"]
    if start:
        conds.append(f"{date_expr} >= ?")
        params.append(str(start)[:10])
    if end:
        end_d = _safe_iso(end)
        if end_d:  # 非法日期容错：跳过上限条件（路由层已拦，双层防护）
            end_next = (end_d + timedelta(days=1)).isoformat()
            conds.append(f"{date_expr} < ?")
            params.append(end_next)
    return ' AND '.join(conds)


def _sched_overlap_sql(start, end, params: list) -> str:
    """「排期与 [start, end] 有交集」SQL 片段；start/end 为 None → 1=1（不限）。"""
    conds = []
    if end:
        conds.append("scheduled_start <= ?")
        params.append(str(end)[:10])
    if start:
        conds.append("(scheduled_end >= ? OR scheduled_end IS NULL)")
        params.append(str(start)[:10])
    return ' AND '.join(conds) if conds else "1=1"


def metric_active_count(start=None, end=None, conn=None) -> int:
    """活跃订单数。
    过滤：is_archived=0 且 非终态（STAGE_META）且 排期与 [start,end] 有交集；无范围=全部活跃。
    金额：—（计数）。含退单：否。
    """
    c, close = _metric_conn(conn)
    params: list = []
    overlap = _sched_overlap_sql(start, end, params)
    terminal = tuple(get_terminal_stages())
    row = c.execute(
        f"SELECT COUNT(1) FROM orders WHERE is_archived = 0 "
        f"AND current_stage NOT IN ({','.join('?' * len(terminal))}) AND {overlap}",
        list(terminal) + params
    ).fetchone()
    if close:
        c.close()
    return row[0]


def metric_realized_income(start=None, end=None, conn=None) -> float:
    """已实现收入（统一口径，按排期月份归属）。
    过滤：is_archived=1 且 终态=完成（非退单）；
    时间归属：scheduled_end ∈ [start,end]（None=不限）；
    金额：SUM(actual_received)（净额）。含退单：否。
    """
    c, close = _metric_conn(conn)
    params: list = []
    in_range = _in_range_sql('scheduled_end', start, end, params)
    row = c.execute(
        f"SELECT COALESCE(SUM(actual_received), 0) FROM orders "
        f"WHERE is_archived = 1 AND current_stage = ? AND {in_range}",
        [get_done_stage()] + params
    ).fetchone()
    if close:
        c.close()
    return round(row[0], 2)


def metric_completed_count(start=None, end=None, conn=None) -> int:
    """完成单数（统一口径）。口径同 metric_realized_income，COUNT。含退单：否。"""
    c, close = _metric_conn(conn)
    params: list = []
    in_range = _in_range_sql('scheduled_end', start, end, params)
    row = c.execute(
        f"SELECT COUNT(1) FROM orders WHERE is_archived = 1 AND current_stage = ? AND {in_range}",
        [get_done_stage()] + params
    ).fetchone()
    if close:
        c.close()
    return row[0]


def metric_expected_income(start=None, end=None, conn=None) -> float:
    """预计收入。
    过滤：is_archived=0 且 非终态 且 排期与 [start,end] 有交集；
    金额：SUM(actual_received)（净额；统一前看板月度预计图用毛额 income）。含退单：否。
    """
    c, close = _metric_conn(conn)
    params: list = []
    overlap = _sched_overlap_sql(start, end, params)
    terminal = tuple(get_terminal_stages())
    row = c.execute(
        f"SELECT COALESCE(SUM(actual_received), 0) FROM orders WHERE is_archived = 0 "
        f"AND current_stage NOT IN ({','.join('?' * len(terminal))}) AND {overlap}",
        list(terminal) + params
    ).fetchone()
    if close:
        c.close()
    return round(row[0], 2)


def _overdue_where(alias: str = '') -> tuple:
    """逾期 WHERE 片段 + 参数（逾期卡片与明细同源的唯一出处）。
    口径：is_archived=0 且 非终态 且 scheduled_end<today（全时间，不随范围变化）。
    """
    p = f"{alias}." if alias else ''
    sql = (f"{p}is_archived = 0 AND {p}scheduled_end < ? "
           f"AND {p}current_stage NOT IN (?, ?)")
    return sql, [date.today().isoformat(), get_done_stage(), get_refund_stage()]


def metric_overdue_orders(conn=None) -> list[dict]:
    """逾期订单明细（全时间口径）。过滤见 _overdue_where；与 metric_overdue_count 同一 WHERE。"""
    c, close = _metric_conn(conn)
    where, params = _overdue_where('o')
    rows = c.execute(
        f"""SELECT o.*, c.name as customer_name
           FROM orders o
           LEFT JOIN customers c ON o.customer_id = c.id
           WHERE {where}
           ORDER BY o.scheduled_end ASC""",
        params
    ).fetchall()
    if close:
        c.close()
    return [dict(r) for r in rows]


def metric_overdue_count(conn=None) -> int:
    """逾期数（全时间口径）。与 metric_overdue_orders 同一 WHERE（卡片数=明细行数）。"""
    c, close = _metric_conn(conn)
    where, params = _overdue_where()
    row = c.execute(f"SELECT COUNT(1) FROM orders WHERE {where}", params).fetchone()
    if close:
        c.close()
    return row[0]


def metric_type_distribution(conn=None, year: int = None, month: int = None) -> list[dict]:
    """品类分布（P20-F17：已完成订单 is_archived=1 且 commission_type 非空，看售出结果）。
    每品类：cnt 单数 / gross=SUM(income) 应收（毛）/ net=SUM(actual_received) 实收（净）。

    year/month: 可选筛选，按排期月份（scheduled_start）过滤。
    """
    c, close = _metric_conn(conn)
    where_parts = ["is_archived = 1", "commission_type IS NOT NULL", "commission_type != ''"]
    params = []
    if year is not None:
        where_parts.append("strftime('%Y', scheduled_start) = ?")
        params.append(str(year))
    if month is not None:
        where_parts.append("strftime('%m', scheduled_start) = ?")
        params.append(str(month).zfill(2))
    where_clause = ' AND '.join(where_parts)
    rows = c.execute(f"""
        SELECT commission_type, COUNT(1) as cnt,
               COALESCE(SUM(income), 0) as gross,
               COALESCE(SUM(actual_received), 0) as net
        FROM orders
        WHERE {where_clause}
        GROUP BY commission_type
        ORDER BY cnt DESC
    """, params).fetchall()
    if close:
        c.close()
    return [dict(r) for r in rows]


def get_dashboard_stats(start_date: str = None, end_date: str = None, preset: str = 'month') -> dict:
    """仪表盘统计 — P13a 口径重定义。

    Args:
        start_date: 统计起始日期 (YYYY-MM-DD)，默认本月1号
        end_date:   统计截止日期 (YYYY-MM-DD)，默认今天
        preset:     month/quarter/year/all/custom，决定环比的上一周期

    口径（P13a 定义，P19-F3 起全部改调统计口径字典，见上方专区 docstring）：
        active_count:    metric_active_count（活跃非终态，排期交集）
        income:          metric_realized_income（归档+终态完成，scheduled_end 归属，净额）
        overdue_count:   metric_overdue_count（活跃非终态 + scheduled_end<today，与明细同源）
        completed_count: metric_completed_count（同 realized，COUNT）
        expected_income: metric_expected_income（活跃非终态，排期交集，净额）
    """
    today_d = date.today()

    if preset == 'all':
        rs = re = None
    else:
        # P19-F6：非法日期兜底默认范围（路由层已拦 400，此处双层防护）
        # 期末修正：预设范围 end 默认到期末（月末/季末/年末），非今天
        rs = _safe_iso(start_date) if start_date else today_d.replace(day=1)
        re = _safe_iso(end_date) if end_date else _period_end_date(preset, today_d)
        if start_date and rs is None:
            rs = today_d.replace(day=1)
        if end_date and re is None:
            re = _period_end_date(preset, today_d)

    conn = get_db()
    cur = {
        'active': metric_active_count(rs, re, conn),
        'income': metric_realized_income(rs, re, conn),
        'completed': metric_completed_count(rs, re, conn),
        'expected': metric_expected_income(rs, re, conn),
    }

    # 逾期：全部时间口径，不随范围变化，无环比（与逾期明细同源）
    overdue = metric_overdue_count(conn)

    # 环比：仅 month/quarter/year 预设，上一同类周期（同长度平移）
    prev = None
    if preset in _PRESET_SHIFT:
        months, _ = _PRESET_SHIFT[preset]
        prs, pre_ = _shift_months(rs, months), _shift_months(re, months)
        prev = {
            'active': metric_active_count(prs, pre_, conn),
            'income': metric_realized_income(prs, pre_, conn),
            'completed': metric_completed_count(prs, pre_, conn),
            'expected': metric_expected_income(prs, pre_, conn),
        }

    conn.close()

    has_compare = prev is not None
    return {
        'active_count': cur['active'],
        'active_change': _pct_change(cur['active'], prev['active']) if has_compare else None,
        'income': round(cur['income'], 2),
        'income_change': _pct_change(cur['income'], prev['income']) if has_compare else None,
        'overdue_count': overdue,
        'completed_count': cur['completed'],
        'completed_change': _pct_change(cur['completed'], prev['completed']) if has_compare else None,
        'expected_income': round(cur['expected'], 2),
        'expected_change': _pct_change(cur['expected'], prev['expected']) if has_compare else None,
        'has_compare': has_compare,
        'compare_label': _PRESET_SHIFT.get(preset, ('', ''))[1],
        'range_label': _PRESET_LABEL.get(preset, '本月'),
        'range_start': rs.isoformat() if rs else None,
        'range_end': re.isoformat() if re else None,
        'preset': preset,
        'is_custom_range': preset == 'custom',
    }


def get_stats_detail(metric: str, start_date: str = None, end_date: str = None, year: int = None, month: int = None) -> dict:
    """统计卡明细（小票弹窗数据源）。统一口径：所有收入/完成统计按 scheduled_end 归属。

    主页小票（与 get_dashboard_stats 同源）：
        active     — is_archived=0 + 非终态 + 排期与范围有交集（date_expr=scheduled_start）
        expected   — 同 active（金额取 actual_received 净额）
        income     — is_archived=1 + 终态=完成（非退单）+ scheduled_end 落范围（按排期月）
        completed  — 同 income（count）
        overdue    — 活跃 + 非终态 + scheduled_end<today（与 _overdue_where 同源，全时间口径）
    图表小票（与 get_monthly_income_stats / get_monthly_projected_stats 同源）：
        monthly_income    — 指定年月的已结算订单实收明细（按 scheduled_end 归属月）
        monthly_projected — 指定年月的进行中订单预计净额明细

    参数：metric + 范围（主页 active/income/overdue/completed/expected 用 start_date/end_date；图表 monthly_income/monthly_projected 用 year/month）。
    返回 {'items': [{id, date, project_name, amount}], 'total': 金额合计, 'count': 单数}。
    """
    stage_done = get_done_stage()  # P19-F2
    stage_cancelled = get_refund_stage()
    today_d = date.today()
    today = today_d.isoformat()

    conn = get_db()
    where, date_expr, amount_expr, params = '', '', 'actual_received', []
    order = 'date ASC'

    if metric in ('active', 'expected'):
        # 排期与范围有交集 且 未归档 且 阶段∉{完成,退单}
        conds = ["is_archived = 0", "current_stage NOT IN (?, ?)"]
        params = [stage_done, stage_cancelled]
        if start_date:
            conds.append("(scheduled_end >= ? OR scheduled_end IS NULL)")
            params.append(start_date)
        if end_date:
            conds.append("scheduled_start <= ?")
            params.append(end_date)
        where = ' AND '.join(conds)
        date_expr = "COALESCE(scheduled_start, substr(updated_at, 1, 10))"
    elif metric in ('income', 'completed'):
        # 统一口径：已归档 且 终态=完成（非退单）且 scheduled_end 落范围
        conds = ["is_archived = 1", "scheduled_end IS NOT NULL", "current_stage = ?"]
        params = [stage_done]
        if start_date:
            conds.append("scheduled_end >= ?")
            params.append(start_date)
        if end_date:
            end_d = _safe_iso(end_date)  # P19-F6 容错：非法日期跳过上限
            if end_d:
                re_next = (end_d + timedelta(days=1)).isoformat()
                conds.append("scheduled_end < ?")
                params.append(re_next)
        where = ' AND '.join(conds)
        date_expr = "scheduled_end"
    elif metric == 'overdue':
        # 与逾期卡片/明细同源（P19-F3 口径字典 _overdue_where）：活跃 + 非终态 + scheduled_end<today
        where, params = _overdue_where()
        date_expr = "scheduled_end"
    elif metric == 'monthly_income':
        # 图表小票：指定月份的已结算订单实收明细（按 scheduled_end 归属）
        paid_sql, paid_params = _paid_status_sql()
        month_str = month or str(today_d.month)
        m = int(month_str) if str(month_str).isdigit() else today_d.month
        y = year or today_d.year
        m_start = date(y, m, 1).isoformat()
        m_end = date(y + (1 if m == 12 else 0), (m % 12) + 1, 1).isoformat()
        where = f"scheduled_end IS NOT NULL AND scheduled_end >= ? AND scheduled_end < ? AND {paid_sql}"
        params = [m_start, m_end] + paid_params
        date_expr = "scheduled_end"
    elif metric == 'monthly_projected':
        # 图表小票：指定月份的进行中订单预计收入明细
        month_str = month or str(today_d.month)
        m = int(month_str) if str(month_str).isdigit() else today_d.month
        y = year or today_d.year
        m_start = date(y, m, 1).isoformat()
        m_end = date(y + (1 if m == 12 else 0), (m % 12) + 1, 1).isoformat()
        where = f"scheduled_end >= ? AND scheduled_end < ? AND is_archived = 0 AND current_stage NOT IN (?, ?)"
        params = [m_start, m_end, stage_done, stage_cancelled]
        date_expr = "scheduled_end"
        amount_expr = 'actual_received'  # P19-F3：预计收入统一净额（与 metric_expected_income 同字段）
    else:
        conn.close()
        return {'items': [], 'total': 0, 'count': 0}

    rows = conn.execute(
        f"""SELECT id, project_name,
                   {date_expr} AS date,
                   COALESCE({amount_expr}, 0) AS amount
            FROM orders WHERE {where}
            ORDER BY {order}, id ASC""",
        params
    ).fetchall()
    conn.close()

    items = [dict(r) for r in rows]
    return {
        'items': items,
        'total': round(sum(i['amount'] for i in items), 2),
        'count': len(items),
    }


def get_commission_type_distribution(year: int = None, month: int = None) -> list[dict]:
    """品类分布 — P19-F3 起改调口径字典 metric_type_distribution（cnt/gross 应收毛/net 实收净）
    支持按 year/month 筛选排期月份"""
    return metric_type_distribution(year=year, month=month)


def get_top_customers(limit: int = 5) -> list[dict]:
    """消费排名"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, total_spent, purchase_count FROM customers ORDER BY total_spent DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════
# 收入看板查询
# ═══════════════════════════════════════════════════════════

def get_available_years() -> list[int]:
    """P20-F19：获取年度选择器范围 = 数据年份 ∪ [最早年, max(当年, 最早年+2)]。
    确保即使数据集中在单一年份，用户也能查看邻近年度（空数据时显示 0）。
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT CAST(strftime('%Y', created_at) AS INTEGER) as y FROM orders "
        "UNION SELECT DISTINCT CAST(strftime('%Y', completed_at) AS INTEGER) FROM orders WHERE completed_at IS NOT NULL "
        "UNION SELECT DISTINCT CAST(strftime('%Y', scheduled_end) AS INTEGER) FROM orders WHERE scheduled_end IS NOT NULL "
        "ORDER BY y DESC"
    ).fetchall()
    conn.close()
    data_years = [r[0] for r in rows if r[0]]
    if not data_years:
        return [date.today().year]
    today = date.today().year
    earliest = min(data_years)
    latest = max(data_years)
    upper = max(today, earliest + 2, latest)
    full_range = list(range(earliest, upper + 1))
    full_range.sort(reverse=True)
    return full_range


def get_monthly_income_stats(year: int = None, months: int = 12) -> list[dict]:
    """月度收入统计（已结算订单实收）— 按排期时间(scheduled_end)归属月份"""
    if year is None:
        year = date.today().year
    conn = get_db()
    paid_sql, paid_params = _paid_status_sql()
    rows = conn.execute(
        f"""SELECT CAST(strftime('%m', scheduled_end) AS INTEGER) as m,
                  COALESCE(SUM(actual_received), 0) as total
           FROM orders
           WHERE scheduled_end IS NOT NULL
             AND scheduled_end >= ? AND scheduled_end < ?
             AND {paid_sql}
           GROUP BY m ORDER BY m""",
        (f"{year}-01-01", f"{year+1}-01-01") + tuple(paid_params)
    ).fetchall()
    result = _fill_monthly_result(rows, months, 'income')
    conn.close()
    return result


def get_monthly_projected_income(year: int = None, months: int = 12) -> list[dict]:
    """每月预计收入 — 单次 GROUP BY 查询（动态阶段名）"""
    if year is None:
        year = date.today().year
    stage_done = get_done_stage()  # P19-F2
    stage_cancelled = get_refund_stage()
    conn = get_db()
    rows = conn.execute(
        f"""SELECT CAST(strftime('%m', scheduled_end) AS INTEGER) as m,
                  COALESCE(SUM(actual_received), 0) as total
           FROM orders
           WHERE scheduled_end >= ? AND scheduled_end < ?
             AND is_archived = 0 AND current_stage NOT IN (?, ?)
           GROUP BY m ORDER BY m""",
        (f"{year}-01-01", f"{year+1}-01-01", stage_done, stage_cancelled)
    ).fetchall()
    result = _fill_monthly_result(rows, months, 'projected')
    conn.close()
    return result


def get_cumulative_annual_income(year: int = None) -> list[dict]:
    """当年收入累进 — 按排期时间(scheduled_end)归属月份"""
    if year is None:
        year = date.today().year
    conn = get_db()
    paid_sql, paid_params = _paid_status_sql()
    rows = conn.execute(
        f"""SELECT CAST(strftime('%m', scheduled_end) AS INTEGER) as m,
                  COALESCE(SUM(actual_received), 0) as total
           FROM orders
           WHERE scheduled_end IS NOT NULL
             AND scheduled_end >= ? AND scheduled_end < ?
             AND {paid_sql}
           GROUP BY m ORDER BY m""",
        (f"{year}-01-01", f"{year+1}-01-01") + tuple(paid_params)
    ).fetchall()
    cumulative = 0.0
    result = []
    row_map = {r[0]: r[1] for r in rows}
    for m in range(1, 13):
        monthly = round(row_map.get(m, 0), 2)
        cumulative += monthly
        result.append({'month': f"{m}月", 'cumulative': cumulative, 'monthly': monthly})
    conn.close()
    return result


def _fill_monthly_result(rows, months, value_key):
    """将 GROUP BY 结果填充为完整月份列表"""
    row_map = {r[0]: r[1] for r in rows}
    return [{'month': f"{m}月", value_key: round(row_map.get(m, 0), 2)} for m in range(1, months + 1)]




# ═══════════════════════════════════════════════════════════
# 客户 CRUD
# ═══════════════════════════════════════════════════════════

def create_customer(data: dict) -> int:
    """创建客户"""
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO customers (name, platform_url, preferences, notes, tags)
           VALUES (?, ?, ?, ?, ?)""",
        (data['name'], data.get('platform_url', ''),
         data.get('preferences', ''), data.get('notes', ''),
         data.get('tags', ''))
    )
    cid = cur.lastrowid
    conn.commit()
    conn.close()
    return cid


def get_customer(customer_id: int) -> dict | None:
    """获取单个客户"""
    conn = get_db()
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# P13b I2 客户排序列白名单
_CUSTOMER_SORT_COLUMNS = {'id', 'name', 'total_spent', 'purchase_count'}


def list_customers(search: str = None, sort: str = None, direction: str = None) -> list[dict]:
    """客户列表（P13b I1：搜索含 notes；I2：四列排序，默认 total_spent DESC）"""
    conn = get_db()
    where, params = '', []
    if search:
        where = "WHERE name LIKE ? OR notes LIKE ?"
        params = [f"%{search}%", f"%{search}%"]

    if sort in _CUSTOMER_SORT_COLUMNS and direction in ('asc', 'desc'):
        dir_sql = 'DESC' if direction == 'desc' else 'ASC'
        if sort == 'name':
            order_by = f"name IS NULL ASC, name {dir_sql}"
        else:
            order_by = f"{sort} {dir_sql}"
    else:
        order_by = "total_spent DESC"

    rows = conn.execute(f"SELECT * FROM customers {where} ORDER BY {order_by}", params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_customer(customer_id: int, data: dict) -> bool:
    """更新客户"""
    if not data:
        return False
    # 列名白名单，防止注入
    valid_cols = {'name', 'platform_url', 'preferences', 'notes', 'total_spent', 'purchase_count', 'tags'}
    data = {k: v for k, v in data.items() if k in valid_cols}
    if not data:
        return False
    data['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    set_clause = ', '.join([f"{k} = ?" for k in data.keys()])
    conn = get_db()
    conn.execute(f"UPDATE customers SET {set_clause} WHERE id = ?", list(data.values()) + [customer_id])
    conn.commit()
    conn.close()
    return True


def delete_customer(customer_id: int) -> bool:
    """删除客户"""
    conn = get_db()
    conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
    conn.commit()
    conn.close()
    return True


def get_customer_orders(customer_id: int) -> list[dict]:
    """客户的所有订单"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM orders WHERE customer_id = ? ORDER BY created_at DESC",
        (customer_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_customer_images(customer_id: int) -> list[dict]:
    """客户名下所有订单的作品图片（用于客户详情作品画廊，P15f）。

    返回每条 order_images 记录并附带所属订单的 project_name，
    按订单创建时间倒序、组内 sort_order/id 升序排列。"""
    conn = get_db()
    rows = conn.execute(
        """SELECT oi.*, o.project_name AS project_name, o.id AS order_id
           FROM order_images oi
           JOIN orders o ON oi.order_id = o.id
           WHERE o.customer_id = ?
           ORDER BY o.created_at DESC, oi.sort_order, oi.id""",
        (customer_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def recalc_customer_stats(customer_id: int, conn=None) -> None:
    """重新计算客户的 total_spent 和 purchase_count。

    口径：仅统计已归档且已完成且已结算的订单 SUM(actual_received) / COUNT——
    排除进行中、未收款、退单，仅计入已实收金额。
    conn：事务连接复用，传入则不 commit/close。
    """
    if not customer_id:
        return
    own = conn is None
    if own:
        conn = get_db()
    row = conn.execute(
        """SELECT COUNT(*) as cnt, COALESCE(SUM(actual_received), 0) as total
           FROM orders
           WHERE customer_id = ?
             AND is_archived = 1
             AND current_stage = '完成'
             AND payment_status = '已结算'""",
        (customer_id,)
    ).fetchone()
    conn.execute(
        "UPDATE customers SET total_spent = ?, purchase_count = ?, updated_at = datetime('now','localtime') WHERE id = ?",
        (row['total'], row['cnt'], customer_id)
    )
    if own:
        conn.commit()
        conn.close()


# ═══════════════════════════════════════════════════════════
# 自动初始化
# ═══════════════════════════════════════════════════════════

if __name__ == '__main__':
    init_db()
    print("数据库初始化完成！")
