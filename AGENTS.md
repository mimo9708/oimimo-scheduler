# AGENTS.md — AI 编码助手 / 开发者指南

本文件是本仓库的"项目地图"，供 AI 编码助手和人类开发者快速上手、安全迭代。

## 项目一句话

面向独立插画师的**本地**接单排单管理工具：Flask + SQLite 单机应用，浏览器作 UI，无网络依赖、无账号体系。

## 技术栈与硬约束

- **后端**：Python 3.9+ / Flask 3 / Pydantic v2（校验）/ sqlite3 标准库（无 ORM）
- **前端**：Jinja2 模板 + HTMX 2 局部刷新。**没有前端构建步骤**，没有 Node/npm，所有第三方 JS 在 `static/vendor/` 本地化（离线可用），不要引入 CDN 链接
- **数据库**：SQLite 单文件 `orders.db`（WAL 模式），随首次运行自动创建
- **保持本地优先**：不要加入任何联网上报、账号、SaaS 化功能

## 文件-职责对照表

| 路径 | 职责 |
|---|---|
| `app.py` | 路由层（33 个 `@app.route`）：页面渲染 + HTMX partial + JSON API，全部业务入口 |
| `db.py` | 数据层：schema 建表/迁移、全部 SQL 读写函数、统计口径计算 |
| `models.py` | Pydantic 模型：订单/客户表单校验与类型转换 |
| `image_processor.py` | 图片处理：上传图压缩、预览图/缩略图生成（Pillow） |
| `launcher.py` | 桌面启动器：依赖自检安装 → 起 Flask 线程 → 开浏览器 → pystray 托盘图标；已适配 PyInstaller（`sys.frozen` / `_MEIPASS` / `resource_path()`） |
| `templates/base.html` | 全局布局：侧边栏导航、主题 CSS 变量注入、模态容器 |
| `templates/index.html` | 主页仪表盘（统计卡/甘特图/本周排单） |
| `templates/calendar.html` | 日历视图（FullCalendar 初始化与拖拽逻辑） |
| `templates/orders/` | 订单：列表/看板/详情 + 3 种表单（form_modal 居中模态 / form_inline 右侧抽屉 / form 完整页）+ 模板管理 |
| `templates/customers/` | 客户列表/详情/表单 |
| `templates/partials/` | HTMX 局部刷新片段（订单行、看板卡、统计卡、归档确认链等） |
| `templates/settings.html` | 设置页 4 Tab（主题/列表选项/数据/关于） |
| `static/app.css` | 全部样式：`:root` CSS 变量主题体系（18 色）+ 组件样式 |
| `static/app.js` | 前端逻辑：模态/抽屉管理、HTMX 事件钩子、Lightbox、拖拽辅助 |
| `static/vendor/` | 本地化第三方库：htmx / fullcalendar / chart / frappe-gantt / sortable / jsbarcode / lucide |
| `packaging/` | 发布工具：`sync_release.bat`（开发版→本仓库同步）/ `build.bat`（一键打包）/ `installer.iss`（Inno Setup） |

## 数据流

```
浏览器 (HTMX 请求)
   → app.py 路由（models.py 校验入参）
   → db.py SQL 函数（sqlite3, WAL）
   → 返回 Jinja2 partial (templates/partials/) 或 JSON
   → HTMX 局部替换 DOM
```

图片上传走 `app.py → image_processor.py → uploads/` 目录（运行时生成，不入库）。

## 既有约定（改代码前必读）

1. **HTMX partial 模式**：列表/看板等页面的增删改走 HTMX，返回 `templates/partials/` 下的片段而非整页；新增交互优先沿用此模式
2. **主题体系**：颜色一律用 `static/app.css` 中 `:root` 的 CSS 变量（18 色），不要写死色值；日历另有 5 种着色模式独立调色板（settings 页可配）
3. **统计口径**：收入统计区分毛/净、平台/直接两种财务模式，口径集中在 `db.py`，不要在模板里重新计算
4. **schema 迁移**：改表结构时在 `db.py` 的 init/migrate 逻辑中用 `ALTER TABLE` 增量迁移，保证老用户 `orders.db` 直接升级不丢数据
5. **运行时产物不入库**：`orders.db`、`uploads/`、`exports/`、`logs/`、`__pycache__/` 均已在 `.gitignore`，不要提交

## 本地运行与验证

```bash
pip install -r requirements.txt
python launcher.py    # 推荐：启动器（自动开浏览器 + 托盘）
python app.py         # 或：纯 Flask 调试模式
```

验证清单（改完代码至少跑一遍）：
1. 启动无报错，首页 `/`、日历 `/calendar`、新建订单 `/orders/new` 均正常渲染
2. 新建一条测试订单 → 在列表/日历/看板中可见 → 删除
3. 若改了 schema：删掉本地 `orders.db` 重新生成一次 + 用旧库跑一次迁移

## 发布流程（维护者）

本仓库是**发布镜像**：日常开发在私有开发目录进行，大版本发布时：
1. 运行 `packaging/sync_release.bat` 从开发目录单向同步（自动排除数据库/上传图/日志）
2. 本仓库 commit `vX.Y.0` + push
3. 运行 `packaging/build.bat` 一键产出 `oimimo-setup.exe` + `oimimo-portable-win64.zip`
4. GitHub Release 打 tag 并上传两个产物，更新 `CHANGELOG.md`

## 深入文档

- `docs/ARCHITECTURE.md` — 路由全表、DB schema、进程模型、核心机制
- `docs/DESIGN.md` — Design Tokens、组件与视图规范
- `docs/PROJECT.md` — 功能矩阵与产品说明
