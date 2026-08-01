/**
 * oimimo scheduler — 前端交互逻辑 (app.js)
 * 日历/甘特图初始化在各页面内联处理
 */

/* ═══════════════════════════════════════════════════════════
   抽屉 & 模态框
   ═══════════════════════════════════════════════════════════ */

// ── 居中模态框（Notion 风格新建）──
function openCenterModal(url, title) {
    document.getElementById('center-modal').classList.remove('hidden');
    document.getElementById('center-modal').querySelector('.modal-header h3').textContent = title || '新建订单';
    var actEl = document.getElementById('center-modal-actions'); if (actEl) actEl.innerHTML = '';
    document.getElementById('center-modal-body').innerHTML = '<div class="empty-state"><div class="empty-state-text">加载中...</div></div>';
    htmx.ajax('GET', url || '/orders/new?modal=1', { target: '#center-modal-body' });
}

function closeCenterModal() {
    document.getElementById('center-modal').classList.add('hidden');
    var actEl = document.getElementById('center-modal-actions'); if (actEl) actEl.innerHTML = '';
    // 刷新日历
    if (window.calendar) window.calendar.refetchEvents();
    // 刷新未排期池（#40 P2：仅日历页存在该容器，其余页面跳过，避免 htmx.ajax 拒绝产生 unhandled promise 噪音）
    if (document.getElementById('unscheduled-pool')) {
        htmx.ajax('GET', '/api/orders/unscheduled', { target: '#unscheduled-pool' });
    }
    // 刷新甘特图（按当前视图 + 「仅进行中/显示全部」筛选重建，避免忽略筛选）
    if (typeof window.refreshGanttData === 'function') {
        window.refreshGanttData();
    } else if (window._gantt) {
        fetch('/api/orders/gantt-data')
            .then(r => r.json())
            .then(data => {
                if (window._gantt && window._gantt.refresh) {
                    window._gantt.refresh(data);
                }
            });
    }
    // 主页：刷新本周排单/逾期表格，保证新建/编辑后立即反映
    if (window.location.pathname === '/' && typeof window.refreshDashboardTables === 'function') {
        window.refreshDashboardTables();
    }
    // 刷新统计卡片（保留用户选择的时间范围）
    refreshStatsPreservingRange();
    // P20d：补录工时弹窗关闭后，接续被暂存的归档确认（串行不并发）
    window.__workHoursPromptActive = false;
    if (window.__pendingArchiveConfirm) {
        var _pid = window.__pendingArchiveConfirm;
        window.__pendingArchiveConfirm = null;
        setTimeout(function() { archiveOrder(_pid, false); }, 300);
    }
}

// Escape 关闭
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        var m = document.getElementById('center-modal');
        if (m && !m.classList.contains('hidden')) closeCenterModal();
    }
});

/* ═══════════════════════════════════════════════════════════
   P18-F4.4：全局快捷键统一分发器
   功能集固定（见后端 DEFAULT_SHORTCUTS），按键由 window.__SHORTCUTS 提供（用户可自定义）。
   支持组合键（Ctrl+N / Escape）与 g 前缀序列（g h）。既有各浮层 Escape 关闭逻辑保留。
   ═══════════════════════════════════════════════════════════ */
(function () {
    var SC = window.__SHORTCUTS || {};
    var NAV_URLS = {
        nav_home: '/', nav_income: '/income', nav_calendar: '/calendar',
        nav_orders: '/orders', nav_kanban: '/orders/kanban',
        nav_customers: '/customers', nav_gallery: '/gallery', nav_settings: '/settings'
    };

    // 构建：组合键映射 combo->func；序列映射 "g x"->func
    var comboMap = {}, seqMap = {};
    Object.keys(SC).forEach(function (func) {
        var v = String(SC[func] || '').trim().toLowerCase().replace(/\s+/g, ' ');
        if (!v) return;
        if (v.indexOf(' ') !== -1) seqMap[v] = func;
        else comboMap[v] = func;
    });

    function eventCombo(e) {
        var parts = [];
        if (e.ctrlKey) parts.push('ctrl');
        if (e.altKey) parts.push('alt');
        if (e.shiftKey) parts.push('shift');
        if (e.metaKey) parts.push('meta');
        var k = e.key === ' ' ? 'space' : String(e.key).toLowerCase();
        parts.push(k);
        return parts.join('+');
    }

    function inEditable(e) {
        var t = e.target;
        if (!t) return false;
        var tag = (t.tagName || '').toLowerCase();
        return tag === 'input' || tag === 'textarea' || tag === 'select' || t.isContentEditable;
    }

    function closeAnyOverlay() {
        var cm = document.getElementById('center-modal');
        if (cm && !cm.classList.contains('hidden')) { if (typeof closeCenterModal === 'function') closeCenterModal(); return; }
        var dr = document.getElementById('edit-drawer');
        if (dr && !dr.classList.contains('hidden')) { if (typeof closeDrawer === 'function') closeDrawer(); return; }
        var rc = document.getElementById('receipt-modal');
        if (rc && !rc.classList.contains('hidden')) { if (typeof closeReceipt === 'function') closeReceipt(); return; }
    }

    function runAction(func) {
        switch (func) {
            case 'new_order':
                if (typeof openCenterModal === 'function') openCenterModal();
                break;
            case 'focus_search':
                var s = document.querySelector('input[type="search"], input[name="search"]');
                if (s) s.focus();
                break;
            case 'toggle_sidebar':
                if (typeof toggleSidebar === 'function') toggleSidebar();
                break;
            case 'save_settings':
                // #48：仅设置页存在 #settings-form 时生效；用 requestSubmit 触发 submit 事件（快捷键序列化依赖它）
                var sf = document.getElementById('settings-form');
                if (sf) { sf.requestSubmit ? sf.requestSubmit() : sf.submit(); }
                break;
            default:
                if (NAV_URLS[func]) window.location.href = NAV_URLS[func];
        }
    }

    var pendingPrefix = null, pendingAt = 0;

    document.addEventListener('keydown', function (e) {
        var combo = eventCombo(e);
        // close/关闭浮层：即使在输入框也允许（既有各浮层 Escape 处理器仍会各自兜底）
        if (comboMap[combo] === 'close') { closeAnyOverlay(); return; }
        // #48：保存全部（Ctrl+S）在输入框内也允许触发（设置页/编辑订单页多为输入框聚集态），并拦截浏览器默认保存
        if (comboMap[combo] === 'save_settings') {
            if (document.getElementById('settings-form')) { e.preventDefault(); runAction('save_settings'); return; }
            // 订单编辑表单（全页 / 模态框 / 抽屉）：Ctrl+S 提交表单
            var orderForm = document.getElementById('order-form') || document.getElementById('order-form-modal');
            if (!orderForm) {
                // 抽屉可能通过 HTMX 加载，id 可能不同；兜底找 drawer 内的表单
                var drawer = document.getElementById('drawer-body');
                if (drawer) orderForm = drawer.querySelector('form[method="POST"]');
            }
            if (orderForm) { e.preventDefault(); orderForm.requestSubmit ? orderForm.requestSubmit() : orderForm.submit(); }
            return;
        }
        if (inEditable(e)) return;

        // g 前缀序列的第二键
        if (pendingPrefix) {
            var seqKey = pendingPrefix + ' ' + String(e.key).toLowerCase();
            pendingPrefix = null;
            if (seqMap[seqKey]) { e.preventDefault(); runAction(seqMap[seqKey]); return; }
        }
        // 若此键是某序列的前缀（无修饰键时），进入等待态
        var lower = String(e.key).toLowerCase();
        var isPrefix = !e.ctrlKey && !e.altKey && !e.metaKey &&
            Object.keys(seqMap).some(function (s) { return s.split(' ')[0] === lower; });
        if (isPrefix) {
            pendingPrefix = lower;
            pendingAt = Date.now();
            setTimeout(function () { if (Date.now() - pendingAt >= 700) pendingPrefix = null; }, 800);
            return;
        }
        // 组合键
        var func = comboMap[combo];
        if (func && func !== 'close') { e.preventDefault(); runAction(func); }
    });
})();

// ── 归档入口（P15b：走居中模态框，后端主导时间判断/确认弹窗）──
function archiveOrder(orderId, isArchived) {
    document.getElementById('center-modal').classList.remove('hidden');
    document.getElementById('center-modal').querySelector('.modal-header h3').textContent = isArchived ? '取消归档' : '归档订单';
    document.getElementById('center-modal-body').innerHTML = '<div class="empty-state"><div class="empty-state-text">处理中...</div></div>';
    htmx.ajax('POST', '/orders/' + orderId + '/archive', { target: '#center-modal-body' });
}

