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

### 2.1b 日历订阅第二监听（Spec 20，feed_enabled=1 时）

主端口 1096（127.0.0.1，本机页面）之外，`start_feed_server()` 拉起 `0.0.0.0:1097` 第二监听，仅供局域网手机日历拉取 ICS：

- **双模式拉起**：源码模式（dev）用独立子进程（env 剔除 `WERKZEUG_*`，防 debug reloader 的 fd 复用/env 污染双陷阱；端口被占自重试接管，`atexit` 连带 terminate）；打包模式（frozen exe）降级同进程守护线程
- **端口守卫**：`before_request` 判 `SERVER_PORT=='1097'` 且非 `/api/feed/` 前缀 → 403；页面/表单/静态资源永远到不了局域网
- **生命周期**：`feed_enabled=1` 保存时热启动（幂等），开启即时生效；关闭需重启（不热停）；launcher `_start_server()` 与 `__main__`（reloader 守卫内）各调用一次
- **鉴权**：`?token=` 匹配 settings `feed_token`（32 hex，可经设置页旋转，旧 URL 立即失效）；设置页「日历订阅」卡片提供开关/URL 复制/旋转

### 2.2 路径分离（PyInstaller 兼容）

| 用途 | 函数 | 开发态 | 打包态 |
|---|---|---|---|
| 只读资源 | `app._resource_dir()` / `launcher.resource_path()` | 脚本目录 | `sys._MEIPASS` |
| 可写数据 | `db.data_dir()` | 脚本目录 | exe 同级目录 |

`DB_PATH = data_dir()/orders.db`；备份目录 `get_backup_dir()` 从 settings 表读取（默认 `data_dir()/backups/`）。

### 2.3 tests/ 测试基建（Spec 25 首建，卡 98）

| 文件 | 职责 |
|---|---|
| tests/conftest.py | 会话级临时根目录隔离：导入期重定向 `db.DB_PATH`/`DEFAULT_BACKUP_DIR` + monkeypatch `db.data_dir` 后 import app；client/pricelist_item/autouse 零残留 fixture + PIL 上传辅助（make_png_bytes） |
| tests/test_pricelist_v2.py | 价目表 A-T01~13（16 用例：CRUD/区间/例图上限/Origin 守卫/inf 拒收） |
| tests/test_receipt.py | 小票服务端 R-T01~18+R-T22（19 用例：计算对拍/边界/错误输入/鲁棒） |
| tests/test_security.py | 安全专项 R-T19 XSS 服务端侧 / R-T20 SQLi / R-T21 Origin+CORS（9 用例） |
| tests/test_payments_*.py（7 文件，Spec 26） | migration 4（建表/幂等/级联/孤儿清理）/ stats 7（事件流手算对拍+主页口径同源）/ crud 7（收齐自动结算+只进不退）/ guards 6（白名单/冲突/切换/免收四守卫）/ routes 5（三路由+Origin 守卫+待收口径）/ ui 5（表单开关+时间线+panel 同源）/ receipt_copy 5（小票三数+按笔+文案） |
| tests/snapshot_stats.py | 统计对拍快照工具（Spec 26 卡 103）：metric_* × dashboard × stats_detail × 三图全出口 × 7 范围矩阵，冻结时钟确定性输出，`--compare` 逐位对比（阶段 1 验收门槛） |
| tests/test_spec31_card_view.py | Spec 31 卡片视图（4 用例：双结构 DOM 冒烟 + 网格 CSS + toggle 控件 + 视图记忆） |
| tests/test_cleanup_images.py | Spec 30 孤立图片清理（用例含清理路由 + 路径归一化 + FK 约束） |
| tests/test_spec30_regression.py | Spec 30 回归（图片上限扩容 + 分类导出 + 压缩参数 + 孤立清理端到端） |
| tests/test_feed_etag.py | 日历订阅 ETag/304 + 访问日志 |
| tests/test_stats_lab_nl.py | Spec 27 NL 意图解析器（关键词匹配 + 路由） |
| tests/test_stats_lab_presets.py | Spec 27 预设 CRUD + 数据源扩展 |

