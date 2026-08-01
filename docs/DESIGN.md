# oimimo scheduler — 设计系统文档

> **基准日期**: 2026-07-28 · 本文档以 `static/app.css`（2852 行）与模板实测为准。
> 架构与数据流见 [ARCHITECTURE.md](ARCHITECTURE.md)；设计技能：`/clean` `/sleek` `/shadcn` `/theme-factory` `/spacious` `/frontend-design`。

---

## 一、设计原则

**Notion-inspired 极简**：字体优先、去阴影、细边框（hairline）、少即是多。信息层级靠字号/字重/墨色深浅表达，而非色块与投影。

- 界面语言：中文（不做国际化）
- 密度取向：紧凑详情网格 + 宽松留白并存（卡片 24px 内边距级）
- 品牌：双行式标题——主标 **oimimo** + 副标小字 *scheduler*；SVG logo（static/logo.svg + ico/png）

---

## 二、Design Tokens（CSS 自定义属性）

### 2.1 表面层级（Surface）

| Token | 值 | 用途 |
|---|---|---|
| `--color-bg` | `#f9f9f7` | 页面底色 |
| `--color-surface` | `#fcfcfb` | 卡片/图表表面 |
| `--color-sidebar` | `#f5f4f1` | 侧边栏 |

### 2.2 墨色层级（Ink）

| Token | 值 | 用途 |
|---|---|---|
| `--color-text` | `#0b0b0b` | 主文字 |
| `--color-text-secondary` | `#52514e` | 次要文字 |
| `--color-text-tertiary` | `#898781` | 弱化文字 |

派生交互色：`--color-hover: rgba(11,11,11,0.04)` · `--color-active: 0.08` · `--color-sidebar-hover: 0.06` · `--color-sidebar-active: #0b0b0b`。

### 2.3 边框

| Token | 值 |
|---|---|
| `--color-border` | `#e1e0d9`（hairline 细线） |
| `--color-border-strong` | `#c3c2b7`（强调边框） |

### 2.4 语义四色 + 信息色（各配 `*-bg` 浅底）

| 语义 | 主色 | 浅底 |
|---|---|---|
| success | `#0ca30c` | `#daf2da` |
| warning | `#fab219` | `#fef4d8` |
| danger | `#d03b3b` | `#fce4e4` |
| info | `#2a78d6` | `#dce9f8` |

另有 `--color-accent: #0b0b0b`（强调=主墨色）· `--color-link: #2a78d6`。

### 2.5 阶段七色（各配 `*-bg`）

| 阶段 | Token | 色值 |
|---|---|---|
| 待开始 | `--stage-pending` | `#898781` |
| 色稿 | `--stage-sketch` | `#2a78d6` |
| 线稿 | `--stage-lineart` | `#1baf7a` |
| 细化 | `--stage-detail` | `#eda100` |
| 收尾 | `--stage-finish` | `#4a3aa7` |
| 完成 | `--stage-completed` | `#008300` |
| 退单 | `--stage-cancelled` | `#e34948` |

### 2.6 几何 / 间距 / 字体 / 动效

| 组 | Tokens |
|---|---|
| 圆角 | `--radius-sm 4` · `md 6` · `lg 8` · `xl 12` · `full 9999px` |
| 间距 | `--space-2xs 4` · `xs 6` · `sm 10` · `md 16` · `lg 24` · `xl 32` · `2xl 40`（近似 8pt 网格） |
| 阴影（克制） | `--shadow-xs/sm/md/lg`（0.03–0.06 透明度级） |
| 焦点环 | `--color-ring: rgba(11,11,11,0.12)` · `--ring-offset: 2px` |
| 过渡 | `--transition-fast 0.12s` · `base 0.18s` · `slow 0.25s`（均 ease） |
| 字体 | `--font-sans: 'Inter' → 系统栈` · `--font-mono: 'JetBrains Mono' → SF Mono` |
| 布局 | `--sidebar-width: 240px`（折叠态 56px） |

---

## 三、主题系统

### 3.1 运行时主题注入

`app._build_theme_css(settings)` 每次请求渲染 `<style>` 注入 base.html，覆盖 app.css 默认值：

- `theme_*`（11 键）→ 映射到 Token
- 阶段色经 `STAGE_LABEL_TO_SLUG` 映射从 `cal_stage_<中文>` 生成 `--stage-*` 变量
- **bg 派生**：`color-mix(in srgb, <色> 12%, transparent)` 自动生成 `--stage-*-bg` 与语义色浅底

### 3.2 着色模式（5 种）

`CALENDAR_PALETTES`（stage/source/ddl/payment/commission），每种模式每个值独立 ColorPicker，落库为 `cal_{mode}_{label}` 键。事件着色优先级：订单 `custom_color` > settings 调色板 > 内置默认 > `#b0b0aa`。

配色统一作用于日历事件、订单徽章、收入图表等所有标签着色。「按阶段」面板遍历实际阶段 choices（自定义新增阶段默认灰，改色后存 `cal_stage_<名>`）。