// ── 居中模态框 — 编辑已有订单 ──
function openOrderModal(orderId) {
    document.getElementById('center-modal').classList.remove('hidden');
    document.getElementById('center-modal').querySelector('.modal-header h3').textContent = '编辑订单';
    document.getElementById('center-modal-body').innerHTML = '<div class="empty-state"><div class="empty-state-text">加载中...</div></div>';
    htmx.ajax('GET', '/orders/' + orderId + '/edit?modal=1', { target: '#center-modal-body' });
}

function openEditDrawer(orderId) {
    const drawer = document.getElementById('edit-drawer');
    const body = document.getElementById('drawer-body');
    drawer.classList.remove('hidden');
    body.innerHTML = '<div class="empty-state"><div class="empty-state-icon">⏳</div><div class="empty-state-text">加载中...</div></div>';
    htmx.ajax('GET', `/orders/${orderId}/edit?inline=1`, { target: '#drawer-body' });
}

function closeDrawer() {
    document.getElementById('edit-drawer').classList.add('hidden');
    // P19-F11 X1：编辑保存后按当前页面分发刷新（不再是统一只刷日历+统计卡）
    refreshCurrentView();
}

// P19-F11 X1：按当前页面分发刷新——列表页 refreshTable()、看板页重载看板区、
// 详情页重载内容区、主页/日历维持现有日历+统计卡刷新
function refreshCurrentView() {
    var path = window.location.pathname;
    if (path === '/orders' && typeof refreshTable === 'function') {
        refreshTable();  // 列表页（refreshTable 定义于 orders/list.html）
        return;
    }
    if (path === '/orders/kanban') {
        reloadKanbanBoard();  // 看板页 partial 重载
        return;
    }
    if (/^\/orders\/\d+$/.test(path)) {
        reloadDetailContent();  // 详情页内容区重载（不整页 reload，保留归档确认链）
        return;
    }
    // 主页/日历等：维持现有行为
    if (window.calendar) window.calendar.refetchEvents();
    // 主页：编辑保存后实时刷新甘特图 + 本周排单/逾期表格（不再需手动刷新整页）
    if (path === '/' && typeof window.refreshDashboardData === 'function') {
        window.refreshDashboardData();
    }
    refreshStatsPreservingRange();
}

// P19-F11 X1：看板区重载——整页 GET 提取 #kanban-board 替换，重建拖拽/统计/筛选
function reloadKanbanBoard() {
    var board = document.getElementById('kanban-board');
    if (!board) { window.location.reload(); return; }
    fetch('/orders/kanban')
        .then(function(r) { return r.text(); })
        .then(function(html) {
            var fresh = new DOMParser().parseFromString(html, 'text/html').getElementById('kanban-board');
            if (!fresh) { window.location.reload(); return; }
            board.innerHTML = fresh.innerHTML;
            if (typeof initKanban === 'function') initKanban();
            if (typeof updateKanbanStats === 'function') updateKanbanStats();
            if (typeof applyKanbanFilters === 'function') applyKanbanFilters();
        })
        .catch(function() { window.location.reload(); });
}

// P19-F11 X1：详情页内容区重载——提取 #main-content 替换（innerHTML 不卸载页面，
// orderUpdated 的 archiveConfirm 300ms 定时链不受打扰）
function reloadDetailContent() {
    var main = document.getElementById('main-content');
    if (!main) { window.location.reload(); return; }
    fetch(window.location.pathname)
        .then(function(r) { return r.text(); })
        .then(function(html) {
            var fresh = new DOMParser().parseFromString(html, 'text/html').getElementById('main-content');
            if (!fresh) { window.location.reload(); return; }
            main.innerHTML = fresh.innerHTML;
            if (window.lucide) lucide.createIcons();
        })
        .catch(function() { window.location.reload(); });
}

// 关闭抽屉的键盘快捷键
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        const drawer = document.getElementById('edit-drawer');
        if (drawer && !drawer.classList.contains('hidden')) {
            closeDrawer();
        }
    }
});


/* ═══════════════════════════════════════════════════════════
   统计明细小票弹窗（超市小票风格）
   ═══════════════════════════════════════════════════════════ */

// 事件委托：点击任意可点击统计卡打开小票
document.addEventListener('click', function(e) {
    var card = e.target.closest('.stat-card-clickable');
    if (!card) return;
    openReceipt(card.getAttribute('data-metric'), card.getAttribute('data-label'), card);
});

function openReceipt(metric, label, cardEl, extraParams) {
    var modal = document.getElementById('receipt-modal');
    if (!modal || !metric) return;

    var params = 'metric=' + encodeURIComponent(metric);
    var subtitle = '';

    // 主页统计卡：附带当前统计范围
    var grid = document.getElementById('stats-grid');
    if (grid && cardEl && grid.contains(cardEl)) {
        var from = grid.getAttribute('data-range-from');
        var to = grid.getAttribute('data-range-to');
        if (from) params += '&from=' + encodeURIComponent(from);
        if (to) params += '&to=' + encodeURIComponent(to);
        subtitle = grid.getAttribute('data-range-label') + (from ? ' · ' + from + ' → ' + to : ' · 全部时间');
    }
    // 收入看板卡片：附带年份
    if (cardEl && cardEl.getAttribute('data-year')) {
        params += '&year=' + encodeURIComponent(cardEl.getAttribute('data-year'));
        subtitle = cardEl.getAttribute('data-year') + ' 年';
    }
    // 额外参数（图表点击时传入 year/month；P20e 时薪卡传入 from/to 供 income 口径）
    if (extraParams) {
        if (extraParams.year) { params += '&year=' + encodeURIComponent(extraParams.year); subtitle = extraParams.year + ' 年'; }
        if (extraParams.month) { params += '&month=' + encodeURIComponent(extraParams.month); subtitle += ' ' + extraParams.month + '月'; }
        if (extraParams.from) { params += '&from=' + encodeURIComponent(extraParams.from); }
        if (extraParams.to) { params += '&to=' + encodeURIComponent(extraParams.to); }
    }

    document.getElementById('receipt-title').textContent = label || '明细';
    document.getElementById('receipt-subtitle').textContent = subtitle;
    var body = document.getElementById('receipt-body');
    body.innerHTML = '<div class="receipt-empty">加载中…</div>';
    document.getElementById('receipt-total').textContent = '';
    // P20-F16：清空旧条码
    document.getElementById('receipt-barcode').innerHTML = '';
    modal.classList.remove('hidden');

    fetch('/api/stats/detail?' + params)
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (!d.items || !d.items.length) {
                body.innerHTML = '<div class="receipt-empty">暂无订单</div>';
                _renderReceiptBarcode();
                return;
            }
            body.innerHTML = d.items.map(function(it) {
                return '<a class="receipt-row" href="/orders/' + it.id + '">'
                    + '<span class="r-date">' + escHtml(it.date || '-') + '</span>'
                    + '<span class="r-name">' + escHtml(it.project_name) + '</span>'
                    + '<span class="r-amount">¥' + Number(it.amount).toLocaleString('zh-CN', { maximumFractionDigits: 0 }) + '</span>'
                    + '</a>';
            }).join('');
            document.getElementById('receipt-total').innerHTML =
                '<span>共 ' + d.count + ' 单</span><span>合计 ¥' + Number(d.total).toLocaleString('zh-CN', { maximumFractionDigits: 0 }) + '</span>';
            // P20-F16：渲染底部条码（小票创建时间编码）
            _renderReceiptBarcode();
        })
        .catch(function() {
            body.innerHTML = '<div class="receipt-empty">加载失败</div>';
        });
}

// P20-F16：底部条码渲染（CODE128 格式，当前时间戳编码）
function _renderReceiptBarcode() {
    var el = document.getElementById('receipt-barcode');
    if (!el || typeof JsBarcode === 'undefined') return;
    var now = new Date();
    var pad = function(n) { return n < 10 ? '0' + n : '' + n; };
    var code = 'R-' + now.getFullYear() + pad(now.getMonth()+1) + pad(now.getDate())
             + pad(now.getHours()) + pad(now.getMinutes()) + pad(now.getSeconds());
    try {
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        el.innerHTML = '';
        el.appendChild(svg);
        JsBarcode(svg, code, {
            format: 'CODE128', width: 1.5, height: 40,
            displayValue: true, fontSize: 10,
            margin: 4, background: 'transparent',
            lineColor: '#222'
        });
    } catch(e) { /* JsBarcode 失败不影响小票显示 */ }
}

function closeReceipt() {
    var modal = document.getElementById('receipt-modal');
    if (modal) modal.classList.add('hidden');
}

// Escape 关闭小票
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeReceipt();
});