隔离口径：全部用例走独立临时 DB/目录，绝不触碰真实 `orders.db`/`uploads/`；autouse fixture 逐用例零残留。运行：`pytest tests/ -q`（当前 162 用例）；build.spec excludes 含 tests（不打包）。

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
| `/tools` | tools/index.html | 小工具市场（搜索+分组 chip+最近使用） |
| `/tools/reply-templates/` | tools/reply_templates.html | 回复模板（分组侧栏+卡片） |
| `/tools/pricelist/` | tools/pricelist_preview.html | 价目表预览（双视图：长条 #pl-menu + 卡片 #pl-cards 网格，视图切换分段控件 + localStorage 记忆；导出统一 `renderOffscreenToBlob` helper，按分类恒 640px + 单卡下载按钮） |
| `/tools/pricelist/edit` | tools/pricelist.html | 价目表编辑看板（分类分节+拖拽+例图管理） |
| `/tools/pricelist/preview` | → 301 重定向 `/tools/pricelist/` | 旧预览地址兼容 |
| `/tools/receipt/` | tools/receipt.html | 小票预览（默认落地：D15 四按钮 + 打印白底黑字 + PNG 导出） |
| `/tools/receipt/edit` | tools/receipt_edit.html | 小票编辑（六分区编辑器 + 实时预览 + datalist 联动 + 模板管理；方案B 柔和填充控件作用域 `.rc-edit-grid`，主图模式分段选择器 灰度/抖动/彩色） |
| `/stats-lab` | stats_lab.html | 统计实验室（Spec 27：NL 意图解析 + 预设持久化 + 多数据源 + 口径速查） |
| `/customers/batch` | — | 客户批量操作 |
| `/export/open-folder` | — | 打开导出目录（Explorer） |
| `/uploads/<path>` | — | 上传文件静态服务 |

### 4.2 数据 API（GET + POST）

| 路由 | 返回 | 说明 |
|---|---|---|
| `/api/gallery` | JSON | 画廊分页（offset/limit） |
| `/api/stats` | HTML | 统计卡 HTMX 刷新 |
| `/api/stats/detail` | JSON | 小票明细（Spec 26 扩展：expected 附三数列，事件流附 payment_mode） |
| `/orders/<id>/payments/panel` | HTML | 收款卡片局部刷新（详情页 installment 单；simple 防御空串，Spec 26） |
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

`/orders` 创建 · `/orders/<id>/edit` 更新 · `/orders/<id>/delete` · `/orders/<id>/archive`（归档确认链）· `/orders/<id>/stage` · `/orders/<id>/reschedule` · `/orders/<id>/color` · `/orders/<id>/unschedule` · `/orders/batch`（阶段/来源/收款/DDL/商用/归档/删除/重算）· `/orders/<id>/upload-image` · `/orders/<id>/remove-image` · `/orders/<id>/work-hours` · `/orders/templates/<id>/delete` · `/orders/<id>/payments`（记一笔 POST，Spec 26）· `/orders/<id>/payments/<pid>`（改/删笔 PUT/DELETE，Spec 26）

### 4.4 设置与系统（POST）

`/settings` · `/settings/reset` · `/settings/theme/import` · `/settings/theme/apply` · `/settings/theme/delete` · `/settings/color-mode` · `/settings/palette/save` · `/settings/palette/apply` · `/settings/palette/update` · `/settings/palette/delete` · `/settings/income-layout` · `/settings/stage-flows` · `/settings/backup-dir` · `/settings/backup` · `/settings/backups` · `/settings/restore/<filename>` · `/settings/backup/delete/<filename>` · `/settings/commission-merge` · `/settings/source-merge` · `/settings/feed-token/rotate` · `/export/orders` · `/api/import/mihuashi` · `/api/shutdown` · `/api/log-error`

### 4.5 小工具平台（Spec 22）

