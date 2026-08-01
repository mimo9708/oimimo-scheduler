# oimimo scheduler — 架构文档

> **版本**: V2 开发版 · **基准日期**: 2026-07-28
> 本文档以当前代码事实为准。功能说明见 [PROJECT.md](PROJECT.md)，视觉设计见 [DESIGN.md](DESIGN.md)。

---

## 一、系统概览

面向独立插画师的**本地单用户**接单排单工具。Tkinter 启动器拉起 Flask 守护线程，用户在浏览器中操作；SQLite 单文件持久化，零网络依赖（前端库已全部本地 vendor 化）。

**运行形态**：

```
launcher.py (Tkinter 窗口, 可选 pystray 托盘)
   │  threading.Thread(daemon=True)
   ▼
app.py (Flask 单实例, 模块级 app = Flask(__name__))
   │  Jinja2 服务端渲染 + HTMX 局部刷新 + JSON API
   ▼
db.py (SQLite WAL) ── models.py (Pydantic v2 校验)
   │
   ▼
orders.db · uploads/ · exports/ · logs/ · backups/
```

**代码规模实测**（2026-07-28）：

| 文件 | 行数 | 内容 |
|---|---|---|
| app.py | 2295 | 74 路由装饰器 · 上下文处理器 · 模板过滤器 · 错误处理 |
| db.py | 3473 | ~136 函数 · 建表/迁移 · CRUD · 统计 · 注册表 · 备份管理 |
| models.py | 113 | 4 个 Pydantic 模型 |
| launcher.py | 410 | Tkinter 窗口 · pystray 托盘 · 重启/日志/端口管理 |
| image_processor.py | 286 | 原图/预览(800px)/缩略图(200×200) 三级产物 |
| static/app.js | 1704 | ~70 函数（模态框/抽屉/看板/Toast/ColorPicker/布局/图表主题） |
| static/app.css | 2852 | 80+ Design Tokens · 组件/视图/响应式样式 |
| templates/ | 36 HTML | 6 页面 + 15 partials + base + 订单/客户/导出模板 |

---

## 二、进程与部署架构

### 2.1 启动链路（launcher.py）

```
main()
 ├─ check_python()        # ≥3.9
 ├─ install_deps()        # pip 安装缺失依赖
 ├─ db.init_db()          # 幂等建表+迁移
 ├─ 端口确定（默认 1096，命令行可覆盖）
 ├─ threading.Thread(target=_start_server, daemon=True)
 ├─ show_postcard(url)    # 340×370 Tkinter 明信片窗口
 └─ 自动打开浏览器
```

- **托盘**：安装 pystray+Pillow 时，关闭窗口 → `hide_to_tray()` 最小化到系统托盘；未安装 → 普通窗口模式
- **窗口按钮**：重启服务、查看日志、清除缓存、停止服务
- **日志重定向**：打包模式 → `logs/app_YYYYMMDD_HHMMSS.log`

### 2.2 路径分离（PyInstaller 兼容）

| 用途 | 函数 | 开发态 | 打包态 |
|---|---|---|---|
| 只读资源 | `app._resource_dir()` / `launcher.resource_path()` | 脚本目录 | `sys._MEIPASS` |
| 可写数据 | `db.data_dir()` | 脚本目录 | exe 同级目录 |

`DB_PATH = data_dir()/orders.db`；备份目录 `get_backup_dir()` 从 settings 表读取（默认 `data_dir()/backups/`）。

---

## 三、应用分层

| 层 | 文件 | 职责 |
|---|---|---|
| 路由层 | app.py | 请求解析、Pydantic 校验、响应、CORS、来源校验 |
| 校验层 | models.py | OrderCreate/OrderUpdate/CustomerCreate/CustomerUpdate |
| 数据层 | db.py | 连接/事务、建表迁移、CRUD、自动计算、统计、注册表、备份 |
| 图片层 | image_processor.py | 上传 → 原图+预览+缩略图，Pillow 缺失时降级 |
| 模板层 | templates/ | base.html（4 block）+ 页面 + partials |
| 静态层 | static/ | app.css（设计系统）、app.js（交互）、vendor（本地库） |