/* ═══════════════════════════════════════════════════════════
   看板初始化 (SortableJS)
   ═══════════════════════════════════════════════════════════ */

function initKanban() {
    const columns = document.querySelectorAll('.kanban-column');
    columns.forEach(col => {
        Sortable.create(col, {
            group: 'kanban',
            animation: 200,
            draggable: '.kanban-card',  // 仅订单卡片可拖拽；列头（.kanban-column-header）固定不可拖
            ghostClass: 'kanban-ghost',
            dragClass: 'kanban-drag',
            onEnd: function(evt) {
                const orderId = evt.item.getAttribute('data-order-id');
                const newStage = evt.to.getAttribute('data-stage');

                if (!orderId || !newStage) return;

                dirtyFetch(`/orders/${orderId}/stage`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: `stage=${encodeURIComponent(newStage)}`
                }).then(res => {
                    // Spec12：400 = 阶段不在快照内，显示 Toast 并恢复原位
                    if (!res.ok) {
                        res.text().then(msg => {
                            showToast(msg || '该阶段不在此订单流程中', 'error');
                            _revertKanbanDrag(evt);
                        });
                        return;
                    }
                    handleWorkHoursTrigger(res);
                    return res.text().then(html => {
                        evt.item.outerHTML = html;
                        updateKanbanStats();
                        showToast('阶段已更新', 'success');
                    });
                }).catch(() => {
                    showToast('更新失败，请重试', 'error');
                    _revertKanbanDrag(evt);
                });
            }
        });
    });
}

// Spec12：看板拖拽失败恢复原位
function _revertKanbanDrag(evt) {
    if (evt.from && evt.item) {
        var ref = evt.from.children[evt.oldIndex] || null;
        evt.from.insertBefore(evt.item, ref);
    }
    updateKanbanStats();
}

function updateKanbanStats() {
    document.querySelectorAll('.kanban-column').forEach(col => {
        const stage = col.getAttribute('data-stage');
        const cards = col.querySelectorAll('.kanban-card');
        const countEl = col.querySelector('.kanban-column-count');
        const totalEl = col.querySelector('.kanban-column-total');

        if (countEl) countEl.textContent = cards.length;

        if (totalEl) {
            let total = 0;
            cards.forEach(card => {
                const amt = parseFloat(card.getAttribute('data-income') || '0');
                total += amt;
            });
            totalEl.textContent = total > 0 ? `¥${total.toLocaleString('zh-CN', {minimumFractionDigits: 0, maximumFractionDigits: 0})}` : '';
        }
        // 空列自动隐藏
        col.style.display = cards.length === 0 ? 'none' : '';
    });
}


/* ═══════════════════════════════════════════════════════════
   Toast 通知
   ═══════════════════════════════════════════════════════════ */

function showToast(message, type) {
    type = type || 'info';
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
}


/* ═══════════════════════════════════════════════════════════
   订单导出（Markdown → 本地文件）
   ═══════════════════════════════════════════════════════════ */

function exportOrders() {
    showToast('正在导出…', 'info');
    fetch('/export/orders', { method: 'POST' })
        .then(r => r.json())
        .then(d => {
            if (d.success) showToast('已导出 ' + d.count + ' 单 → exports/全部订单.md', 'success');
            else showToast('导出失败', 'error');
        })
        .catch(() => showToast('导出失败', 'error'));
}

function openExportFolder() {
    fetch('/export/open-folder')
        .then(r => r.json())
        .then(d => { if (d.success) showToast('已打开导出文件夹', 'success'); else showToast('打开文件夹失败', 'error'); })
        .catch(() => showToast('打开文件夹失败', 'error'));
}


/* ═══════════════════════════════════════════════════════════
   侧边栏收纳（收起/展开 + localStorage 记忆）
   ═══════════════════════════════════════════════════════════ */

function toggleSidebar() {
    var sidebar = document.getElementById('sidebar');
    if (!sidebar) return;
    sidebar.classList.toggle('collapsed');
    var collapsed = sidebar.classList.contains('collapsed');
    // 同步切换 .app-layout 类名，触发 --sidebar-width 变化
    var layout = document.querySelector('.app-layout');
    if (layout) layout.classList.toggle('sidebar-collapsed', collapsed);
    localStorage.setItem('sidebar-collapsed', collapsed ? 'true' : 'false');
    // 切换图标
    var icon = sidebar.querySelector('.sidebar-toggle-btn [data-lucide]');
    if (icon) {
        icon.setAttribute('data-lucide', collapsed ? 'panel-left-open' : 'panel-left-close');
        lucide.createIcons();
    }
}

function initSidebarState() {
    var collapsed = localStorage.getItem('sidebar-collapsed') === 'true';
    var sidebar = document.getElementById('sidebar');
    var layout = document.querySelector('.app-layout');
    if (collapsed && sidebar) {
        sidebar.classList.add('collapsed');
        if (layout) layout.classList.add('sidebar-collapsed');
        var icon = sidebar.querySelector('.sidebar-toggle-btn [data-lucide]');
        if (icon) {
            icon.setAttribute('data-lucide', 'panel-left-open');
            lucide.createIcons();
        }
    }
    // 为导航项添加 data-tooltip（收起时悬浮显示）
    document.querySelectorAll('.sidebar .nav-item').forEach(function(item) {
        var span = item.querySelector('span:not(.nav-badge)');
        if (span) item.setAttribute('data-tooltip', span.textContent.trim());
    });
}


/* ═══════════════════════════════════════════════════════════
   图片灯箱 + 上传（订单详情页）
   ═══════════════════════════════════════════════════════════ */

/* ── 图片灯箱 ── */
function openLightbox(src) {
    var lb = document.getElementById('image-lightbox');
    var img = document.getElementById('lightbox-img');
    if (!lb || !img) return;
    img.src = src;
    lb.style.display = 'flex';
}
function closeLightbox() {
    var lb = document.getElementById('image-lightbox');
    if (lb) lb.style.display = 'none';
}
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeLightbox();
});

/* ── 图片上传（点击 / 拖拽 / 粘贴，多图追加，P15d）── */
function initImageUpload() {
    var zone = document.getElementById('image-upload-zone');
    if (!zone || zone.dataset.bound === '1') return;   // 幂等：HTMX 注入后可重复调用
    zone.dataset.bound = '1';
    var input = document.getElementById('image-upload-input');
    var orderId = zone.dataset.orderId;

    zone.addEventListener('click', function() { input.click(); });
    zone.addEventListener('dragover', function(e) {
        e.preventDefault();
        zone.classList.add('drag-over');
    });
    zone.addEventListener('dragleave', function() { zone.classList.remove('drag-over'); });
    zone.addEventListener('drop', function(e) {
        e.preventDefault();
        zone.classList.remove('drag-over');
        uploadImages(orderId, e.dataTransfer.files);
    });
    input.addEventListener('change', function() {
        uploadImages(orderId, input.files);
        input.value = '';   // 允许重复选择同一文件
    });

    // 粘贴上传：监听整个文档的 paste 事件（仅编辑态存在上传区时生效）
    document.addEventListener('paste', function(e) {
        if (!document.getElementById('image-upload-zone')) return;
        var items = (e.clipboardData || window.clipboardData).items;
        if (!items) return;
        for (var i = 0; i < items.length; i++) {
            if (items[i].type.indexOf('image/') === 0) {
                var file = items[i].getAsFile();
                if (file) {
                    e.preventDefault();
                    uploadImages(orderId, [file]);
                    return;
                }
            }
        }
    });
}

function uploadImages(orderId, files) {
    if (!files || !files.length) return;
    Array.prototype.slice.call(files).forEach(function(f) { uploadImage(orderId, f); });
}

function uploadImage(orderId, file) {
    if (file.size > 10 * 1024 * 1024) {
        showToast('文件过大，最大 10MB', 'error');
        return;
    }
    var fd = new FormData();
    fd.append('image', file);
    dirtyFetch('/orders/' + orderId + '/upload-image', { method: 'POST', body: fd })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                showToast('图片上传成功', 'success');
                appendEditThumb(orderId, data.image_id, data.thumb_url || data.url);
            } else {
                showToast(data.error || '上传失败', 'error');
            }
        })
        .catch(function() { showToast('上传失败', 'error'); });
}

// 上传成功后把新图追加到编辑区图片列表（不覆盖既有图）
function appendEditThumb(orderId, imageId, url) {
    var list = document.getElementById('edit-image-list');
    if (!list) return;
    var wrap = document.createElement('div');
    wrap.className = 'edit-image-thumb';
    wrap.setAttribute('data-image-id', imageId);
    var img = document.createElement('img');
    img.src = url; img.alt = '作品图'; img.loading = 'lazy';
    var btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'edit-image-del'; btn.title = '移除';
    btn.innerHTML = '&times;';
    btn.onclick = function() { removeImage(orderId, imageId); };
    wrap.appendChild(img);
    wrap.appendChild(btn);
    list.appendChild(wrap);
}