- **注册表**：`TOOL_REGISTRY`（`_TOOL_REGISTRY_RAW` + `_build_tool_registry()` 重复 slug 校验）；`_tool_or_404(slug)` 统一取工具元数据
- **回复模板**：`POST /tools/reply-templates/` 创建 · `<id>/update` · `<id>/delete` · `group/rename` · `group/delete`
- **价目表**：`POST /tools/pricelist/` 创建 · `<id>/update` · `<id>/delete` · `<id>/upload-example`（JSON，多例图上限 3 张） · `images/<img_id>/delete`（JSON，清三件套文件+回填首图） · `images/reorder`（JSON ids → 204） · `reorder`（JSON ids → 204） · `meta`（JSON）· `board`（分类看板页）；2026-08-12 多图化后原 `<id>/remove-example` 移除（由 `images/<img_id>/delete` 取代）
- **小票打印机**（Spec 23）：`POST /tools/receipt/draft`（整体保存，Pydantic 校验 400） · `upload-image`（gray/dither 黑白化 / color 保留原色 + 三级产物） · `remove-image` · `upload-bg`（单文件 1600px 缩放） · `remove-bg` · `templates/` 创建 · `templates/<id>/apply`（只合并 style+文案，不触碰 items/计算参数） · `templates/<id>/update` · `templates/<id>/delete`（引用检查 `receipt_bg_referenced`） · `mult-presets`（Spec 24 单品倍率预设保存）；渲染口径唯一来源 `static/receipt.js`（`rcRender`/`rcCalc`/`rcEditorInit`），四预设 list/retro/hand/mono；**Spec 24 计算模型**：单品倍率（无/商用/买断快捷预设 + 手改）+ 单品/整单折扣双形态（直减金额 / 中文折数×/10），冻结公式 =（单价×量+Σ附加）×单品倍率×单品折扣 → Σ合计 → ×整体倍率 − 整体折扣 − 定金 = 尾款（赠品恒 0 不参与倍率折扣）
- **写响应约定**：成功 → 整区片段 + `HX-Retarget`（#rt-app/#pl-app）+ `HX-Reswap: innerHTML` + `HX-Trigger`（`json.dumps` 默认转义，HTTP 头禁非 latin-1）；校验失败 → 表单片段 + 400（app.js `htmx:responseError` 仅限 `.tool-htmx-form` 手动换入）

---

## 五、数据库设计

### 5.1 表结构（实测 orders.db）

**orders（39 列 + 7 索引，Spec 26 增 payment_mode）**

| 分组 | 列 |
|---|---|
| 标识/关联 | id PK · customer_id FK→customers ON DELETE SET NULL |
| 内容 | project_name · source · is_commercial · commission_type · platform_url · notes · custom_color |
| 流程 | current_stage · ddl_status · payment_status · stage_flow（本单阶段快照 JSON） |
| 财务 | deposit · balance · income · platform_fee · platform_fee_pct（快照）· actual_received · payment_mode（收款方式 simple 默认/installment 分期，Spec 26）|
| 折扣（Spec19） | discount_pct（订单级折扣快照 %）· discounted_income（折后金额） |
| 归档 | is_archived · completed_at · is_overdue |
| 复购 | is_repeat · repeat_count（查询时计算，列保留作回滚兜底） |
| 排期 | page_deadline · scheduled_start · scheduled_end · sort_order |
| 时薪 | estimated_hours · work_hours · exclude_hourly（不进 `_MONEY_FIELDS`） |
| 图片 | image_url · image_path · has_image |
| 审计 | created_at · updated_at |

**customers（10 列）**：id · name UNIQUE · platform_url · preferences · notes · tags · total_spent · purchase_count · created_at · updated_at

**settings（86 键）**：key PK, value — 详见 §9

**order_images（6 列）**：id · order_id · image_url · image_path · sort_order · created_at

**order_payments（6 列，Spec 26 分期收款流水）**：id · order_id（FK → orders ON DELETE CASCADE）· paid_at（到账日 YYYY-MM-DD）· amount REAL（ge=0）· note（≤500 可空）· created_at；双索引（idx_payments_order / idx_payments_paid_at）；孤儿记录启动时自动清理；写入仅经收款 CRUD 状态机（§6.8），无直接 INSERT 路径