### 3.3 配色预设

整套 `cal_*` 配色可命名保存为预设（`palette_presets` JSON 列表），支持保存/选用/更新/删除。使用中预设以主色描边 + 「使用中」徽章标记。

### 3.4 自定义 CSS 主题

`settings.custom_themes`（JSON 列表）+ `active_custom_theme`。导入 CSS → `_sanitize_theme_css()` 消毒后存储。

### 3.5 外观

| 设置 | 键 | 可选值 |
|---|---|---|
| 字号 | `font_size` | 14/16/18/20px → `--font-size-base` |
| 字体 | `font_family` | system / serif / mono → `--font-sans` |

---

## 四、布局系统

### 4.1 应用骨架

```
┌────────────┬──────────────────────────┐
│ Sidebar    │  Main Content            │
│ 240px/56px │  Page Header + 内容区      │
└────────────┴──────────────────────────┘
全局容器：#center-modal(680px) · #edit-drawer(480px) · #toast-container(右下)
```

### 4.2 响应式

- **断点（7）**：`max-width` 1200/1024/860/768/480 + `min-width` 1600/2000
- **容器查询**：`.dashboard { container-type: inline-size }`
- **画廊**：CSS columns——≥1600px 5 列 / ≤1200px 3 列 / ≤768px 2 列 / ≤480px 1 列
- **动效可访问性**：`prefers-reduced-motion: reduce` 降级

---

## 五、组件规范

| 组件 | 要点 |
|---|---|
| Card | 去阴影、hairline 边框、大留白 |
| Stats 卡 | 5 指标；点击弹小票弹窗（明细列表+环比箭头） |
| Table | Notion 数据库风；订单 12+ 列 + 复选；行点击进详情 |
| Stage Badge | 七色徽章（主色文字 + `*-bg` 底） |
| Button | 圆角、细边框、hover/active 墨色透明度反馈 |
| Form | 干净白底、细边框、ring 焦点；平台/直接双财务模式实时预览 |
| Detail Grid | Compact 双栏（订单详情：基本+财务） |
| 居中模态 | 680px，Notion 风，滑入动画 |
| 右抽屉 | 480px，含 24px 圆形 ColorPicker |
| Toast | 右下固定，0.25s 上滑入，2.5s 后淡出 |
| Lightbox | 图片全屏查看 |
| ColorPicker | 原生零依赖（~230 行）：SB Canvas+色相条+hex 输入 |
| Tooltip | JS Portal 渲染到 body（fixed 定位），220ms 延迟触发，触发区域收窄到 `?` 图标 |
| 阶段时间轴 | 横向步进器（节点+连线+进度条），完成态整条变绿 |
| 模块定制器 | 设置面板控卡片显隐，prefs 存 localStorage |

---

## 六、视图规范

### 6.1 甘特图（Frappe Gantt 0.6）

纯色条；日/周/月/年 4 视图 + 仅进行中/全部切换；拖拽改排期；今日虚线+圆标居中；容器 max-height 400px。

### 6.2 日历（FullCalendar 6）

月/周视图；5 种着色模式；逾期红边+发光；未排期池拖拽入历；拖拽移动/调时长自动保存。

### 6.3 看板（SortableJS）

7 阶段列；列头 sticky + 列统计（卡数+金额）；卡片 hover 快捷 ◀/▶/✓；阶段色左边框 + 逾期/临期标识；内容筛选；全量加载。

### 6.4 收入看板（Chart.js 4 + gridstack.js）

| 图表 | 类型 | 交互 |
|---|---|---|
| 月度收入 | Line 面积 | 数据标签 k/w，点小票 |
| 每月预计 | Line 虚线面积 | 点小票 |
| 年累进 | Line 面积 | 逐月累加 |
| 品类分布 | Doughnut | 百分比+毛净分列 |
| 时薪统计 | 3 卡+3 图 | 趋势/品类/来源条形图 |
| 客户排名 | 表格 Top 8 | 点击进详情 |

gridstack.js 12 列自定义布局（Notion 式拖拽+缩放+持久化）；默认静态只读防误拖。

### 6.5 画廊

瀑布流 columns + 无限滚动（IntersectionObserver）；多条件筛选；Lightbox。

### 6.6 页面过渡

`.sidebar { view-transition-name }` 参与视图过渡，消除跳转黑屏。

---

## 七、设计-代码映射

| 设计资产 | 落点 |
|---|---|
| Tokens 默认值 | `static/app.css` `:root` |
| Tokens 运行时覆盖 | `app._build_theme_css()` → base.html `<style>` |
| 阶段色/语义色 | CSS 变量 ← settings `cal_stage_*` / `theme_*` |
| 日历调色板 | `db.CALENDAR_PALETTES` 默认 + settings `cal_*` 覆盖 |
| 交互动效 | `static/app.js`（Toast/模态/抽屉/ColorPicker/布局） |

**修改纪律**：改色先改 token，禁硬编码色值进组件；阶段相关一律走 `--stage-*` 变量。