// 删除单张（带 image_id）；无 image_id 时后端回退清空整单（兼容旧调用）
function removeImage(orderId, imageId) {
    var fd = new FormData();
    if (imageId != null) fd.append('image_id', imageId);
    dirtyFetch('/orders/' + orderId + '/remove-image', { method: 'POST', body: fd })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                showToast('图片已移除', 'info');
                var list = document.getElementById('edit-image-list');
                if (list && imageId != null) {
                    var el = list.querySelector('[data-image-id="' + imageId + '"]');
                    if (el) el.remove();
                }
            } else {
                showToast(data.error || '移除失败', 'error');
            }
        });
}


/* ═══════════════════════════════════════════════════════════
   统计模块自定义显隐（P16h）
   - 扫描 [data-module] 容器，应用持久化状态（显示 / 隐藏）
   - 偏好持久化到 localStorage，key = 'modules:' + pageKey
   - 操作入口统一在页面右上角「模块设置」面板（P20-F15）
   ═══════════════════════════════════════════════════════════ */

function loadModulePrefs(pageKey) {
    try { return JSON.parse(localStorage.getItem('modules:' + pageKey) || '{}'); }
    catch (e) { return {}; }
}

function saveModulePref(pageKey, id, state) {
    var prefs = loadModulePrefs(pageKey);
    if (state === 'show') delete prefs[id]; else prefs[id] = state;   // 显示=默认，不落库
    localStorage.setItem('modules:' + pageKey, JSON.stringify(prefs));
}

function applyModuleState(el, state) {
    el.classList.toggle('module-hidden', state === 'hide');
    syncGridModuleState(el, state);   // P21a：收入页网格下同步 widget 摘除/挂回
}

function getModuleState(prefs, id) {
    return prefs[id] === 'hide' ? 'hide' : 'show';   // 历史 'min' 值按显示处理
}

// P20-F15：原 injectModuleToolbar（向模块容器注入操作按钮）已移除，
// 显示/隐藏统一经右上角「模块设置」面板操作（applyModuleState 不变）。

function collectModules() {
    var out = [];
    document.querySelectorAll('[data-module]').forEach(function(el) {
        out.push({ id: el.getAttribute('data-module'), title: el.getAttribute('data-module-title') || el.getAttribute('data-module'), el: el });
    });
    return out;
}

function syncModuleSettingsPanel(pageKey) {
    var panel = document.getElementById('module-settings-panel');
    if (!panel) return;
    var prefs = loadModulePrefs(pageKey);
    panel.querySelectorAll('.module-setting-row').forEach(function(row) {
        var id = row.getAttribute('data-module-id');
        var state = getModuleState(prefs, id);
        var chk = row.querySelector('input[type="checkbox"]');
        if (chk) chk.checked = (state !== 'hide');
    });
}

function buildModuleSettingsPanel(pageKey, modules) {
    var panel = document.getElementById('module-settings-panel');
    if (!panel) return;
    var html = '<div class="module-settings-head">模块设置</div>';
    modules.forEach(function(m) {
        html += '<div class="module-setting-row" data-module-id="' + m.id + '">'
            + '<label class="module-setting-main"><input type="checkbox"> ' + m.title + '</label>'
            + '</div>';
    });
    html += '<div class="module-settings-foot"><button type="button" class="btn btn-sm btn-ghost" onclick="resetModules(\'' + pageKey + '\')">恢复默认</button></div>';
    panel.innerHTML = html;
    // 绑定事件
    modules.forEach(function(m) {
        var row = panel.querySelector('.module-setting-row[data-module-id="' + m.id + '"]');
        if (!row) return;
        var showChk = row.querySelector('.module-setting-main input');
        showChk.addEventListener('change', function() {
            var next = showChk.checked ? 'show' : 'hide';
            applyModuleState(m.el, next);
            saveModulePref(pageKey, m.id, next);
            if (window.lucide) lucide.createIcons();
        });
    });
    syncModuleSettingsPanel(pageKey);
}

function toggleModuleSettings() {
    var panel = document.getElementById('module-settings-panel');
    if (panel) panel.classList.toggle('open');
}

function resetModules(pageKey) {
    localStorage.removeItem('modules:' + pageKey);
    collectModules().forEach(function(m) { applyModuleState(m.el, 'show'); });
    syncModuleSettingsPanel(pageKey);
    if (window.lucide) lucide.createIcons();
}

// P20e：/income 旧「图表行」模块键一次性迁移为按块键（P16h 偏好继承）
function migrateIncomeModulePrefs(pageKey) {
    if (pageKey !== '/income') return;
    var prefs = loadModulePrefs(pageKey);
    var map = { 'charts-row1': ['monthly-stats', 'type-distribution'], 'charts-row2': ['projected', 'top-customers'] };
    var changed = false;
    Object.keys(map).forEach(function(oldKey) {
        if (!prefs[oldKey]) return;
        map[oldKey].forEach(function(newKey) { if (!prefs[newKey]) prefs[newKey] = prefs[oldKey]; });
        delete prefs[oldKey];
        changed = true;
    });
    if (changed) localStorage.setItem('modules:' + pageKey, JSON.stringify(prefs));
}

function initModuleCustomizer(pageKey) {
    migrateIncomeModulePrefs(pageKey);
    var modules = collectModules();
    if (!modules.length) return;   // 无统计模块的页面 no-op
    var prefs = loadModulePrefs(pageKey);
    modules.forEach(function(m) {
        applyModuleState(m.el, getModuleState(prefs, m.id));
    });
    buildModuleSettingsPanel(pageKey, modules);
    // 点击面板外关闭
    document.addEventListener('click', function(e) {
        var panel = document.getElementById('module-settings-panel');
        if (!panel || !panel.classList.contains('open')) return;
        if (panel.contains(e.target) || (e.target.closest && e.target.closest('#module-settings-btn'))) return;
        panel.classList.remove('open');
    });
    if (window.lucide) lucide.createIcons();
}


/* ═══════════════════════════════════════════════════════
   P21a 收入页自定义布局（gridstack）
   模板已用 gs-* 属性写好默认坐标；gridstack 未加载/初始化失败时
   `.dash-grid` 不带 layout-active，退化为服务端顺序块流（零感知降级）。
   服务端 data-layout 提供用户坐标（空串=默认）；仅 12 列（宽屏）下允许
   保存——窄屏 gridstack 折成 1/2 列，此时 save() 会返回折叠后坐标。
   ═══════════════════════════════════════════════════════ */

function incomeSafeJson(raw) {
    try { return JSON.parse(raw); } catch (e) { return null; }
}

function initIncomeLayout() {
    var wrap = document.getElementById('income-grid');
    if (!wrap) return;                              // 非收入页
    if (typeof GridStack === 'undefined') return;   // vendor 未加载 → 顺序块流

    var grid;
    try {
        grid = GridStack.init({
            column: 12, cellHeight: 20, margin: 8, float: false,
            staticGrid: true, animate: true,
            handle: '.card-header, .stats-grid',
            columnOpts: { breakpointForWindow: true, breakpoints: [{ w: 1024, c: 1 }, { w: 1200, c: 2 }] }
        }, wrap);
    } catch (e) {
        logFrontendError('P21a', 'gridstack 初始化失败：' + e.message, 'app.js', 0);
        return;
    }
    window.__incomeGrid = grid;
    wrap.classList.add('layout-active');

    var saved = incomeSafeJson(wrap.getAttribute('data-layout') || 'null');
    if (wrap.getAttribute('data-layout') && !Array.isArray(saved)) {
        logFrontendError('P21a', '布局 JSON 非法，回退默认布局', 'app.js', 0);
        saved = null;
    }
    if (Array.isArray(saved) && saved.length) applyIncomeLayout(grid, saved);

    var editBtn = document.getElementById('layout-edit-btn');
    var resetBtn = document.getElementById('layout-reset-btn');
    if (editBtn) editBtn.style.display = '';
    if (resetBtn && Array.isArray(saved) && saved.length) resetBtn.style.display = '';

    var timer = null;
    grid.on('change', function() {
        if (grid.opts.staticGrid) return;   // 只读态（含 P16h 显隐引发的 update）不落库
        clearTimeout(timer);
        timer = setTimeout(saveIncomeLayout, 800);
    });
    grid.on('resizestop', function(ev, item) {
        var cv = item && item.querySelector('canvas');
        var chart = cv && window.Chart && Chart.getChart(cv);
        if (chart) chart.resize();
    });
}