**order_templates（4 列）**：id · name · data_json · created_at

**reply_templates（7 列，Spec 22）**：id · group_name（默认「未分组」，db 层空值兜底） · title · content · sort_order · created_at · updated_at

**pricelist_items（11 列，Spec 22 + Spec 30 例图上限 10）**：id · category（默认「默认」） · name · price REAL · price_max REAL NULL（价格区间上限，可空） · unit · description · example_image_path（首图 preview 相对路径，多图后始终回填首图） · sort_order · created_at · updated_at；例图三级产物落 `uploads/pricelist/<id>/`

**pricelist_images（5 列，2026-08-12 多例图）**：id · item_id（FK → pricelist_items ON DELETE CASCADE） · image_path · sort_order · created_at；每项目最多 10 张（Spec 30 路由层拦截），对齐 `order_images` 多图模式

**receipt_items（12 列，Spec 23 + Spec 24）**：id · name · price REAL（主项单价；附加子行=单项加价金额） · qty REAL · parent_id（自引用 FK ON DELETE CASCADE，附加服务子行挂主项） · is_gift（1=赠品划线计 0） · multiplier REAL（单品倍率默认 1，Spec 24） · mult_label（倍率标签，小票角标，Spec 24） · discount_type（单品折扣形态 none/amount/rate，Spec 24） · discount_value REAL（金额或中文折数，Spec 24） · sort_order · created_at

**receipt_templates（5 列，Spec 23）**：id · name · config_json（只存 style+文案，不存制品/计算参数 D14） · created_at · updated_at；小票草稿另存 settings `receipt_draft` 键（全删全插 CRUD）

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

- **财务** `_calc_financials`：**先折后费**（Spec19）——`discount_pct` 非 NULL → `discounted_income = income × 折扣%`（NULL → 折后=原价）；平台来源 → `platform_fee = 折后 × pct%`，直接来源 → 0；`actual_received = 折后 − 费`。折扣为订单级快照（建单从客户带出、可逐单改，客户改折扣不回溯存量，与费率快照同构）
- **DDL** `_auto_calc_ddl_status`：终态优先 → 满足归档集合时联动 → 按 scheduled_end 与今天比较
- **归档唯一入口**：`set_archived()` / `_apply_archive_to_data()` 写 `completed_at` + `is_overdue` 快照
- **复购查询时计算**：`_apply_repeat_for_rows()`（不落库）
- **days_remaining 读取时注入**：`_apply_days_remaining_for_rows()`（新增返回订单 dict 的函数时必须调用）

### 6.4 update_order 单管线

读旧单 → merge → 费率快照规则 → `_calc_financials` → DDL 重算+条件归档 → UPDATE → recalc_customer_stats。

### 6.5 统计口径字典（Spec 26 事件流 + 口径分离）

**口径分离原则**：工作口径（描述"事"的进展）与现金口径（描述"钱"的到账）自 Spec 26 起分离——工作口径沿用订单快照列；现金口径统一走**收款事件流**。

**事件流构建器 `_payment_events_sql()`**（db.py 唯一真实来源，恒输出 ev_date/ev_amount/ev_mode）：

- 整单（simple）：虚拟事件 `[scheduled_end, actual_received]`——过滤条件逐字沿用旧口径，数学等价（老订单统计逐位不变）
- 分期（installment）：JOIN order_payments 每笔真实事件 `[paid_at, amount]`——到账即计，无状态过滤

| 口径 | 指标 | 数据源 |
|---|---|---|
| 工作口径 | 进行中 / 已完成 / 逾期 / 时薪 / 品类分布 | 订单快照列（不因收款方式变化） |
| 现金口径 | 本期到账（metric_realized_income）/ 月度收入 / 年累进 | 事件流（同源：get_stats_detail income/monthly_income 成对） |
| 现金口径 | 待收金额（metric_expected_income）/ 每月预计 | simple=净额；installment=净额−Σ已到账（下限 0） |
| 工作口径（明确不动） | completed 小票 / 客户 total_spent | 旧口径保留 |

