# oimimo 画师排单助手

面向**独立插画师**的本地接单排单管理工具，覆盖完整工作流：

```
接单 → 排期 → 跟进 → 收款 → 统计 → 归档
```

数据全部保存在本地 SQLite 单文件中，无需联网、无账号注册、不上传任何数据。

## 功能一览

- **主页仪表盘**：统计卡（进行中 / 本月收入 / 逾期 / 已完成 / 累计收入）、甘特图排期、本周排单表、逾期提醒
- **日历视图**：FullCalendar 月/周视图，未排期池拖拽入历，5 种着色模式独立调色
- **排单列表**：多维筛选 + 分页 + 批量操作 + Markdown 导出
- **看板**：7 阶段列拖拽流转
- **收入看板**：月度收入 / 预计收入 / 年累进 / 品类分布图表，客户消费排名
- **画廊**：作品瀑布流，订单多图上传（粘贴/选择），Lightbox 查看
- **客户管理**：消费统计、标签、历史订单与作品
- **订单模板**：常用约稿类型一键建单
- **主题设置**：18 色全局主题 + 日历调色板，均可自定义

## 安装使用（普通用户）

前往 [Releases](../../releases) 页面下载：

| 文件 | 说明 |
|---|---|
| `oimimo-setup.exe` | 安装版：双击安装，自动创建开始菜单和桌面快捷方式 |
| `oimimo-portable-win64.zip` | 便携版：解压到任意文件夹，双击 `oimimo.exe` 运行 |

启动后程序会自动打开浏览器（本地地址），所有数据保存在程序目录下的 `orders.db`。

> **关于 Windows SmartScreen 提示**
> 首次运行时 Windows 可能弹出"Windows 已保护你的电脑 / 未知发布者"提示。这是因为本软件是个人开发者发布、未购买代码签名证书（签名证书每年需数百至上千元费用），并非软件有问题。
> 处理方法：点击提示中的 **"更多信息"**，再点击 **"仍要运行"** 即可。软件完全本地运行，源代码公开在本仓库，可自行审阅。

### 数据位置与备份

- 安装版：数据库位于 `%LOCALAPPDATA%\oimimo\orders.db`
- 便携版：数据库位于解压目录下的 `orders.db`
- 备份只需复制 `orders.db` 文件（以及 `uploads/` 文件夹，如果你上传过作品图）
- 覆盖安装新版本不会丢失数据

## 从源码运行（开发者）

需要 Python 3.9+：

```bash
pip install -r requirements.txt
python launcher.py        # 启动器模式（推荐，含托盘图标）
# 或
python app.py             # 直接运行 Flask（调试用）
```

启动后访问终端输出的本地地址。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python Flask 3 + Pydantic v2 |
| 数据库 | SQLite（WAL 模式，单文件零配置） |
| 前端 | Jinja2 模板 + HTMX 2（无构建步骤，无 Node 依赖） |
| 可视化 | FullCalendar 6 / Chart.js 4 / Frappe Gantt（全部本地 vendor，离线可用） |
| 打包 | PyInstaller（onedir）+ Inno Setup 6 |

## 文档

- [AGENTS.md](AGENTS.md) — 给 AI 编码助手 / 开发者的项目地图（模块职责、约定、验证方式）
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 架构：路由全表、DB schema、核心机制
- [docs/DESIGN.md](docs/DESIGN.md) — 设计系统：Design Tokens、主题、组件规范
- [docs/PROJECT.md](docs/PROJECT.md) — 项目说明书：功能矩阵、快速开始
- [CHANGELOG.md](CHANGELOG.md) — 版本更新记录

## 许可证

[MIT](LICENSE)