// 按 gs-id 套用服务端坐标；saved 里没有的块（新增块）自动追加到底部
function applyIncomeLayout(grid, saved) {
    var pos = {}, maxY = 0;
    saved.forEach(function(n) {
        if (!n || !n.id) return;
        pos[n.id] = n;
        maxY = Math.max(maxY, (n.y || 0) + (n.h || 1));
    });
    var plan = [];
    grid.getGridItems().forEach(function(item) {
        var id = item.getAttribute('gs-id');
        var p = pos[id];
        if (p) {
            plan.push([item, { x: p.x || 0, y: p.y || 0, w: p.w || 1, h: p.h || 1 }]);
        } else {
            var h = parseInt(item.getAttribute('gs-h'), 10) || 10;
            plan.push([item, { x: 0, y: maxY, w: parseInt(item.getAttribute('gs-w'), 10) || 12, h: h }]);
            maxY += h;
        }
    });
    grid.batchUpdate();
    plan.forEach(function(pair) { grid.update(pair[0], pair[1]); });
    grid.commit();
}

function toggleIncomeLayoutEdit() {
    var grid = window.__incomeGrid;
    if (!grid) return;
    var editing = !grid.opts.staticGrid;
    if (!editing && grid.getColumn() !== 12) {
        showToast('窗口太窄（已折成单列），拉宽后再自定义布局', 'warning');
        return;
    }
    grid.setStatic(editing);   // 正在编辑 → 设为静态（退出）
    var wrap = document.getElementById('income-grid');
    if (wrap) wrap.classList.toggle('layout-editing', !editing);
    var btn = document.getElementById('layout-edit-btn');
    if (btn) {
        btn.textContent = editing ? '布局' : '完成';
        btn.classList.toggle('btn-primary', !editing);
        btn.classList.toggle('btn-secondary', editing);
    }
    if (!editing) showToast('拖标题栏换位 · 拖右下角缩放', 'info');
}

// 收集全部块坐标：隐藏块用摘除前坐标
function collectIncomeLayout() {
    var wrap = document.getElementById('income-grid');
    if (!wrap) return [];
    var out = [];
    wrap.querySelectorAll('.grid-stack-item').forEach(function(item) {
        var id = item.getAttribute('gs-id');
        if (!id) return;
        var src = item.dataset.gsHidePos ? incomeSafeJson(item.dataset.gsHidePos) : item.gridstackNode;
        if (!src) return;
        out.push({ id: id, x: src.x || 0, y: src.y || 0, w: src.w || 1, h: src.h || 1 });
    });
    return out;
}

function saveIncomeLayout() {
    var grid = window.__incomeGrid;
    if (!grid || grid.getColumn() !== 12) return;   // 折叠列下的坐标不写库
    var nodes = collectIncomeLayout();
    if (!nodes.length) return;
    var fd = new FormData();
    fd.append('layout', JSON.stringify(nodes));
    fetch('/settings/income-layout', { method: 'POST', body: fd }).then(function(res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        var rb = document.getElementById('layout-reset-btn');
        if (rb) rb.style.display = '';
    }).catch(function(err) {
        showToast('布局保存失败：' + err.message, 'error');
    });
}

function resetIncomeLayout() {
    var fd = new FormData();
    fd.append('layout', '');
    fetch('/settings/income-layout', { method: 'POST', body: fd }).then(function(res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        window.location.reload();   // 重新渲染 → 模板 gs-* 默认坐标生效
    }).catch(function(err) {
        showToast('布局重置失败：' + err.message, 'error');
    });
}

// P16h 显隐在网格下的等价操作（applyModuleState 调用；非收入页 no-op）
function syncGridModuleState(el, state) {
    var grid = window.__incomeGrid;
    if (!grid || !el.closest) return;
    var item = el.closest('.grid-stack-item');
    if (!item) return;

    if (state === 'hide') {
        var n = item.gridstackNode;
        if (n) {
            item.dataset.gsHidePos = JSON.stringify({ x: n.x, y: n.y, w: n.w, h: n.h });
            grid.removeWidget(item, false);   // 保留 DOM，只摘出网格（腾出空间）
        }
        item.style.display = 'none';
        return;
    }

    if (item.style.display === 'none') item.style.display = '';
    if (!item.gridstackNode) {
        grid.makeWidget(item);
        var p = item.dataset.gsHidePos ? incomeSafeJson(item.dataset.gsHidePos) : null;
        if (p) grid.update(item, p);
        delete item.dataset.gsHidePos;
    }
}



/* ═══════════════════════════════════════════════════════
   #41 自动检测更新
   浏览器直连 GitHub Releases API（官方支持 CORS），不经 Flask 转发——
   打包后用户机器未必有代理，服务端请求易失败；前端失败则静默降级。
   自动检测每次启动一次（sessionStorage 记会话标记，页面切换不重复请求），
   同一新版本只 Toast 一次；设置页「检查更新」按钮走 checkForUpdate(true) 强制检测并展示结果。
   ═══════════════════════════════════════════════════════ */

// 语义化版本比较：容忍 v 前缀，逐段比数字；a > b 返 1，相等 0，小于 -1
 function compareVersions(a, b) {
    var pa = String(a || '').replace(/^v/i, '').split('.');
    var pb = String(b || '').replace(/^v/i, '').split('.');
    for (var i = 0; i < Math.max(pa.length, pb.length); i++) {
        var na = parseInt(pa[i] || '0', 10) || 0;
        var nb = parseInt(pb[i] || '0', 10) || 0;
        if (na > nb) return 1;
        if (na < nb) return -1;
    }
    return 0;
}