年份推算 `get_available_years()`：scheduled_end ∪ paid_at（跨年尾款年份可见，D11）。对拍验证：`tests/snapshot_stats.py` 全出口 × 7 范围矩阵，改造前后逐位一致（阶段 1 验收门槛，全 simple 数据 CASE 短路等价）。

### 6.6 备份管理

- `get_backup_dir()`：settings 表读取自定义路径，留空回退 `DEFAULT_BACKUP_DIR`（exe 同级 `backups/`）
- `set_backup_dir(path)`：校验存在+可写，保存到 settings
- 5 个备份函数统一使用 `get_backup_dir()`：`_do_backup` / `get_backup_list` / `delete_backup` / `restore_backup` / `check_data_recovery_needed`
- `delete_backup`：basename 校验防路径穿越 + 仅允许 `orders_backup_*.db` 格式
- 启动时不自动备份（仅保留手动入口）

### 6.7 订单删除自动清理

`delete_order()` 删除订单后检查 `commission_type` / `current_stage` / `source` 三个字段，无订单使用则自动移除对应 `cal_*` 颜色配置。

### 6.8 收款状态机（Spec 26，只进不退）

- **收齐自动结算**：`add_payment`/`update_payment` 单事务内先模拟校验 Σ笔（≤ 净额+0.01 容差 `PAYMENT_EPSILON`、笔数≤50）→ 写流水 → Σ笔 ≥ actual_received−0.01 且当前'未收款' → 经 `update_order` 置'已结算'走既有归档链 + 客户重算；杜绝 with-return 半成品提交
- **只进不退（D4）**：删笔/改笔不回退状态、不撤销归档；不一致由详情页警告条呈现（状态已结算但 Σ笔 < 净额−0.01）
- **金额冲突拦截（D12①）**：update_order 金额改小致净额 < 已到账−0.01 → 拒绝并整单回滚
- **切换双向保护（D12③）**：installment 有收款记录拒绝切回 simple（先清空再切）；simple 已收齐切 installment 同事务自动生成 1 笔初始收款 [有效排期日或今日, actual_received]（0 元跳过留痕）
- **免收互斥**：免收状态与 installment 互斥（切向与置免收双向拦截）
- `update_order` 返回 `(ok, err)` tuple；payment_mode 经 `order_columns` 白名单登记（D10 保持性）

---

## 七、前端架构

### 7.1 页面骨架

侧边栏（7 导航含「小工具」+新建 CTA，240px/56px 折叠态，localStorage 记忆；`hx-preserve="true"` 跨导航保留 DOM）+ 主内容区（`hx-boost="true"` 局部导航，仅 `<main>` AJAX 刷新 + View Transitions 平滑转场）；全局容器：`#center-modal`（680px 居中模态）· `#edit-drawer`（480px 右抽屉）· `#toast-container`（右下角）。

### 7.2 三种表单模式

| 模式 | 触发 | 提交返回 |
|---|---|---|
| 居中模态 | `?modal=1` | modal_success.html |
| 右抽屉 | `?inline=1` | JSON + HX-Trigger |
| 完整页 | 默认 | redirect |

### 7.3 app.js 模块（~70 函数）

模态框/抽屉 · 看板（SortableJS） · 回执小票 · Toast · 导出 · 侧边栏 · Lightbox · 图片上传 · 模块定制器 · 错误日志 · ColorPicker（原生零依赖 ~230 行）· 图表主题 helper · gridstack 布局管理 · 小工具桥接（`recordRecentTool` + closeCenterModal/showToast 事件监听 + `.tool-htmx-form` 400 换入）· **页面导航**：`runPageInit()` 三路径统一初始化（DOMContentLoaded / htmx:afterSettle boosted / htmx:historyRestore）· `updateSidebarActive()` 侧边栏高亮 · `onceBind()` 会话级一次性绑定 · 脏数据拦截（form `__formDirty` → `htmx:beforeRequest` boosted GET confirm）