---

## 四、路由体系

### 4.1 页面路由（GET）

| 路由 | 模板 | 说明 |
|---|---|---|
| `/` | index.html | 统计卡+甘特图+本周排单+逾期 |
| `/income` | income.html | 收入看板（gridstack 自定义布局） |
| `/calendar` | calendar.html | 日历（5 种着色模式） |
| `/gallery` | gallery.html | 瀑布流画廊 |
| `/orders` | orders/list.html | 列表（多维筛选+分页） |
| `/orders/kanban` | orders/kanban.html | 看板（SortableJS 拖拽） |
| `/orders/<id>` | orders/detail.html | 订单详情 |
| `/orders/new` | orders/form_modal.html | 新建（`?template=` 预填） |
| `/orders/<id>/edit` | 三模式分流 | `?inline=1` 抽屉 / `?modal=1` 模态 / 默认完整页 |
| `/customers` | customers/list.html | 客户列表 |
| `/customers/<id>` | customers/detail.html | 客户详情+历史+作品 |
| `/customers/new` · `/customers/<id>/edit` | customers/form.html | 客户表单 |
| `/settings` | settings.html | 设置（5 区域） |

### 4.2 数据 API（GET + POST）

| 路由 | 返回 | 说明 |
|---|---|---|
| `/api/gallery` | JSON | 画廊分页（offset/limit） |
| `/api/stats` | HTML | 统计卡 HTMX 刷新 |
| `/api/stats/detail` | JSON | 小票明细 |
| `/api/orders` | HTML | 订单行 HTMX 刷新 |
| `/api/orders/gantt-data` | JSON | 甘特图数据 |
| `/api/orders/calendar-events` | JSON | 日历事件 |
| `/api/orders/unscheduled` | HTML | 未排期池 HTMX |
| `/api/customers/quick` | HTML | 快速建客户 |
| `/api/income/type-distribution` | JSON | 品类分布（月份筛选） |
| `/api/income/hourly-type-distribution` | JSON | 时薪品类分布 |
| `/api/quote-suggestion` | HTML | 报价建议条 |
| `/api/v1/health` | JSON | 健康检查 `{status, version}` |

### 4.3 订单操作（POST）

`/orders` 创建 · `/orders/<id>/edit` 更新 · `/orders/<id>/delete` · `/orders/<id>/archive`（归档确认链）· `/orders/<id>/stage` · `/orders/<id>/reschedule` · `/orders/<id>/color` · `/orders/<id>/unschedule` · `/orders/batch`（阶段/来源/收款/DDL/商用/归档/删除/重算）· `/orders/<id>/upload-image` · `/orders/<id>/remove-image` · `/orders/<id>/work-hours` · `/orders/templates/<id>/delete`

### 4.4 设置与系统（POST）

`/settings` · `/settings/reset` · `/settings/theme/import` · `/settings/theme/apply` · `/settings/theme/delete` · `/settings/color-mode` · `/settings/palette/save` · `/settings/palette/apply` · `/settings/palette/update` · `/settings/palette/delete` · `/settings/income-layout` · `/settings/stage-flows` · `/settings/backup-dir` · `/settings/backup/delete/<filename>` · `/settings/commission-merge` · `/export/orders` · `/api/import/mihuashi` · `/api/shutdown` · `/api/log-error`

---

## 五、数据库设计

### 5.1 表结构（实测 orders.db）

**orders（36 列 + 7 索引）**

| 分组 | 列 |
|---|---|
| 标识/关联 | id PK · customer_id FK→customers ON DELETE SET NULL |
| 内容 | project_name · source · is_commercial · commission_type · platform_url · notes · custom_color |
| 流程 | current_stage · ddl_status · payment_status · stage_flow（本单阶段快照 JSON） |
| 财务 | deposit · balance · income · platform_fee · platform_fee_pct（快照）· actual_received |
| 归档 | is_archived · completed_at · is_overdue |
| 复购 | is_repeat · repeat_count（查询时计算，列保留作回滚兜底） |
| 排期 | page_deadline · scheduled_start · scheduled_end · sort_order |
| 时薪 | estimated_hours · work_hours · exclude_hourly（不进 `_MONEY_FIELDS`） |
| 图片 | image_url · image_path · has_image |
| 审计 | created_at · updated_at |