// 检测更新。force=true（设置页按钮）：忽略会话限制，结果写入 #update-check-result；
// force=false（启动自检）：每次启动仅一次 + 发现新版 Toast，同一版本不重复提醒。
function checkForUpdate(force) {
    var current = window.__APP_VERSION;
    var repo = window.__GITHUB_REPO;
    if (!current || !repo) return;

    var resultEl = document.getElementById('update-check-result');
    if (!force) {
        var checked = '';
        try { checked = sessionStorage.getItem('updateCheckedSession') || ''; } catch (e) {}
        if (checked) return;
    } else if (resultEl) {
        resultEl.textContent = '检查中…';
    }

    fetch('https://api.github.com/repos/' + repo + '/releases/latest', {
        headers: { 'Accept': 'application/vnd.github+json' }
    }).then(function(res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
    }).then(function(rel) {
        try { sessionStorage.setItem('updateCheckedSession', '1'); } catch (e) {}
        var latest = rel.tag_name || rel.name || '';
        var pageUrl = rel.html_url || ('https://github.com/' + repo + '/releases');
        var hasNew = latest && compareVersions(latest, current) > 0;

        if (resultEl) {
            if (hasNew) {
                resultEl.innerHTML = '发现新版本 <strong>' + latest.replace(/[<>&"]/g, '') + '</strong>，' +
                    '<a href="' + pageUrl + '" target="_blank" rel="noopener" style="color:var(--color-accent);text-decoration:underline;">前往下载</a>';
            } else {
                resultEl.textContent = '当前已是最新版本（v' + current + '）';
            }
        }

        if (!force && hasNew) {
            var notified = '';
            try { notified = localStorage.getItem('updateNotifiedVersion') || ''; } catch (e) {}
            if (notified !== latest) {
                try { localStorage.setItem('updateNotifiedVersion', latest); } catch (e) {}
                showToast('发现新版本 ' + latest + '，可在设置 → 系统 中查看', 'info');
            }
        }
    }).catch(function(err) {
        // 离线/限流/无 Release 等均静默降级；#42：手动检查失败时给出项目地址让用户自行查看
        if (force && resultEl) {
            var releasesUrl = 'https://github.com/' + repo + '/releases';
            resultEl.innerHTML = '检查失败（网络不可用或 GitHub 限流），可前往 ' +
                '<a href="' + releasesUrl + '" target="_blank" rel="noopener" style="color:var(--color-accent);text-decoration:underline;">' + releasesUrl + '</a> 自行查看';
        }
    });
}

document.addEventListener('DOMContentLoaded', function() {
    // 甘特图: index.html 内联 | 日历: calendar.html 内联
    initKanban();
    initSidebarState();
    initImageUpload();
    initIncomeLayout();                        // P21a 收入页自定义布局（须早于 P16h：网格先建，隐藏块才能正确摘除）
    initModuleCustomizer(location.pathname);   // P16h 统计模块自定义显隐
    checkForUpdate(false);                     // #41/#46 启动自检更新（每会话一次，失败静默）
});

/* ═══════════════════════════════════════════════════════════
   P16i 写后缓存失效（任务卡 39 / T16i.3）
   页面切换用跨文档 View Transitions + Speculation Rules 预取 + 浏览器 bfcache
   做「轻量缓存」。前进导航每次都是新鲜服务端渲染，天然不陈旧；唯一可能陈旧的
   是 bfcache/预取快照在写操作后被恢复。做法：任一写操作（htmx 非 GET 请求成功，
   覆盖新建/编辑/删除/归档/状态更新）后打「数据已变更」时间戳；页面若从 bfcache
   恢复且晚于本页加载时间的写操作发生过，则重新加载，保证切回列表/统计页展示最新数据。
   ═══════════════════════════════════════════════════════════ */
window.__pageLoadedAt = Date.now();

function markDataDirty() {
    try { sessionStorage.setItem('dataDirtyAt', String(Date.now())); } catch (e) {}
}

// P19-F11 X2：写操作统一入口——非 GET 且响应 ok 时自动 markDataDirty。
// 所有改数据的裸 fetch 必须经此封装（跨页 bfcache/预取缓存才不展示旧数据）；
// 纯读取（gantt-data/stats detail/export 打开文件夹/log-error）保持裸 fetch。
function dirtyFetch(url, opts) {
    opts = opts || {};
    var verb = (opts.method || 'GET').toUpperCase();
    return fetch(url, opts).then(function(res) {
        if (res.ok && verb !== 'GET') markDataDirty();
        return res;
    });
}

// htmx 非 GET 请求成功 = 写操作（create/update/delete/archive/status），统一标记数据变更
document.addEventListener('htmx:afterRequest', function(e) {
    var cfg = e.detail && e.detail.requestConfig;
    var verb = cfg && cfg.verb ? String(cfg.verb).toLowerCase() : '';
    var ok = e.detail && e.detail.successful;
    if (ok && verb && verb !== 'get') markDataDirty();
});

// 从 bfcache/预取快照恢复且数据已变更 → 刷新，避免展示旧快照
window.addEventListener('pageshow', function(e) {
    if (!e.persisted) return;
    var dirtyAt = 0;
    try { dirtyAt = parseInt(sessionStorage.getItem('dataDirtyAt') || '0', 10); } catch (err) {}
    if (dirtyAt && dirtyAt > (window.__pageLoadedAt || 0)) location.reload();
});

// HTMX 事件
// P20d：完成终态未录工时 → 弹补录窗（该监听器先于 orderUpdated 触发，见后端 triggers 键序）
document.addEventListener('promptWorkHours', function(e) {
    var d = (e && e.detail) || {};
    if (!d.id) return;
    window.__workHoursPromptActive = true;
    openCenterModal('/orders/' + d.id + '/work-hours', '记录工时');
});

document.addEventListener('orderUpdated', function(e) {
    closeDrawer();
    showToast('订单已更新', 'success');
    // P18-F7：内联抽屉保存命中「过去时间+完成」→ 抽屉关闭后打开归档确认
    var d = (e && e.detail) || {};
    if (d.archiveConfirm) {
        // P20d：补录工时弹窗在场时先暂存，等其关闭后再串行弹归档确认
        if (window.__workHoursPromptActive) {
            window.__pendingArchiveConfirm = d.archiveConfirm;
        } else {
            setTimeout(function() { archiveOrder(d.archiveConfirm, false); }, 300);
        }
    }
});

// P20d：看板/快速切换走原生 fetch，HX-Trigger 头不会自动触发，需手动解析
function handleWorkHoursTrigger(res) {
    try {
        var raw = res.headers.get('HX-Trigger');
        if (!raw) return;
        var data = JSON.parse(raw);
        if (data.promptWorkHours && data.promptWorkHours.id) {
            setTimeout(function() {
                window.__workHoursPromptActive = true;
                openCenterModal('/orders/' + data.promptWorkHours.id + '/work-hours', '记录工时');
            }, 200);
        }
    } catch (err) { /* 头缺失或非 JSON：忽略 */ }
}

// P20d：报价建议条「采用」→ 回填表单金额字段并触发实收联动
function applyQuoteSuggestion(btn, amount) {
    var form = btn.closest('form');
    if (!form) return;
    var input = form.querySelector('input[name="deposit"]:not([disabled])');
    if (!input) return;
    input.value = amount;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    showToast('已采用建议报价 ¥' + amount, 'success');
}

document.addEventListener('orderDeleted', function() {
    showToast('订单已删除', 'info');
    // 返回上一层并刷新
    if (document.referrer && new URL(document.referrer).pathname !== window.location.pathname) {
        window.location.href = document.referrer;
    } else {
        window.location.href = '/orders';
    }
});


/* ═══════════════════════════════════════════════════════════
   系统工具 — 错误日志 + 重启
   ═══════════════════════════════════════════════════════════ */

// ── 全局错误捕获 ──
window.addEventListener('error', function(e) {
    logFrontendError('JS Error', e.message, e.filename, e.lineno);
});

window.addEventListener('unhandledrejection', function(e) {
    // 过滤 View Transitions API 的正常跳过事件（非真实错误）
    var reason = e.reason;
    if (reason && reason.name === 'AbortError') { e.preventDefault(); return; }
    // #46 reason 为空（拿不到任何错误信息）的 rejection 属无害噪音，静默忽略不进日志
    if (reason === undefined || reason === null || String(reason).trim() === '') {
        e.preventDefault(); return;
    }
    logFrontendError('Unhandled Promise', String(reason), '', 0);
});

function logFrontendError(type, message, source, line) {
    // 存储到 localStorage
    var logs = [];
    try { logs = JSON.parse(localStorage.getItem('app-error-logs') || '[]'); } catch(e) {}
    logs.push({
        time: new Date().toISOString(),
        type: type,
        message: message,
        source: source || '',
        line: line || 0,
        url: window.location.href
    });
    if (logs.length > 100) logs = logs.slice(-100);  // 最多 100 条
    try { localStorage.setItem('app-error-logs', JSON.stringify(logs)); } catch(e) {}

    // 同时上报到服务器
    try {
        fetch('/api/log-error', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                time: new Date().toISOString(),
                type: type,
                message: message,
                source: source,
                line: line
            })
        });
    } catch(e) {}
}

// ── 渲染错误日志查看器（供 settings.html 调用）──
function renderErrorLogs() {
    var container = document.getElementById('error-log-viewer');
    var empty = document.getElementById('error-log-empty');
    if (!container || !empty) return;

    var logs = [];
    try { logs = JSON.parse(localStorage.getItem('app-error-logs') || '[]'); } catch(e) {}

    if (logs.length === 0) {
        container.innerHTML = '';
        empty.style.display = '';
        return;
    }

    empty.style.display = 'none';
    logs.reverse();
    var html = '<div style="overflow-x:auto;"><table style="font-size:0.82rem;width:100%;"><thead><tr>' +
        '<th style="width:140px;">时间</th><th style="width:110px;">类型</th><th>信息</th></tr></thead><tbody>';

    logs.forEach(function(log) {
        var badgeClass = log.type.indexOf('Promise') >= 0 ? 'due-soon' : 'overdue';
        var timeStr = (log.time || '').replace('T', ' ').substring(0, 19);
        html += '<tr>' +
            '<td style="white-space:nowrap;font-family:var(--font-mono);font-size:0.75rem;">' + escHtml(timeStr) + '</td>' +
            '<td><span class="status-badge ' + badgeClass + '">' + escHtml(log.type) + '</span></td>' +
            '<td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="' + escHtml(log.message) + '">' + escHtml(log.message) + '</td>' +
            '</tr>';
    });

    html += '</tbody></table></div>';
    container.innerHTML = html;
}

function clearErrorLogs() {
    if (!confirm('确定清空所有前端错误日志？')) return;
    localStorage.removeItem('app-error-logs');
    renderErrorLogs();
    showToast('错误日志已清空', 'success');
}

