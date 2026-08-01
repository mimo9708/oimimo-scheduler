"""oimimo scheduler — 一键启动器（精致明信片窗口，无控制台）

Flask 在后台守护线程运行；前台显示一张「明信片」窗口
（logo + 运行状态 + URL + 停止/重启/日志/缓存按钮）。关闭窗口即停止服务。
"""
import subprocess, sys, os, time, threading, webbrowser, shutil, logging
from datetime import datetime

ROOT = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))


def resource_path(rel):
    """打包后从 _MEIPASS 读只读资源；开发时从 ROOT 读。"""
    base = getattr(sys, '_MEIPASS', None) or ROOT
    return os.path.join(base, rel)


def check_python():
    v = sys.version_info
    if v < (3, 9):
        print("需要 Python 3.9+, 当前:", sys.version)
        sys.exit(1)


def install_deps():
    try:
        import flask, pydantic, flask_cors
    except ImportError:
        print("安装依赖...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r",
            os.path.join(ROOT, "requirements.txt"), "-q"])
        print("依赖安装完成\n")


def init_db():
    sys.path.insert(0, ROOT)
    import db
    db.init_db()


def _start_server(port, state):
    os.chdir(ROOT)
    import app
    try:
        app.app.run(debug=False, host="127.0.0.1", port=port)
    except Exception as e:
        state['error'] = str(e)


def _port_ok(url):
    import urllib.request
    try:
        urllib.request.urlopen(url, timeout=2)
        return True
    except Exception:
        return False


def _get_console_hwnd():
    """返回当前进程控制台窗口句柄；无控制台或非 Windows 返回 0。"""
    if os.name != 'nt':
        return 0
    try:
        import ctypes
        return ctypes.windll.kernel32.GetConsoleWindow()
    except Exception:
        return 0


def _set_console_visible(visible):
    """显示/隐藏控制台日志窗口（Windows）。无控制台时静默跳过。"""
    if os.name != 'nt':
        return
    hwnd = _get_console_hwnd()
    if not hwnd:
        return
    try:
        import ctypes
        ctypes.windll.user32.ShowWindow(hwnd, 5 if visible else 0)  # SW_SHOW=5 / SW_HIDE=0
    except Exception:
        pass


def _is_own_console():
    """当前控制台是否为本进程专属（双击/run.bat 启动）；
    从终端交互启动时控制台被 cmd/pwsh 等共享，返回 False（避免误隐藏用户终端）。
    非 Windows 或无控制台返回 False。"""
    if os.name != 'nt':
        return False
    try:
        import ctypes
        buf = (ctypes.c_ulong * 16)()
        n = ctypes.windll.kernel32.GetConsoleProcessList(buf, 16)
        return n == 1
    except Exception:
        return False


def _init_file_logging():
    """无条件初始化 logs/ 目录 + root logger 文件输出（启动时调用，幂等）。

    修复「日志文件夹每次打开都不存在」：旧逻辑仅打包 exe 模式才创建 logs/，
    开发模式永远没有日志文件。现在两种模式都在启动时创建目录并按天追加日志。
    """
    log_dir = os.path.join(ROOT, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"app_{datetime.now().strftime('%Y%m%d')}.log")
    root_logger = logging.getLogger()
    for h in root_logger.handlers:
        if isinstance(h, logging.FileHandler) and getattr(h, 'baseFilename', '') == os.path.abspath(log_path):
            return  # 已挂载，重复调用幂等
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
    root_logger.addHandler(fh)
    if root_logger.level == logging.NOTSET or root_logger.level > logging.INFO:
        root_logger.setLevel(logging.INFO)
    logging.info('oimimo scheduler 启动 (pid=%s)', os.getpid())