**customers（10 列）**：id · name UNIQUE · platform_url · preferences · notes · tags · total_spent · purchase_count · created_at · updated_at

**settings（86 键）**：key PK, value — 详见 §9

**order_images（6 列）**：id · order_id · image_url · image_path · sort_order · created_at

**order_templates（4 列）**：id · name · data_json · created_at

### 5.2 事务模型

`db.transaction()` 上下文管理器：成功 commit、异常 rollback、始终 close。业务函数接受 `conn=` 参数复用外部事务。

---

## 六、核心机制

### 6.1 中央注册表 CHOICE_REGISTRY

5 类选择列表唯一真实来源（stage/ddl/payment/source/commission_type）。

**get_choices() 合并语义**：settings 自定义列表为 base → orders 表去重 auto-discover 新值追加 → 保序去重。

**进程内缓存**：`_CHOICES_CACHE` 按类别缓存；失效统一走 `_invalidate_choices_cache()`。

### 6.2 终态元数据 + 阶段流程

`STAGE_META` / `DDL_STATUS_META` 显式注册 terminal/kind/progress。`STAGE_FLOWS` 流程预设（首尾锚点固定），快照优先链：本单快照 → STAGE_META → 退单=100 / 未知=0。

### 6.3 自动计算链

- **财务** `_calc_financials`：平台来源 → `platform_fee = income × pct%`，直接来源 → 0
- **DDL** `_auto_calc_ddl_status`：终态优先 → 满足归档集合时联动 → 按 scheduled_end 与今天比较
- **归档唯一入口**：`set_archived()` / `_apply_archive_to_data()` 写 `completed_at` + `is_overdue` 快照
- **复购查询时计算**：`_apply_repeat_for_rows()`（不落库）
- **days_remaining 读取时注入**：`_apply_days_remaining_for_rows()`（新增返回订单 dict 的函数时必须调用）

### 6.4 update_order 单管线

读旧单 → merge → 费率快照规则 → `_calc_financials` → DDL 重算+条件归档 → UPDATE → recalc_customer_stats。

### 6.5 统计口径字典

金额一律 `actual_received`（净额）；收入按 `scheduled_end` 归月。

| 指标 | 口径 |
|---|---|
| 月度已收 | 已结算 + scheduled_end 归月 → SUM(actual_received) |
| 月度预计 | 未归档 + 非终态 + scheduled_end 归月 |
| 主页逾期 | is_archived=0 + scheduled_end<today + 非终态 |
| 客户 total_spent | 全部订单 SUM(actual_received)（名义合作规模） |

### 6.6 备份管理

- `get_backup_dir()`：settings 表读取自定义路径，留空回退 `DEFAULT_BACKUP_DIR`（exe 同级 `backups/`）
- `set_backup_dir(path)`：校验存在+可写，保存到 settings
- 5 个备份函数统一使用 `get_backup_dir()`：`_do_backup` / `get_backup_list` / `delete_backup` / `restore_backup` / `check_data_recovery_needed`
- `delete_backup`：basename 校验防路径穿越 + 仅允许 `orders_backup_*.db` 格式
- 启动时不自动备份（仅保留手动入口）

### 6.7 订单删除自动清理

`delete_order()` 删除订单后检查 `commission_type` / `current_stage` / `source` 三个字段，无订单使用则自动移除对应 `cal_*` 颜色配置。

---

## 七、前端架构

### 7.1 页面骨架

侧边栏（6 导航+新建 CTA，240px/56px 折叠态，localStorage 记忆）+ 主内容区；全局容器：`#center-modal`（680px 居中模态）· `#edit-drawer`（480px 右抽屉）· `#toast-container`（右下角）。

### 7.2 三种表单模式

| 模式 | 触发 | 提交返回 |
|---|---|---|
| 居中模态 | `?modal=1` | modal_success.html |
| 右抽屉 | `?inline=1` | JSON + HX-Trigger |
| 完整页 | 默认 | redirect |