// ── P22b 数据备份管理 ──
function loadBackupList() {
    var container = document.getElementById('backup-list');
    if (!container) return;
    fetch('/settings/backups')
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (!d.success) {
                container.innerHTML = '<div style="color:var(--color-danger);padding:var(--space-sm);">加载失败: ' + escHtml(d.error || '') + '</div>';
                return;
            }
            var backups = d.backups || [];
            if (!backups.length) {
                container.innerHTML = '<div style="color:var(--color-text-tertiary);padding:var(--space-sm);">暂无备份，请手动创建</div>';
                return;
            }
            var html = '';
            for (var i = 0; i < backups.length; i++) {
                var b = backups[i];
                html += '<div style="display:flex;align-items:center;gap:var(--space-sm);padding:8px 12px;background:var(--color-bg);border-radius:var(--radius-sm);margin-bottom:4px;">';
                html += '<div style="flex:1;min-width:0;">';
                html += '<div style="font-weight:500;">' + escHtml(b.mtime) + '</div>';
                html += '<div style="font-size:0.75rem;color:var(--color-text-tertiary);">' + escHtml(b.size) + ' KB · ' + escHtml(b.orders) + ' 订单 · ' + escHtml(b.customers) + ' 客户</div>';
                html += '</div>';
                html += '<button class="btn btn-ghost btn-sm" style="font-size:0.78rem;" onclick="restoreBackup(\'' + escHtml(b.name) + '\')">恢复</button>';
                html += '<button class="btn btn-ghost btn-sm" style="color:var(--color-danger);font-size:0.78rem;" onclick="deleteBackup(\'' + escHtml(b.name) + '\')">删除</button>';
                html += '</div>';
            }
            container.innerHTML = html;
        })
        .catch(function(e) {
            container.innerHTML = '<div style="color:var(--color-danger);padding:var(--space-sm);">网络错误</div>';
        });
}

function createBackup() {
    fetch('/settings/backup', { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.success) {
                showToast('备份成功', 'success');
                loadBackupList();
            } else {
                showToast('备份失败: ' + (d.error || '未知错误'), 'error');
            }
        })
        .catch(function() { showToast('网络错误', 'error'); });
}

function saveBackupDir() {
    var input = document.getElementById('backup-dir-input');
    if (!input) return;
    var path = input.value.trim();
    var formData = new FormData();
    formData.append('path', path);
    fetch('/settings/backup-dir', { method: 'POST', body: formData })
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.success) {
                showToast(d.message || '备份目录已保存', 'success');
                loadBackupList();
            } else {
                showToast(d.message || '保存失败', 'error');
            }
        })
        .catch(function() { showToast('网络错误', 'error'); });
}

function restoreBackup(filename) {
    if (!confirm('确定恢复此备份？当前全部数据将被替换为该备份的内容！（恢复前会自动备份当前状态）')) return;
    fetch('/settings/restore/' + encodeURIComponent(filename), { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.success) {
                showToast('恢复成功，页面刷新中…', 'success');
                setTimeout(function() { location.reload(); }, 800);
            } else {
                showToast('恢复失败: ' + (d.error || '未知错误'), 'error');
            }
        })
        .catch(function() { showToast('网络错误', 'error'); });
}

function deleteBackup(filename) {
    if (!confirm('确定删除此备份？删除后不可恢复。')) return;
    fetch('/settings/backup/delete/' + encodeURIComponent(filename), { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.success) {
                showToast('备份已删除', 'success');
                loadBackupList();
            } else {
                showToast('删除失败: ' + (d.error || '未知错误'), 'error');
            }
        })
        .catch(function() { showToast('网络错误', 'error'); });
}

loadBackupList();

// ── 重启相关 ──
function openAppFolder() {
    fetch('/export/open-folder')
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.success) showToast('已打开程序目录', 'success');
            else showToast('打开失败，请手动浏览程序目录', 'error');
        })
        .catch(function() { showToast('打开失败，请手动浏览程序目录', 'error'); });
}