### 7.4 vendor 本地按需加载

HTMX+Lucide+JsBarcode（base.html 全局）；Frappe Gantt / Chart.js / FullCalendar / SortableJS / gridstack（base.html head 全局加载，hx-boost 导航不再重复加载）

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

## 九、设置系统（settings 表 90+ 键，随 `cal_*` 着色动态增减）

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
| 小工具 | `pricelist_meta`（1） | 价目表菜单标题/附注 JSON（Spec 22） |
| 小工具 | `receipt_draft`（1） | 小票草稿整体 JSON（Spec 23，键移除回退默认草稿） |
| 小工具 | `receipt_mult_presets`（1） | 单品倍率快捷预设 JSON（Spec 24，损坏/缺失回退默认） |
| 其他 | `api_token` · `shortcuts_json` 等 | API 预留/快捷键/杂项 |

### 9.1 主题引擎（Spec 32）

服务端渲染时由 `theme_css(settings)` 生成 `:root` 变量块：

- **亮度判深**：`_relative_luminance(theme_bg)` < 0.35 判深色主题（缺键/非法值回退浅色）
- **accent 对比字色**：`db._contrast_text_color(accent)` 注入 `--color-accent-text`，`--color-accent-hover` 向对比极混合 15%；按钮文字统一消费 accent-text，白/黑 accent 均可见
- **深色六项派生**（用户未显式配置时）：text-tertiary / border-strong / hover / active / sidebar-hover / placeholder
- **warning 主题派生**（卡 154）：浅色主题下 `--color-warning` 压暗为 `color-mix(warning 55%, #000)` 深琥珀（亮琥珀黄作文字对浅底 ratio≈1.8 不可见），深色主题保持用户原值
- **color-scheme**：按深色判定注入 `dark`/`light`，原生控件（select/date 弹层）随主题配色
- **预览同步**：settings.html `applyPreset`/单色微调实时同步上述派生（`_relLuminance`/`_syncColorScheme`/`_syncAccentDerived`/`_warningEffective` 等 helper），预览态与保存后一致
- **缓存穿透**：app.css/app.js 引用带 `?v={{ APP_VERSION }}`，改静态资源必升 APP_VERSION（当前 1.4.0）

---

## 十、启动方式

| 方式 | 命令 | 端口 |
|---|---|---|
| 启动器（推荐） | `python launcher.py` | 1096 |
| 直接运行 | `python app.py --port 1096` | 1096 |
| 批处理 | `run.bat` | 1096 |
| 打包 | PyInstaller exe（`build.spec`） | 1096 |
| 一键构建（卡 88） | `build.bat`（自动建/复用 `.venv-build` 干净环境 → 锁定版本装依赖 → 出包 → 体积对比追加 `logs/build-size.log`；exe 运行中守卫拒绝覆盖） | — |

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
12. **口径分离 + 事件流（Spec 26）**：工作口径看进展、现金口径看到账；整单虚拟事件数学等价保旧数据逐位不变；收款状态机只进不退
13. **主题引擎派生变量（Spec 32）**：亮度判深（阈值 0.35）+ accent 对比字色 + 深色六项派生 + warning 浅色压暗，服务端注入与设置页预览同口径，双主题对比度 ratio≥3 审计保障
14. **hx-boost 局部导航（卡 156）**：`<body hx-boost>` + `<aside hx-preserve>` 实现类 SPA 导航（仅 `<main>` AJAX 刷新），View Transitions 平滑转场；`runPageInit()` 消费式初始化三路径统一；`HX-Boosted` 请求头分支返回完整页（非 fragment）

---

## 十二、已知约束（反目标）

不 SaaS 化/云同步 · 不引入 SPA 框架 · 不做 AI 自动排期 · 不做多语言 · 不做微服务/消息队列 · 保持 PyInstaller 可打包运行 · 前端库本地 vendor 化。