### 7.3 app.js 模块（~70 函数）

模态框/抽屉 · 看板（SortableJS） · 回执小票 · Toast · 导出 · 侧边栏 · Lightbox · 图片上传 · 模块定制器 · 错误日志 · ColorPicker（原生零依赖 ~230 行）· 图表主题 helper · gridstack 布局管理。

### 7.4 vendor 本地按需加载

HTMX+Lucide+JsBarcode（base.html 全局）；Frappe Gantt / Chart.js / FullCalendar / SortableJS / gridstack 各页按需引入。

---

## 八、安全模型

本地单用户定位，以来源校验替代鉴权：

- **CORS 白名单**：`127.0.0.1/localhost` · `mihuashi.com` · `chrome-extension://` · `moz-extension://`；仅 `/api/*` 生效
- **`_is_local_origin()`**：取第一个非空 Origin/Referer 判定；缺失按同源导航放行
- **`/api/shutdown`**：仅 POST + 来源校验
- **XSS**：`render_notes` 先 `html.escape(quote=True)` 再解析图片标记
- **SQL**：全量 `?` 参数绑定
- **输入校验**：Pydantic ge=0、日期 ISO 归一、跨字段校验

---

## 九、设置系统（settings 表 86 键）

| 分组 | 键模式 | 说明 |
|---|---|---|
| 全局主题 | `theme_*`（11） | bg/surface/sidebar/text/text_secondary/border/accent/link/success/warning/danger |
| 阶段色（旧） | `stage_*`（7） | 仅作读取回退，新值落 `cal_stage_*` |
| 日历调色板 | `cal_{mode}_{label}`（~31） | 5 模式 × 各值独立色 |
| 着色模式 | `calendar_color_mode`（1） | 持久化着色模式选择 |
| 选择列表 | `*_list`（4） | 用户自定义列表 |
| 阶段流程 | `stage_flows`（1） | 流程预设 JSON 数组 |
| 平台/费率 | `platform_sources` · `*_fee_*`（~10） | 平台来源集合与费率 |
| 外观 | `font_size` · `font_family`（2） | 字号/字体 |
| 主题扩展 | `custom_themes` · `palette_presets` 等（4） | 自定义主题+配色预设 |
| 收入布局 | `income_layout`（1） | gridstack 布局 JSON |
| 备份 | `backup_dir`（1） | 自定义备份目录 |
| 时薪 | `hourly_rate_enabled`（1） | 时薪功能全局开关 |
| 其他 | `api_token` · `shortcuts_json` 等 | API 预留/快捷键/杂项 |

---

## 十、启动方式

| 方式 | 命令 | 端口 |
|---|---|---|
| 启动器（推荐） | `python launcher.py` | 1096 |
| 直接运行 | `python app.py --port 1096` | 1096 |
| 批处理 | `run.bat` | 1096 |
| 打包 | PyInstaller exe（`build.spec`） | 1096 |

---

## 十一、设计决策记录

1. **SQLite + WAL**：本地零配置、单文件便携
2. **HTMX + Jinja2 而非 SPA**：单体工具无前端框架复杂度
3. **中央注册表 + 合并语义**：选择列表去硬编码，标准值永不丢失
4. **终态元数据化**：免疫 auto-discover 追加值对位置约定的破坏
5. **归档唯一入口 + 快照列**：completed_at/is_overdue 归档时写入不再回算
6. **费率快照落库**：历史订单金额与设置页费率脱钩
7. **复购 + days_remaining 查询时计算**：消除写入式派生列的腐烂面
8. **原生 ColorPicker**：零依赖完全可控
9. **来源校验替代鉴权**：本地单用户最小成本阻断跨站读写
10. **vendor 本地化**：离线可用，避免每页 ~1MB JS
11. **备份路径可配置**：用户可自定义备份位置，默认 exe 同级

---

## 十二、已知约束（反目标）

不 SaaS 化/云同步 · 不引入 SPA 框架 · 不做 AI 自动排期 · 不做多语言 · 不做微服务/消息队列 · 保持 PyInstaller 可打包运行 · 前端库本地 vendor 化。
