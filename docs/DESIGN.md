# oimimo scheduler — 设计系统文档

> **基准日期**: 2026-07-13 · 本文档以 `static/app.css`（2066 行）与模板实测为准。
> 架构与数据流见 [ARCHITECTURE.md](ARCHITECTURE.md)。

---

## 一、设计原则

**Notion-inspired 极简**：字体优先、去阴影、细边框（hairline）、少即是多。信息层级靠字号/字重/墨色深浅表达，而非色块与投影。

- 界面语言：中文（不做国际化）
- 密度取向：Codex-style 紧凑详情网格 + 宽松留白并存（卡片 24px 内边距级）
- 品牌：P18-F1 双行式标题——主标 **oimimo** + 副标小字 *scheduler*；SVG logo（static/logo.svg + ico/png）

---

## 二、Design Tokens（CSS 自定义属性实测）

### 2.1 表面层级（Surface）

| Token | 值 | 用途 |
|---|---|---|
| `--color-bg` | `#f9f9f7` | 页面底色 |
| `--color-surface` | `#fcfcfb` | 卡片/图表表面 |
| `--color-sidebar` | `#f5f4f1` | 侧边栏 |

### 2.2 墨色层级（Ink）

| Token | 值 | 用途 |
|---|---|---|
| `--color-text` | `#0b0b0b` | 主文字（hover/active 由它派生透明度） |
| `--color-text-secondary` | `#52514e` | 次要文字/侧边栏文字 |
| `--color-text-tertiary` | `#898781` | 弱化文字 |

派生交互色：`--color-hover: rgba(11,11,11,0.04)` · `--color-active: 0.08` · `--color-sidebar-hover: 0.06` · `--color-sidebar-active: #0b0b0b`。

### 2.3 边框

| Token | 值 | 用途 |
|---|---|---|
| `--color-border` | `#e1e0d9` | hairline 细线（主要分隔手段） |
| `--color-border-strong` | `#c3c2b7` | 基线/强调边框 |

### 2.4 语义四色 + 信息色（各配 `*-bg` 浅底）

| 语义 | 主色 | 浅底 |
|---|---|---|
| success | `#0ca30c` | `#daf2da` |
| warning | `#fab219` | `#fef4d8` |
| danger | `#d03b3b` | `#fce4e4` |
| info | `#2a78d6` | `#dce9f8` |

另有 `--color-accent: #0b0b0b`（强调=主墨色）· `--color-link: #2a78d6`。

### 2.5 阶段七色（各配 `*-bg`）

| 阶段 | Token | 色值 | 语义 |
|---|---|---|---|
| 待开始 | `--stage-pending` | `#898781` | 中性灰 |
| 色稿 | `--stage-sketch` | `#2a78d6` | 蓝（categorical slot 1） |
| 线稿 | `--stage-lineart` | `#1baf7a` | 青（slot 2） |
| 细化 | `--stage-detail` | `#eda100` | 黄（slot 3） |
| 收尾 | `--stage-finish` | `#4a3aa7` | 紫（slot 5） |
| 完成 | `--stage-completed` | `#008300` | 绿（slot 4） |
| 退单 | `--stage-cancelled` | `#e34948` | 红（slot 6） |

### 2.6 几何 / 间距 / 字体 / 动效

| 组 | Tokens |
|---|---|
| 圆角 | `--radius-sm 4` · `md 6` · `lg 8` · `xl 12` · `full 9999px` |
| 间距 | `--space-2xs 4` · `xs 6` · `sm 10` · `md 16` · `lg 24` · `xl 32` · `2xl 40`（近似 8pt 网格） |
| 阴影（克制使用） | `--shadow-xs/sm/md/lg`（0.03–0.06 透明度级） |
| 焦点环 | `--color-ring: rgba(11,11,11,0.12)` · `--ring-offset: 2px` |
| 过渡 | `--transition-fast 0.12s` · `base 0.18s` · `slow 0.25s`（均 ease） |
| 字体 | `--font-sans: 'Inter' → 系统栈` · `--font-mono: 'JetBrains Mono' → SF Mono/Fira Code/Consolas` |
| 布局 | `--sidebar-width: 240px`（折叠态 56px，同一变量联动主内容区） |