function escHtml(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── 保留统计范围的 /api/stats 刷新 ──
function refreshStatsPreservingRange() {
    var params = '';
    try {
        var saved = localStorage.getItem('stats-range');
        if (saved) {
            var range = JSON.parse(saved);
            if (range.preset) params = 'preset=' + encodeURIComponent(range.preset);
            if (range.preset && range.preset !== 'all' && range.preset !== 'month') {
                // 需要重新应用预设
                var now = new Date();
                var from = null, to = null;
                switch (range.preset) {
                    case 'quarter':
                        var q = Math.floor(now.getMonth() / 3);
                        from = new Date(now.getFullYear(), q * 3, 1);
                        to = new Date();
                        break;
                    case 'year':
                        from = new Date(now.getFullYear(), 0, 1);
                        to = new Date();
                        break;
                }
                if (from) params += '&from=' + encodeURIComponent(fmtDateSt(from));
                if (to) params += '&to=' + encodeURIComponent(fmtDateSt(to));
            }
        }
    } catch(e) {}
    var url = '/api/stats' + (params ? '?' + params : '');
    htmx.ajax('GET', url, { target: '#stats-cards' });
}

function fmtDateSt(d) {
    return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
}


/* ═══════════════════════════════════════════════════════════
   原生滑动面板选色器（方案 E：色块 + 滑动面板）
   ═══════════════════════════════════════════════════════════ */

var ColorPicker = (function() {
    var activePanel = null; // 当前打开的面板

    // HSV → Hex
    function hsvToHex(h, s, v) {
        var r, g, b;
        var i = Math.floor(h / 60) % 6;
        var f = h / 60 - Math.floor(h / 60);
        var p = v * (1 - s);
        var q = v * (1 - f * s);
        var t = v * (1 - (1 - f) * s);
        switch (i) {
            case 0: r=v; g=t; b=p; break;
            case 1: r=q; g=v; b=p; break;
            case 2: r=p; g=v; b=t; break;
            case 3: r=p; g=q; b=v; break;
            case 4: r=t; g=p; b=v; break;
            case 5: r=v; g=p; b=q; break;
        }
        return '#' + [r,g,b].map(function(x) {
            return Math.round(x * 255).toString(16).padStart(2, '0');
        }).join('');
    }

    // Hex → HSV
    function hexToHsv(hex) {
        hex = hex.replace('#', '');
        if (hex.length === 3) hex = hex[0]+hex[0]+hex[1]+hex[1]+hex[2]+hex[2];
        var r = parseInt(hex.substring(0,2), 16) / 255;
        var g = parseInt(hex.substring(2,4), 16) / 255;
        var b = parseInt(hex.substring(4,6), 16) / 255;
        var max = Math.max(r, g, b), min = Math.min(r, g, b);
        var d = max - min, h = 0, s = max === 0 ? 0 : d / max, v = max;
        if (d !== 0) {
            switch (max) {
                case r: h = ((g - b) / d + (g < b ? 6 : 0)) * 60; break;
                case g: h = ((b - r) / d + 2) * 60; break;
                case b: h = ((r - g) / d + 4) * 60; break;
            }
        }
        return { h: h, s: s, v: v };
    }

    // 绘制饱和度/亮度区域
    function drawSB(canvas, hue) {
        var ctx = canvas.getContext('2d');
        var w = canvas.width, h = canvas.height;
        // 背景色 = 当前色相
        ctx.fillStyle = 'hsl(' + hue + ', 100%, 50%)';
        ctx.fillRect(0, 0, w, h);
        // 白色渐变（左→右）
        var whiteGrad = ctx.createLinearGradient(0, 0, w, 0);
        whiteGrad.addColorStop(0, 'rgba(255,255,255,1)');
        whiteGrad.addColorStop(1, 'rgba(255,255,255,0)');
        ctx.fillStyle = whiteGrad;
        ctx.fillRect(0, 0, w, h);
        // 黑色渐变（下→上）
        var blackGrad = ctx.createLinearGradient(0, 0, 0, h);
        blackGrad.addColorStop(0, 'rgba(0,0,0,0)');
        blackGrad.addColorStop(1, 'rgba(0,0,0,1)');
        ctx.fillStyle = blackGrad;
        ctx.fillRect(0, 0, w, h);
    }

    // 创建面板 HTML
    function createPanel(triggerEl, opts) {
        var hex = opts.hex || '#b0b0aa';
        var hsv = hexToHsv(hex);
        var panelId = 'cp-' + Math.random().toString(36).substr(2, 6);

        var wrapper = document.createElement('div');
        wrapper.className = 'cp-wrapper';
        triggerEl.parentNode.insertBefore(wrapper, triggerEl);
        wrapper.appendChild(triggerEl);

        var panel = document.createElement('div');
        panel.className = 'cp-panel';
        panel.id = panelId;
        panel.innerHTML =
            '<canvas class="cp-sb-area" width="216" height="140"></canvas>' +
            '<div class="cp-hue-bar"><div class="cp-hue-cursor"></div></div>' +
            '<div class="cp-bottom">' +
                '<div class="cp-preview"></div>' +
                '<input class="cp-hex-input" type="text" value="' + hex + '" maxlength="7">' +
                (opts.showClear ? '<button class="cp-clear-btn" type="button">清除</button>' : '') +
            '</div>';

        wrapper.appendChild(panel);

        var sbCanvas = panel.querySelector('.cp-sb-area');
        var sbCursor = document.createElement('div');
        sbCursor.className = 'cp-sb-cursor';
        sbCanvas.appendChild(sbCursor);
        var hueBar = panel.querySelector('.cp-hue-bar');
        var hueCursor = panel.querySelector('.cp-hue-cursor');
        var preview = panel.querySelector('.cp-preview');
        var hexInput = panel.querySelector('.cp-hex-input');

        var state = { h: hsv.h, s: hsv.s, v: hsv.v };

        function updateUI() {
            var hexVal = hsvToHex(state.h, state.s, state.v);
            preview.style.background = hexVal;
            hexInput.value = hexVal;
            triggerEl.style.background = hexVal;
            triggerEl.dataset.hex = hexVal;
            sbCursor.style.left = (state.s * 216) + 'px';
            sbCursor.style.top = ((1 - state.v) * 140) + 'px';
            hueCursor.style.left = (state.h / 360 * 216) + 'px';
        }
        function fireChange() {
            if (opts.onChange) opts.onChange(hsvToHex(state.h, state.s, state.v));
        }

        // 绘制饱和度区域
        function redrawSB() { drawSB(sbCanvas, state.h); }
        redrawSB();
        updateUI();

        // === 交互：饱和度/亮度区域 ===
        var sbDragging = false;
        function sbInteract(e) {
            var rect = sbCanvas.getBoundingClientRect();
            var x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
            var y = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
            state.s = x;
            state.v = 1 - y;
            updateUI();
        }
        sbCanvas.addEventListener('mousedown', function(e) { sbDragging = true; sbInteract(e); e.preventDefault(); });
        document.addEventListener('mousemove', function(e) { if (sbDragging) sbInteract(e); });
        document.addEventListener('mouseup', function() { if (sbDragging) { sbDragging = false; fireChange(); } });

        // === 交互：色相条 ===
        var hueDragging = false;
        function hueInteract(e) {
            var rect = hueBar.getBoundingClientRect();
            var x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
            state.h = x * 360;
            redrawSB();
            updateUI();
        }
        hueBar.addEventListener('mousedown', function(e) { hueDragging = true; hueInteract(e); e.preventDefault(); });
        document.addEventListener('mousemove', function(e) { if (hueDragging) hueInteract(e); });
        document.addEventListener('mouseup', function() { if (hueDragging) { hueDragging = false; fireChange(); } });

        // === 交互：hex 输入 ===
        hexInput.addEventListener('change', function() {
            var v = hexInput.value.trim();
            if (!v.startsWith('#')) v = '#' + v;
            if (/^#[0-9a-fA-F]{6}$/.test(v)) {
                var newHsv = hexToHsv(v);
                state.h = newHsv.h; state.s = newHsv.s; state.v = newHsv.v;
                redrawSB();
                updateUI();
                fireChange();
            }
        });

        // === 交互：清除按钮 ===
        var clearBtn = panel.querySelector('.cp-clear-btn');
        if (clearBtn && opts.onClear) {
            clearBtn.addEventListener('click', function() { opts.onClear(); closePanel(); });
        }

        // === 定位面板（fixed，不被父容器裁剪）===
        function positionPanel() {
            var rect = triggerEl.getBoundingClientRect();
            var pw = 240, ph = 230;
            var left = rect.left;
            var top = rect.bottom + 6;
            if (left + pw > window.innerWidth - 10) left = window.innerWidth - pw - 10;
            if (top + ph > window.innerHeight - 10) top = rect.top - ph - 6;
            if (left < 10) left = 10;
            panel.style.left = left + 'px';
            panel.style.top = top + 'px';
        }

        // === 打开/关闭 ===
        function openPanel() {
            closeAllPanels();
            panel.classList.add('open');
            activePanel = panel;
            positionPanel();
            var curHex = triggerEl.dataset.hex || hex;
            var curHsv = hexToHsv(curHex);
            state.h = curHsv.h; state.s = curHsv.s; state.v = curHsv.v;
            redrawSB();
            updateUI();
        }

        triggerEl.addEventListener('click', function(e) {
            e.stopPropagation();
            if (panel.classList.contains('open')) {
                closePanel();
            } else {
                openPanel();
            }
        });

        // 阻止面板内的点击冒泡（避免关闭）
        panel.addEventListener('click', function(e) { e.stopPropagation(); });

        return { open: openPanel, close: closePanel, destroy: function() { wrapper.parentNode.insertBefore(triggerEl, wrapper); wrapper.remove(); } };
    }

    function closePanel() {
        if (activePanel) {
            activePanel.classList.remove('open');
            activePanel = null;
        }
    }

    function closeAllPanels() {
        document.querySelectorAll('.cp-panel.open').forEach(function(p) { p.classList.remove('open'); });
        activePanel = null;
    }

    // 点击页面空白处关闭
    document.addEventListener('click', function() { closeAllPanels(); });

    return {
        create: createPanel,
        closeAll: closeAllPanels,
        hsvToHex: hsvToHex,
        hexToHsv: hexToHsv
    };
})();

/* ═══════════════════════════════════════════════════════════
   P20a：图表主题 helper（Chart.js 统一样式）
   收入页 4 图共用，后续时薪新图（P20e）复用同一地基。
   ═══════════════════════════════════════════════════════════ */

// 金额缩写：>=1w 显示 w、>=1k 显示 k（y 轴 ticks 与数据标签共用）
function chartMoneyShort(v) {
    return (v >= 10000) ? (v / 10000).toFixed(1) + 'w' : (v >= 1000 ? (v / 1000).toFixed(1) + 'k' : v);
}

// 读取当前主题 CSS 变量，输出图表统一样式件
// 注：ticksColor/gridColor 供新图接线用；旧 4 图保持 Chart.js 默认坐标轴色（零视觉回归）
function chartTheme() {
    var cs = getComputedStyle(document.documentElement);
    function cssVar(name, fallback) {
        var v = cs.getPropertyValue(name);
        return v ? v.trim() : fallback;
    }
    return {
        text: cssVar('--color-text', '#0b0b0b'),
        textSecondary: cssVar('--color-text-secondary', '#52514e'),
        border: cssVar('--color-border', '#e1e0d9'),
        surface: cssVar('--color-surface', '#fcfcfb'),
        ticksColor: cssVar('--color-text-secondary', '#52514e'),
        gridColor: cssVar('--color-border', '#e1e0d9'),
        // 金额 tooltip（¥ + 千分位）
        moneyTooltip: { callbacks: { label: function(ctx) { return '¥' + ctx.raw.toLocaleString(); } } },
        // 金额 y 轴 ticks（k/w 缩写）
        moneyTicks: { callback: chartMoneyShort },
    };
}

// datalabels 插件工厂：数据点上方金额标签（k/w 缩写、跳过 0 值）
// 颜色在每次绘制时读主题变量（原硬编码 #0b0b0b，暗色主题下不可见 → 跟随 --color-text）
function makeDatalabels() {
    return {
        id: 'datalabels',
        afterDraw: function(chart) {
            var ctx = chart.ctx;
            var color = chartTheme().text;
            chart.data.datasets.forEach(function(ds, i) {
                var meta = chart.getDatasetMeta(i);
                meta.data.forEach(function(pt, j) {
                    var v = ds.data[j];
                    if (v === 0) return;
                    ctx.fillStyle = color;
                    ctx.font = '10px -apple-system, sans-serif';
                    ctx.textAlign = 'center';
                    ctx.fillText(chartMoneyShort(v), pt.x, pt.y - 10);
                });
            });
        }
    };
}

// 系列取色：优先配置色映射（日历品类着色 cal_* 同源），未配色按 8 色循环 + 透明度变体回退
// （自 income.html 迁移；#42 品类可能超过 8 个，第二轮起叠透明度避免颜色重复难辨）
var CHART_FALLBACK_COLORS = ['#2a78d6', '#1baf7a', '#eda100', '#008300', '#4a3aa7', '#e34948', '#e87ba4', '#eb6834'];
function chartFallbackColor(i) {
    var alphas = ['', 'b3', '66'];
    var base = CHART_FALLBACK_COLORS[i % CHART_FALLBACK_COLORS.length];
    var round = Math.floor(i / CHART_FALLBACK_COLORS.length);
    return base + alphas[round % alphas.length];
}
// 单个系列取色（miss 序号由调用方维护）
function seriesColor(name, colorMap, missIndex) {
    return (colorMap || {})[name] || chartFallbackColor(missIndex || 0);
}
// 批量取色：未配色项 miss 计数器只对回退递增（与原 typeChartColors 一致）
function seriesColors(labels, colorMap) {
    var miss = 0;
    var map = colorMap || {};
    return labels.map(function(label) {
        return map[label] || chartFallbackColor(miss++);
    });
}