def show_postcard(url, state):
    import tkinter as tk
    import tkinter.messagebox as tkmb

    # F2：系统托盘依赖（缺失则降级为原行为：窗口正常显示，关闭即退出）
    try:
        import pystray
        from PIL import Image as _PILImage
        _tray_ok = True
    except Exception:
        pystray = None
        _PILImage = None
        _tray_ok = False
        print('[启动器] 未安装 pystray/Pillow，降级为普通窗口模式（关闭即退出）', flush=True)
    _tray = {'icon': None}

    root = tk.Tk()
    root.title('oimimo scheduler')
    w, h = 340, 440
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry(f'{w}x{h}+{x}+{y}')
    root.resizable(False, False)
    root.configure(bg='#fcfcfb')
    try:
        root.iconbitmap(resource_path('static/logo.ico'))
    except Exception:
        pass

    # logo（缩小到约 56px）
    try:
        img = tk.PhotoImage(file=resource_path('static/logo.png'))
        f = max(1, img.width() // 56)
        if f > 1:
            img = img.subsample(f, f)
        tk.Label(root, image=img, bg='#fcfcfb').pack(pady=(16, 2))
    except Exception:
        pass

    tk.Label(root, text='oimimo scheduler', font=('Microsoft YaHei', 14, 'bold'),
             bg='#fcfcfb', fg='#0b0b0b').pack()
    status = tk.Label(root, text='启动中…', font=('Microsoft YaHei', 9),
                      bg='#fcfcfb', fg='#52514e')
    status.pack()
    tk.Label(root, text=url, font=('Microsoft YaHei', 9),
             bg='#fcfcfb', fg='#2a78d6').pack(pady=(2, 4))

    # ── 状态 & 线程引用（闭包捕获，供重启使用）──
    port = state.get('port', 5001)
    server_thread = state.get('thread', None)

    def restart_server():
        """重启应用：启动新进程后退出当前进程"""
        if not tkmb.askyesno('确认重启', '确定重启应用？\n当前服务将停止，浏览器连接会中断。'):
            return

        # 获取当前可执行文件路径
        if getattr(sys, 'frozen', False):
            # 打包后的 exe
            exe_path = sys.executable
            cmd = [exe_path]
            # 如果有 --port 参数，传递给新进程
            if port != 5001:
                cmd.append(str(port))
        else:
            # 开发模式
            script_path = os.path.join(ROOT, 'launcher.py')
            cmd = [sys.executable, script_path]
            if port != 5001:
                cmd.append(str(port))

        try:
            # 启动新进程（完全独立，不继承当前进程）
            # 默认不弹控制台（与“默认隐藏日志窗口”一致）；日志可在面板里找回
            subprocess.Popen(
                cmd,
                cwd=ROOT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                close_fds=True,
            )
        except Exception:
            # 降级：不带 CREATE_NEW_CONSOLE
            subprocess.Popen(cmd, cwd=ROOT, close_fds=True)

        # 停止当前窗口（含托盘）
        ic = _tray.get('icon')
        if ic:
            try:
                ic.stop()
            except Exception:
                pass
        root.destroy()
        sys.exit(0)

    def view_logs():
        """打开日志文件夹"""
        log_dir = os.path.join(ROOT, 'logs')
        if os.path.isdir(log_dir):
            try:
                os.startfile(log_dir)
            except Exception:
                tkmb.showinfo('日志', f'日志文件夹：\n{log_dir}')
        else:
            tkmb.showinfo('日志', '暂无日志文件。\n日志会在首次运行时自动创建于 logs/ 目录。')

    def clear_cache():
        """清除缓存（__pycache__ 目录）"""
        if not tkmb.askyesno('确认', '确定清除缓存？\n将删除 __pycache__ 编译缓存（开发者调试用，普通用户无需操作）。\n数据库文件 orders.db 不受影响。'):
            return

        cleaned = []
        # 清理 __pycache__
        for dirpath, dirnames, filenames in os.walk(ROOT):
            if '__pycache__' in dirnames:
                p = os.path.join(dirpath, '__pycache__')
                try:
                    shutil.rmtree(p)
                    cleaned.append(p)
                except Exception:
                    pass

        if cleaned:
            tkmb.showinfo('完成', f'已清除 {len(cleaned)} 个缓存目录。\n\n建议重启应用以完成清理。')
        else:
            tkmb.showinfo('完成', '没有需要清理的缓存。')

    # ── 日志窗口显隐切换 ──
    def toggle_log_window():
        # 无控制台（--windowed 打包）→ 引导到日志文件夹
        if os.name == 'nt' and not _get_console_hwnd():
            tkmb.showinfo('日志', '当前为无控制台模式，实时日志窗口不可用。\n可点「📁 日志文件夹」查看历史日志。')
            return
        visible = not state.get('log_visible', False)
        _set_console_visible(visible)
        state['log_visible'] = visible
        log_btn.config(text='🖥 隐藏日志' if visible else '🖥 显示日志')

    def open_web():
        webbrowser.open(url)

    # ── 主操作：打开主界面（强调色，服务就绪后启用）──
    open_btn = tk.Button(root, text='🌐 打开主界面', font=('Microsoft YaHei', 10, 'bold'),
                         bg='#d6e6fb', fg='#1a5fb4',
                         activebackground='#c4d9f5',
                         relief='flat', padx=24, pady=6, cursor='hand2',
                         state=tk.DISABLED, command=open_web)
    open_btn.pack(pady=(10, 4))
    state['open_btn'] = open_btn

    btn_style = {
        'font': ('Microsoft YaHei', 9),
        'bg': '#f0eeea', 'fg': '#0b0b0b',
        'activebackground': '#e4e1db',
        'relief': 'flat', 'padx': 8, 'pady': 4,
        'cursor': 'hand2', 'width': 11,
    }

    # 次操作行 1：重启 + 日志显隐
    row1 = tk.Frame(root, bg='#fcfcfb')
    row1.pack(pady=(2, 2))
    tk.Button(row1, text='🔄 重启服务', command=restart_server, **btn_style).pack(side=tk.LEFT, padx=3)
    log_btn = tk.Button(row1, text='🖥 显示日志', command=toggle_log_window, **btn_style)
    log_btn.pack(side=tk.LEFT, padx=3)

    # 次操作行 2：日志文件夹 + 清除缓存
    row2 = tk.Frame(root, bg='#fcfcfb')
    row2.pack(pady=(0, 4))
    tk.Button(row2, text='📁 日志文件夹', command=view_logs, **btn_style).pack(side=tk.LEFT, padx=3)
    tk.Button(row2, text='🗑️ 清除缓存', command=clear_cache, **btn_style).pack(side=tk.LEFT, padx=3)

    # 停止按钮（红色高亮）
    # ── F2 托盘集成：真正退出 / 窗口显隐 ──
    def _real_quit():
        ic = _tray.get('icon')
        if ic:
            try:
                ic.stop()
            except Exception:
                pass
        try:
            root.destroy()
        except Exception:
            pass
        sys.exit(0)

    def show_window():
        root.deiconify()
        root.lift()
        root.attributes('-topmost', True)
        root.after(300, lambda: root.attributes('-topmost', False))

    def hide_to_tray():
        root.withdraw()

    tk.Button(root, text='⏹ 停止服务', font=('Microsoft YaHei', 10),
              bg='#e8d4d4', fg='#d03b3b',
              activebackground='#dcc8c8',
              relief='flat', padx=24, pady=6, cursor='hand2',
              command=_real_quit).pack(pady=(6, 8))

    # 有托盘：关闭/最小化 → 收回托盘，进程不退出；无托盘：关闭即退出
    if _tray_ok:
        root.protocol('WM_DELETE_WINDOW', hide_to_tray)
    else:
        root.protocol('WM_DELETE_WINDOW', _real_quit)

    def poll():
        if state.get('error'):
            status.config(text='启动失败：端口可能被占用', fg='#d03b3b')
            return
        if _port_ok(url):
            status.config(text='本地服务运行中', fg='#0ca30c')
            ob = state.get('open_btn')
            if ob:
                ob.config(state=tk.NORMAL)
            if not state.get('opened'):
                state['opened'] = True
                webbrowser.open(url)
            return
        root.after(500, poll)

    # ── F2：启动托盘线程 + 默认最小化到托盘 ──
    if _tray_ok:
        def _run_tray():
            try:
                image = _PILImage.open(resource_path('static/logo.png'))
            except Exception:
                image = _PILImage.new('RGB', (64, 64), (42, 120, 214))
            menu = pystray.Menu(
                pystray.MenuItem('打开主窗口', lambda icon, item: root.after(0, show_window), default=True),
                pystray.MenuItem('打开网页', lambda icon, item: webbrowser.open(url)),
                pystray.MenuItem('退出', lambda icon, item: root.after(0, _real_quit)),
            )
            icon = pystray.Icon('oimimo', image, 'oimimo scheduler', menu)
            _tray['icon'] = icon
            icon.run()
        threading.Thread(target=_run_tray, daemon=True).start()
        root.withdraw()  # 默认隐藏到托盘，仅托盘图标存在

    root.after(800, poll)
    root.mainloop()


def main():
    # 任何模式都先建好 logs/ 并挂文件日志（修复「日志文件夹不存在」）
    _init_file_logging()
    # --windowed 打包时 stdout/stderr 为 None，重定向到日志文件
    if getattr(sys, 'frozen', False) and sys.stderr is None:
        log_dir = os.path.join(ROOT, 'logs')
        os.makedirs(log_dir, exist_ok=True)
        log_name = f"app_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        log_path = os.path.join(log_dir, log_name)
        sys.stdout = open(log_path, 'w', encoding='utf-8')
        sys.stderr = open(log_path, 'w', encoding='utf-8')
        print(f"[{datetime.now()}] oimimo scheduler 启动", flush=True)

    if not getattr(sys, 'frozen', False):
        check_python()
        print("=" * 50)
        print("  oimimo scheduler")
        print("=" * 50)
        install_deps()

    init_db()

    port = 5001
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except Exception:
            pass

    url = f"http://127.0.0.1:{port}"
    state = {'port': port}

    server_thread = threading.Thread(target=_start_server, args=(port, state), daemon=True)
    server_thread.start()
    state['thread'] = server_thread

    # 默认隐藏控制台日志窗口（可在面板中「显示日志」找回）：
    # 仅当控制台为本进程专属（双击/run.bat 启动）时隐藏；
    # 从终端交互启动（控制台被 cmd/pwsh 共享）则不动，避免连累用户终端
    state['log_visible'] = False
    if _is_own_console():
        _set_console_visible(False)

    show_postcard(url, state)


if __name__ == '__main__':
    main()
