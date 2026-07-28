# oimimo scheduler — 架构文档

> **版本**: V2 开发版 · **基准日期**: 2026-07-13
> 本文档以当前代码事实为准（非规划态）。功能说明见 [PROJECT.md](PROJECT.md)，视觉设计系统见 [DESIGN.md](DESIGN.md)。

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
orders.db · uploads/ · exports/ · logs/
```

**技术栈与依赖**（requirements.txt 实际内容）：

| 依赖 | 版本 | 用途 |
|---|---|---|
| Flask | ≥3.0 | Web 框架 |
| Pydantic | ≥2.0 | 输入校验（models.py） |
| flask-cors | 未锁定 | `/api/*` CORS 白名单 |
| Pillow | ≥10.0 | 图片缩略图/预览生成 |
| pystray | ≥0.19 | 启动器系统托盘（可选，缺失时降级普通窗口） |
| 前端库（本地 vendor） | HTMX 2 / Lucide / Frappe Gantt 0.6 / Chart.js 4 / FullCalendar 6 / SortableJS 1 / JsBarcode | static/vendor/ 按需加载，无 CDN |

**代码规模实测**（2026-07-13）：

| 文件 | 行数 | 内容 |
|---|---|---|
| app.py | 1708 | 49 路由（24 GET + 25 POST）· 71 函数 · 2 模板过滤器 · 1 上下文处理器 · 2 错误处理器 |
| db.py | 2508 | ~100 函数 · 建表/迁移 · CRUD · 统计口径字典 · 注册表 |
| models.py | 91 | 4 个 Pydantic 模型 + 平台/直接来源常量 |
| launcher.py | 313 | 11 函数 · Tkinter 明信片窗口 · pystray 托盘 · 重启/日志/清缓存 |
| image_processor.py | ~260 | 4 函数 · 原图/预览(800px)/缩略图(200×200) 三级产物 |
| static/app.js | 1073 | ~60 函数（模态框/抽屉/看板/回执小票/上传/模块定制器/错误日志/ColorPicker） |
| static/app.css | 2066 | 60+ Design Tokens · 组件/视图/响应式样式 |
| templates/ | 27 HTML + 1 导出模板 | 14 页面 + 11 partials + base + export/orders.md |

---

## 二、进程与部署架构

### 2.1 启动链路（launcher.py）

```
main()
 ├─ check_python()        # ≥3.9
 ├─ install_deps()        # pip 安装缺失依赖
 ├─ db.init_db()          # 幂等建表+迁移
 ├─ 端口确定（默认 5001，命令行可覆盖；直接 python app.py 默认 5000，--port 覆盖）
 ├─ threading.Thread(target=_start_server, daemon=True)
 ├─ show_postcard(url)    # 340×370 Tkinter 明信片窗口（logo/状态/URL/按钮）
 └─ 自动打开浏览器
```

- **托盘**（P18-F2）：安装 pystray+Pillow 时，关闭窗口 → `hide_to_tray()` 最小化到系统托盘而非退出；未安装 → 普通窗口模式，关闭即退出。`_set_console_visible()` 控制伴随控制台显隐。
- **窗口按钮**：重启服务（Popen 新进程+退出）、查看日志（logs/）、清除缓存（`__pycache__/`）、停止服务。
- **日志重定向**：打包模式（stdout 为 None）→ `logs/app_YYYYMMDD_HHMMSS.log`。

### 2.2 路径分离（PyInstaller 兼容）

| 用途 | 函数 | 开发态 | 打包态 |
|---|---|---|---|
| 只读资源（templates/static/logo） | `app._resource_dir()` / `launcher.resource_path()` | 脚本目录 | `sys._MEIPASS` |
| 可写数据（orders.db/uploads/exports/logs） | `db.data_dir()` | 脚本目录 | exe 同级目录 |

`DB_PATH = data_dir()/orders.db`（模块级常量）；上传图存 `data_dir()/uploads/orders/<order_id>/`，经 `/uploads/<path:filename>` 路由回读。

---

## 三、应用分层

| 层 | 文件 | 职责 |
|---|---|---|
| 路由层 | app.py | 请求解析、Pydantic 校验调用、响应（HTML/JSON/重定向）、CORS、来源校验 |
| 校验层 | models.py | OrderCreate/OrderUpdate/CustomerCreate/CustomerUpdate（格式+跨字段校验） |
| 数据层 | db.py | 连接/事务、建表迁移、CRUD、自动计算、统计、注册表、设置 |
| 图片层 | image_processor.py | 上传文件 → 原图+预览+缩略图，Pillow 缺失时 `save_without_pillow` 降级 |
| 模板层 | templates/ | base.html（4 block：title/head_extra/content/scripts）+ 页面 + partials |
| 静态层 | static/ | app.css（设计系统）、app.js（交互）、logo |

> 当前为**扁平化单体**（路由+业务编排同在 app.py），分层靠约定：SQL 只在 db.py，校验只在 models.py，图片 IO 只在 image_processor.py。`.qoder/rules/project-rules.md` R15 保留三层架构为远期目标。

---

## 四、路由体系全表（49 条）

### 4.1 页面路由（14 GET）

| 路由 | 视图函数 | 模板 | 说明 |
|---|---|---|---|
| `/` | dashboard | index.html | 统计卡+甘特图+本周排单+逾期；`from/to/preset` 参数 |
| `/income` | income_dashboard | income.html | 收入看板（`year` 参数） |
| `/calendar` | calendar_view | calendar.html | 日历（`color` 着色模式参数） |
| `/orders` | orders_list | orders/list.html | 列表（stage/source/status/search/archived/page/per_page/sort/dir） |
| `/orders/kanban` | kanban_view | orders/kanban.html | 看板（per_page=0 全量） |
| `/orders/<id>` | order_detail | orders/detail.html | 订单详情 |
| `/orders/new` | order_new | orders/form_modal.html | 新建（`template` 参数预填模板） |
| `/orders/<id>/edit` | order_edit | 三模式分流 | `?inline=1` 抽屉 / `?modal=1` 模态 / 默认完整页 |
| `/customers` | customers_list | customers/list.html | 客户列表（search/sort/dir） |
| `/customers/<id>` | customer_detail | customers/detail.html | 客户详情+历史订单+作品图 |
| `/customers/new` · `/customers/<id>/edit` | customer_new/edit | customers/form.html | 客户表单 |
| `/gallery` | gallery_view | gallery.html | 画廊视图 |
| `/settings` | settings_page | settings.html | 设置（主题/订单属性/外观/系统 4 Tab） |

### 4.2 数据 API（7 GET + 1 POST）

| 路由 | 返回 | 说明 |
|---|---|---|
| `/api/gallery` | JSON | 画廊分页（offset/limit≤100，默认 24；stage/source/customer/type 筛选） |
| `/api/stats` | HTML | 统计卡 HTMX 刷新（from/to 非法日期 → 400） |
| `/api/stats/detail` | JSON | 小票明细（metric 维度） |
| `/api/orders` | HTML | 订单行 HTMX 刷新（筛选+分页） |
| `/api/orders/gantt-data` | JSON | 甘特图数据 |
| `/api/orders/calendar-events` | JSON | 日历事件（color_mode + 筛选） |
| `/api/orders/unscheduled` | HTML | 未排期池 HTMX 刷新 |
| `/api/customers/quick` (POST) | HTML | 快速建客户并返回刷新后的下拉 |

### 4.3 订单操作（11 POST）

`/orders` 创建 · `/orders/<id>/edit` 更新（三模式三分返回：inline→JSON HX-Trigger / modal→成功 HTML / page→redirect）· `/orders/<id>/delete` · `/orders/<id>/archive`（归档确认链入口）· `/orders/<id>/stage` · `/orders/<id>/reschedule` · `/orders/<id>/color` · `/orders/batch`（阶段/来源/收款/DDL/商用/归档/删除/重算）· `/orders/<id>/upload-image` · `/orders/<id>/remove-image` · `/orders/templates/<id>/delete`

### 4.4 客户操作（4 POST）

`/customers` · `/customers/<id>/edit` · `/customers/<id>/delete`（关联订单 SET NULL）· `/customers/batch`（关联单客户跳过）

### 4.5 设置与系统（11）

| 路由 | 说明 |
|---|---|
| POST `/settings` | 保存全部设置（同事务跑 choices 重命名同步） |
| POST `/settings/reset` | 恢复默认（DELETE + `_ensure_default_settings`） |
| POST `/settings/theme/import` · `/apply` · `/delete` | 自定义主题 CSS 导入/应用/删除 |
| POST `/export/orders` | Markdown 全量导出 → `exports/全部订单.md` |
| GET `/export/open-folder` | 系统调用打开导出目录 |
| POST `/api/import/mihuashi` | 浏览器插件导入（`_is_local_origin` 校验，否则 403） |
| POST `/api/shutdown` | 仅 POST + 本机来源校验（GET→405，跨站→403） |
| GET `/api/v1/health` | #41 健康检查：返回 `{status, version}`（插件/外部工具探活） |
| POST `/api/log-error` | 前端错误上报 |
| GET `/uploads/<path:filename>` · `/favicon.ico` | 静态资源 |

错误处理：`@app.errorhandler(500)` 与 `@app.errorhandler(Exception)`。

---

## 五、数据库设计

### 5.1 连接与迁移

- **WAL**：库级持久 pragma，仅 `init_db()` 启动时设置一次（P19-F10）；`get_db()` 不再每连接执行。
- **外键**：`PRAGMA foreign_keys=ON` 为连接级非持久，每次 `get_db()` 设置。
- **迁移**：`init_db()` 幂等建表 + `ALTER TABLE ADD COLUMN` 补列（先查列存在性）；含 `completed_at`/`is_overdue`/`platform_fee_pct` 等历史回填（按来源费率、COALESCE 归属月），全部幂等。

### 5.2 表结构（实测 orders.db）

**orders（32 列 + 7 索引，50 行）**

| 分组 | 列 |
|---|---|
| 标识/关联 | id PK · customer_id FK→customers ON DELETE SET NULL |
| 内容 | project_name · source（默认'米画师'）· is_commercial · commission_type · platform_url · notes |
| 流程 | current_stage（默认'待开始'）· ddl_status（默认'正常'）· payment_status（默认'未收款'） |
| 财务 | deposit · balance · income · platform_fee · platform_fee_pct（快照，可 NULL）· actual_received |
| 归档 | is_archived · completed_at · is_overdue |
| 复购 | is_repeat · repeat_count（P19-F8 起查询时计算，列保留作回滚兜底） |
| 排期 | page_deadline · scheduled_start · scheduled_end · sort_order |
| 图片 | image_url · image_path · has_image（封面冗余） |
| 审计 | created_at · updated_at |

索引：customer_id · current_stage · (scheduled_start,scheduled_end) · sort_order · is_archived · completed_at · payment_status。

**customers（10 列，18 行）**：id PK · name UNIQUE · platform_url · preferences · notes · tags · total_spent · purchase_count · created_at · updated_at。

**settings（key PK, value）**：69 键，详见 §8。

**order_images（6 列，25 行）**：id PK · order_id · image_url · image_path · sort_order · created_at — 订单多图。

**order_templates（4 列，1 行）**：id PK · name · data_json · created_at — 订单模板（`ORDER_TEMPLATE_FIELDS` 快照字段集）。

### 5.3 事务模型

`db.transaction()` 上下文管理器：单连接、成功 commit、异常 rollback、始终 close。业务函数接受 `conn=` 参数复用外部事务（传入则不自建/不提交/不关闭）。

事务化写入路径：`create_order` · `create_order_with_template` · `update_order` · `update_stage`（≤2 连接）· `delete_order` · 三个 batch_*（批量单事务）· 设置重命名（sync_choice_renames + update_settings 同事务）· `recompute_order`。

---

## 六、核心机制

### 6.1 中央注册表 CHOICE_REGISTRY（db.py:267）

5 类选择列表唯一真实来源：

| 类型 | editable | 默认值 | 映射字段 |
|---|---|---|---|
| stage | 否 | 待开始,色稿,线稿,细化,收尾,完成,退单 | current_stage |
| ddl | 否 | 正常,即将到期,🔴逾期,已完成✅,已退单 | ddl_status |
| payment | 否 | 已收定金,未收款,已结算,欠款,免收 | payment_status |
| source | 是 | 米画师,B站工坊,画加,微信,QQ,其他 | source |
| commission_type | 是 | 单人半身,色稿大头,双人横插,立绘,场景插画,Q版,服设,厚涂头像 | commission_type |

**get_choices() 合并语义**（P19-F4）：settings 自定义列表为 base（缺省用注册表 defaults）→ orders 表去重 auto-discover 新值追加（已被用户删除的默认值不回魂）→ 保序去重。任何情况下返回 ⊇ defaults。

**进程内缓存**（P19-F10）：`_CHOICES_CACHE` 按类别缓存（命中返回副本防污染）；失效统一走 `_invalidate_choices_cache()`，挂载点：update_settings / sync_choice_renames（有实际重命名）/ init_db / 三个订单写入函数（auto-discover 可能带新值）。delete_order 不挂（值残留无害）。

### 6.2 终态元数据（P19-F2）

`STAGE_META` / `DDL_STATUS_META` 显式注册 terminal/kind/progress，替代旧位置魔法（stages[-2:] 会被 auto-discover 追加值颠覆）。auto-discover 的非标准值一律视为非终态。

helper：`is_terminal_stage` / `is_refund_stage` / `get_done_stage` / `get_refund_stage` / `get_terminal_stages` / `get_stage_progress` / `get_ddl_status(kind)`。

### 6.3 自动计算链

**财务** `_calc_financials(data)`：`income = deposit+balance`；平台来源（∈ `get_platform_sources()`）→ `platform_fee = income × platform_fee_pct%`（pct 取订单快照列），直接来源 → 0；`actual_received = income − platform_fee`。

**DDL** `_auto_calc_ddl_status(data)`：终态优先（完成→已完成✅ / 退单→已退单）→ 满足归档集合时联动归档 → 否则按 scheduled_end 与今天比较（过期→🔴逾期 / ≤3 天→即将到期 / 其他→正常）。

**归档唯一入口**（P19-F1）：`set_archived()`（DB 直写）/ `_apply_archive_to_data()`（管线字典）双形态，禁止裸 UPDATE is_archived。归档写 `completed_at`（用户确认归属月优先，默认今日）+ `is_overdue`（completed_at 晚于 scheduled_end）；两字段为事件快照，重算不覆盖。取消归档清空。判定集合 `ARCHIVE_PAID_STATUSES={'已结算','免收'}` 与收入集合 `PAID_STATUSES={'已结算'}` 拆分（免收归档但不计收入）。

**复购查询时计算**（P19-F8）：读取路径统一经 `_apply_repeat_for_rows()`——一次 GROUP BY + Python 覆盖：`repeat_count = 同 customer 他单数（排除退单终态、排除本单）`，`is_repeat = count>0`。写入不再落库。

**DDL 读取时刷新**：`_refresh_ddl_for_rows()` 对非终态行按今天重算（经 STAGE_META 零 SQL），时间漂移在读取侧自愈。

### 6.4 update_order 单管线（P19-F5）

读旧单 → merge（部分更新语义）→ **费率快照规则**（P19-F9：显式传 pct→用之；仅切来源未传→按新来源 `default_fee_<source>` 刷新；皆无→保留旧快照）→ `_calc_financials` → DDL 重算+条件归档 → 单次 UPDATE → 新旧 customer_id 去重后 `recalc_customer_stats`（同事务）。表单不再直传 is_archived（归档只能走确认链）。

财务重算（recompute_order / batch_recompute_orders）按订单自身快照 pct 计算；设置页改费率不回写历史订单。

### 6.5 设置重命名同步（P19-F9 加固）

`sync_choice_renames()`：设置页改 source/commission_type 列表时，orders 表中的旧值去 emoji 模糊匹配新值——**候选恰好 1 个才合并**（多候选跳过 + warning 日志）；来源重命名同事务级联 `resnapshot_fee_for_renamed_source()`：按新名费率重快照 pct 并重算 platform_fee/actual_received + recalc_customer_stats。

### 6.6 统计口径字典（P19-F3）

全部统计指标唯一定义于 db.py `metric_*` 专区，`get_dashboard_stats` / `get_overdue_orders` / `get_commission_type_distribution` / `get_stats_detail` 等同源调用。金额一律 `actual_received`（净额）；`income` 毛额仅在品类分布分列并标注。

| 指标 | 口径 |
|---|---|
| 月度已收 | payment_status='已结算' + scheduled_end 归月 → SUM(actual_received) |
| 月度预计 | is_archived=0 + 非终态 + scheduled_end 归月 → SUM(actual_received) |
| 年累进 | 已结算 actual_received 按归月逐月累加 |
| 品类分布 | is_archived=0 GROUP BY commission_type → COUNT + 毛/净分列 |
| 主页活跃 | is_archived=0 + 非终态 + 排期与范围相交 |
| 主页逾期 | `_overdue_where`：is_archived=0 + scheduled_end<today + 非终态（卡片/明细同源） |
| 主页收入/已完成 | is_archived=1 + 终态=完成（非退单）+ scheduled_end 落范围 → SUM/COUNT |
| 客户 total_spent | 该客户全部订单 SUM(actual_received)（含进行中/退单；名义合作规模口径） |

### 6.7 主题与外观

`_build_theme_css(settings)`：theme_*（11 键）+ stage_*（7 键）→ CSS 变量覆盖 + color-mix 派生 `*-bg`；字体 `font_size`/`font_family`。自定义主题：`_load_custom_themes`（settings.custom_themes JSON 列表 + active_custom_theme）→ 导入时 `_sanitize_theme_css` 消毒（剥危险语法），apply/delete 路由管理。快捷键：`shortcuts_json` + `merge_shortcuts()` 合并默认。

### 6.8 订单模板

`ORDER_TEMPLATE_FIELDS` 定义快照字段集；`create_order_with_template()` 单事务建单+可选存模板；`/orders/new?template=<id>` 预填；删除走 `/orders/templates/<id>/delete`。

### 6.9 图片管线（image_processor.py）

上传约束：扩展名 {png,jpg,jpeg,gif,webp,bmp}，≤10MB。三级产物写入 `uploads/orders/<id>/`：原图 · preview（宽 800）· thumb（200×200）。入口：`process_image_file`（本地文件/批量导入）· `process_uploaded_file`（单文件）· `process_uploaded_file_multi`（多图带 img_key）· `save_without_pillow`（Pillow 缺失降级）。`_refresh_order_cover()` 维护 orders 表封面冗余列（image_url/image_path/has_image）。画廊：`list_gallery_page()` 分页查询 + `/api/gallery` 无限滚动；客户详情页作品墙 `get_customer_images()`。

---

## 七、前端架构

### 7.1 页面骨架（base.html）

侧边栏（6 导航+新建 CTA，240px/56px 折叠态，localStorage 记忆）+ 主内容区；全局容器：`#center-modal`（680px 居中模态）· `#edit-drawer`（480px 右抽屉）· `#toast-container`；`theme_css` 注入 `<style>`；Lucide 初始化 + HTMX afterSwap 重渲染。

### 7.2 三种表单模式

| 模式 | 触发 | 加载 | 提交返回 | 关闭后刷新 |
|---|---|---|---|---|
| 居中模态 | `?modal=1` | `htmx.ajax → #center-modal-body` | modal_success.html | 日历+未排期池+甘特+统计 |
| 右抽屉 | `?inline=1` | `htmx.ajax → #drawer-body` | JSON + HX-Trigger: orderUpdated | `refreshCurrentView()` 按 pathname 分发 |
| 完整页 | 默认 | 整页 | redirect | — |

`refreshCurrentView()`（P19-F11）：/orders→refreshTable()；/orders/kanban→reloadKanbanBoard()（fetch 整页→DOMParser 提取 #kanban-board 局部替换，失败降级 reload）；/orders/<id>→reloadDetailContent()（保留归档确认定时链）；其余→calendar.refetchEvents()+统计刷新。

### 7.3 HTMX / fetch 纪律

- 局部刷新：统计卡 `/api/stats`、订单行 `/api/orders`（300ms 防抖）、未排期池、客户下拉。
- **dirtyFetch**（P19-F11）：fetch 包装，非 GET 且成功 → `markDataDirty()`（beforeunload 防未刷新离开）。所有 DB 写操作走 dirtyFetch；白名单（裸 fetch）：GET 读取、`/export/*`、`/api/log-error`。

### 7.4 app.js 模块划分（~60 函数）

模态框/抽屉（openCenterModal/closeCenterModal/openEditDrawer/closeDrawer）· 看板（initKanban/SortableJS 列内跨列+列头排序/updateKanbanStats/quickStageSwitch）· 回执小票（openReceipt/closeReceipt，统计点明细弹窗）· Toast · 导出 · 侧边栏状态 · Lightbox · 图片上传（initImageUpload 粘贴+选择/uploadImages/removeImage/appendEditThumb）· 页面模块定制器（loadModulePrefs/injectModuleToolbar/collectModules/buildModuleSettingsPanel，P16h 统计模块显隐）· 错误日志（logFrontendError/renderErrorLogs，localStorage 100 条+上报）· 存储清理 · 统计刷新 · ColorPicker（IIFE ~230 行，HSV↔Hex，Canvas SB 面板+色相条，fixed 智能定位，零依赖）。

### 7.5 vendor 本地按需加载

前端库全部本地化在 `static/vendor/`，无任何 CDN 依赖。base.html 仅 HTMX+Lucide+JsBarcode；Frappe Gantt（主页）/ Chart.js（收入）/ FullCalendar（日历）/ SortableJS（看板）在各页 `head_extra`/`scripts` 按需引入。

---

## 八、安全模型（P19-F7 收敛）

定位本地单用户、不引入鉴权体系，收敛点为「本机页面/浏览器插件」来源校验：

- **CORS 白名单** `_CORS_ORIGINS`：`http(s)://127.0.0.1|localhost[:port]`、`https://(www.)?mihuashi.com`、`chrome-extension://`；仅 `/api/*` 生效，页面路由不开放 CORS；不带凭据。
- **`_is_local_origin()`**：取第一个非空 Origin/Referer 判定——chrome-extension:// 前缀 / host∈{127.0.0.1,localhost} / 米画师宿主放行；两头缺失按同源导航放行；其余外站 → 403。
- **`/api/shutdown`**：仅 POST（GET→405）+ 来源校验（消除 `<img src>` 跨站关机面）。
- **`/api/import/mihuashi`**：复用来源校验。字段映射兼容多命名（projectName/custName/...），客户精确匹配否则自动创建，日期/价格容错解析；settings 含 `api_token` 键（预留）。
- **XSS**：模板默认 `{{ |e }}`；唯一 raw 输出 `render_notes` 过滤器——URL `html.escape(quote=True)` 后仅置 src，onclick 用 `window.open(this.src)`（P19-F6 防引号逃逸）。
- **SQL**：全量 `?` 参数绑定（project-rules R4）。
- **输入校验**：Pydantic ge=0 非负、日期 ISO 归一、跨字段（end≥start、fee≤总额）；路由查询日期参数非法 → 400；库内脏日期 `_iso_or_none`/`_safe_iso` 容错不 500。

---

## 九、设置系统（settings 表 69 键实测）

| 分组 | 键模式 | 数量 | 说明 |
|---|---|---|---|
| 全局主题 | `theme_*` | 11 | bg/surface/sidebar/text/text_secondary/border/accent/link/success/warning/danger |
| 阶段色 | `stage_*` | 7 | pending/sketch/lineart/detail/finish/completed/cancelled |
| 日历调色板 | `cal_{mode}_{label}` | ~31 | 5 模式（stage/source/ddl/payment/commission）× 各值独立色 |
| 选择列表 | `stage_list` · `payment_status_list` · `source_list` · `commission_type_list` | 4 | 用户自定义列表（合并语义 base） |
| 平台/费率 | `platform_sources` · `platform_fee_{src}` · `default_fee_{src}` | ~10 | 平台来源集合；fee 历史键与 default_fee 新键并存 |
| 外观 | `font_size` · `font_family` | 2 | 4 档字号 / 3 种字体 |
| 主题扩展 | `custom_themes` · `active_custom_theme` | 2 | 自定义主题 JSON 列表 + 当前应用 |
| 快捷键 | `shortcuts_json` | 1 | 用户快捷键覆盖 |
| 其他 | `api_token` · `schedule_conflict_threshold` | 2 | API 预留 / 排期冲突阈值 |

---

## 十、导出、导入与错误处理

- **导出**：`/export/orders` 用 `templates/export/orders.md` 渲染全量订单（覆盖写 `exports/全部订单.md`）；`/export/open-folder` 跨平台打开目录（os.startfile/open/xdg-open）。
- **导入**：`/api/import/mihuashi`（§8）；图片批量导入走 `process_image_file`。
- **后端错误**：errorhandler(500)/(Exception) 打印控制台 + 友好文案；前端 `window.error`/`unhandledrejection` → localStorage（≤100 条）+ `POST /api/log-error`（控制台 `[FRONTEND ERROR]`）；设置页「系统」Tab 查看/清空，另提供 localStorage/sessionStorage 缓存清理。

---

## 十一、启动方式

| 方式 | 命令 | 端口 |
|---|---|---|
| 启动器（推荐） | `python launcher.py` | 5001（参数可覆盖） |
| 直接运行 | `python app.py`（debug 仅此处启用） | 5000（`--port` 覆盖） |
| 批处理 | `run.bat` | 5001 |
| 打包 | PyInstaller exe | 5001（可传参） |

---

## 十二、设计决策记录

1. **SQLite + WAL**：本地零配置、单文件便携；WAL 支撑读写并发。
2. **HTMX + Jinja2 而非 SPA**：单体工具无前端框架复杂度，服务端渲染与局部刷新天然契合。
3. **中央注册表 + 合并语义**：选择列表去硬编码；settings 自定义与 auto-discover 合并保序，标准值永不丢失。
4. **终态元数据化**：terminal/kind/progress 显式注册，免疫 auto-discover 追加值对位置约定的破坏。
5. **归档唯一入口 + 快照列**：completed_at/is_overdue 归档时写入不再回算，保证收入归月稳定。
6. **费率快照落库**（platform_fee_pct）：历史订单金额与设置页费率脱钩。
7. **复购查询时计算**：消除写入式派生列的腐烂面。
8. **原生 ColorPicker**：替代 Blossom（overflow 裁剪/定位不可根治），零依赖完全可控。
9. **收款状态 5 值锁定**：统计口径稳定性的前提。
10. **来源校验替代鉴权**：本地单用户定位下，以最小成本阻断跨站读写。
11. **vendor 本地化 + 按需加载**：离线可用，且避免每页 ~1MB JS。
12. **`_MEIPASS` 资源路径兼容**：打包后模板/静态与可写数据分目录。

---

## 十三、已知约束（反目标）

不 SaaS 化/云同步 · 不引入 SPA 框架 · 不做 AI 自动排期 · 不做多语言 · 不做微服务/消息队列 · 保持 PyInstaller 可打包运行 · 前端库本地 vendor 化（无 CDN 依赖）。