---

## 三、主题系统

### 3.1 运行时主题注入

`app._build_theme_css(settings)` 在每次请求渲染 `<style>` 注入 base.html，覆盖 app.css 默认值：

- settings 键：`theme_*`（11 个：bg/surface/sidebar/text/text_secondary/border/accent/link/success/warning/danger）+ `stage_*`（7 个阶段色）→ 映射到上表 token。
- **bg 派生**：`color-mix(in srgb, <色> 12%, transparent)` 自动生成 `--stage-*-bg` 与 `--color-{success,warning,danger}-bg`。
- 设置页「主题设置」Tab 提供 3 组 18 色调色行（ColorPicker 取色）。

### 3.2 自定义主题（P16j）

- `settings.custom_themes`（JSON 列表）+ `active_custom_theme`。
- 导入：`POST /settings/theme/import` 粘贴 CSS → `_sanitize_theme_css()` 消毒后存为预设；apply/delete 路由切换与删除。
- `_load_custom_themes()` 合并进设置页预设列表。

### 3.3 日历着色子主题

5 种着色模式 `CALENDAR_PALETTES`（stage/source/ddl/payment/commission），每种模式的每个值一行 ColorPicker，落库为 `cal_{mode}_{label}` 键（实测 ~31 键）。事件着色优先级：订单 `custom_color` > settings 调色板 > 内置默认 > `#b0b0aa`。

### 3.4 外观

| 设置 | 键 | 可选值 |
|---|---|---|
| 字号 | `font_size` | 14/16/18/20px → `--font-size-base` |
| 字体 | `font_family` | system（Inter 栈）/ serif（Noto Serif SC、Source Han Serif、Georgia）/ mono（JetBrains Mono 栈）→ `--font-sans` |

---

## 四、布局系统

### 4.1 应用骨架

```
┌────────────┬──────────────────────────┐
│ Sidebar    │  Main Content            │
│ 240px/56px │  Page Header + 内容区      │
└────────────┴──────────────────────────┘
全局容器：#center-modal(680px) · #edit-drawer(480px) · #toast-container
```

- **Sidebar**：Notion 文件夹式导航（6 项 + 新建 CTA + 版本号）；折叠仅图标 + Tooltip；宽度经 `--sidebar-width` 与主区联动；状态存 localStorage。
- **Page Header**：一级页简化导航（P18-F6：去返回按钮/副标题，标题重设计）。
- **全局滚动条**（P14d）：圆角、去箭头、低对比、默认半透明悬停显现，全站统一。

### 4.2 响应式

- **断点（7）**：`max-width` 1200/1024/860/768/480 + `min-width` 1600/2000。
- **容器查询**：`.dashboard { container-type: inline-size; container-name: dashboard }`，统计卡与图表区在 ≤500/≤900 时重排。
- **画廊**：`.gallery-grid` CSS columns——≥1600px 5 列 / ≤1200px 3 列 / ≤768px 2 列级 / ≤480px 1 列。
- **动效可访问性**：两处 `prefers-reduced-motion: reduce` 降级。

---

## 五、组件规范

| 组件 | 要点 |
|---|---|
| Card | 去阴影、hairline 边框、大留白 |
| Stats 卡 | 5 指标（进行中/本月收入+环比/逾期/已完成/累计收入）；点击进入变色态并弹出**小票弹窗**（明细列表，CSS 三角底边，环比箭头红涨绿跌） |
| Table | Notion 数据库风；订单 12 列 + 复选；行点击进入；P13b 表头强化 |
| Stage Badge | 七色徽章（主色文字 + `*-bg` 底） |
| Status Badge | DDL/收款语义色 |
| Button | 圆角、细边框、hover/active 墨色透明度反馈（`--transition-fast`） |
| Form | 干净白底、细边框、ring 焦点；平台/直接双财务模式实时预览 |
| Detail Grid | Compact Codex-style 双栏（订单详情：基本+财务） |
| 居中模态 | 680px，Notion 风，滑入动画；新建成功走 modal_success 页 |
| 右抽屉 | 480px，日历/甘特/列表上下文编辑；内含 24px 圆形 ColorPicker（日历颜色覆盖） |
| Toast | 右上固定，0.25s 上滑入，2.5s 后淡出；success 绿/error 红/info 蓝 |
| Lightbox | 图片全屏查看（画廊/订单图） |
| ColorPicker | 原生零依赖（app.js IIFE ~230 行）：SB Canvas(216×140)+色相条(216×14)+hex 输入+清除；HSV↔Hex；fixed 智能定位（右溢出左对齐/下溢出上弹）；mouseup 才 onChange；0.15s ease-out 滑入 |
| 模块定制器（P16h） | 统计/看板模块卡片注入齿轮工具条，设置面板控显隐，prefs 存 localStorage |

---

## 六、视图规范

### 6.1 甘特图（主页，Frappe Gantt 0.6）

纯色条（隐藏进度条/手柄）；`custom_class` = stage-* + overdue；标签 14px/600 纯黑；日/周/月/年 4 视图 + 仅进行中/全部切换；拖拽改排期；点击开编辑模态；容器 max-height 400px；P15c 时间线当前位微调；弹窗动画替换 frappe 默认黑底。

### 6.2 日历（FullCalendar 6）

月/周视图，中文 locale；事件色按 §3.3 优先级；逾期红边+发光（eventDidMount）；未排期池左侧卡片池（Draggable 插件，P13b F1 卡片式筛选）；拖入/拖移/调时长均落库；点击开抽屉。

### 6.3 看板（SortableJS）

7 阶段列；列头 sticky（列内滚动）；列统计（卡数+金额合计）；卡片 hover 显示 ◀/▶/✓ 快捷切换；阶段色左边框 + 逾期红/临期橙标识；列头拖拽排序（临时视觉，刷新恢复）；P18-F5 内容筛选条；看板页固定高度保证 sticky 生效。

### 6.4 收入看板（Chart.js 4）

| 图表 | 类型 | 颜色 | 交互 |
|---|---|---|---|
| 月度收入 | Line 面积 | 蓝 #2a78d6 | 数据标签 k/w 格式化，点圆点出小票 |
| 每月预计 | Line 虚线面积 | 黄 #eda100 | borderDash [6,3]，点小票 |
| 年累进 | Line 面积 | 绿 #008300 | 逐月累加 |
| 品类分布 | Doughnut | 8 色调色板 | 百分比标注，悬停见毛/净分列 |
| 客户排名 | 表格 Top 8 | — | 点击进客户详情 |

年份切换（自动发现数据年份）；近 3/6/12 月范围切换。

### 6.5 画廊

瀑布流 columns 布局 + 无限滚动（`/api/gallery` offset/limit，IntersectionObserver 懒加载）；筛选（stage/source/customer/type）；点击 Lightbox；客户详情页内嵌作品墙。

### 6.6 页面过渡（P17c）

`.sidebar { view-transition-name }` 参与视图过渡，消除全站跳转黑屏/闪烁；配合轻量缓存（P16i）。

---

## 七、设计-代码映射

| 设计资产 | 落点 |
|---|---|
| Tokens 默认值 | `static/app.css` `:root` |
| Tokens 运行时覆盖 | `app._build_theme_css()` → base.html `<style>` |
| 阶段色/语义色 | CSS 变量 → `_build_theme_css` 映射 ← settings `stage_*`/`theme_*` |
| 日历调色板 | `db.CALENDAR_PALETTES` 默认 + settings `cal_*` 覆盖 |
| 组件样式分区 | app.css 注释分区（Sidebar/Cards/Stats/Table/Badges/Buttons/Forms/Detail Grid/Calendar/Kanban/Gantt/Gallery…） |
| 交互动效 | `static/app.js`（Toast/模态/抽屉/ColorPicker/模块定制器） |

**修改纪律**：改色先改 token（settings 或 `:root`），禁硬编码色值进组件；新组件复用 radius/space/shadow/transition 分档；阶段相关一律走 `--stage-*` 变量，保证主题可覆盖。
