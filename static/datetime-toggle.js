/**
 * 双模式时间选择器组件
 * 为 date/datetime-local 输入框添加按日/精确到分钟模式切换
 *
 * 用法：
 *   1. 引入本脚本：<script src="/static/datetime-toggle.js"></script>
 *   2. 初始化：DateTimeToggle.init(document.querySelector('input[name="scheduled_start"]'));
 *   3. 批量初始化：DateTimeToggle.initAll('.dt-toggle-wrap input');
 *
 * 模式说明：
 *   - 按日模式（默认）：type="date"，存储 YYYY-MM-DD，日历显示为全天事件
 *   - 精确模式：type="datetime-local"，存储 YYYY-MM-DDTHH:MM，日历显示为定时事件
 *   - 加载已有值时自动检测：含 'T' → 精确模式，否则 → 按日模式
 */
(function (global) {
    'use strict';

    var CLOCK_SVG_OUTLINE = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>';
    var CLOCK_SVG_FILLED = '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10" fill="currentColor" stroke="none"/><polyline points="12 6 12 12 16 14" stroke="#fff" fill="none"/></svg>';

    /**
     * 为单个日期输入框添加模式切换按钮
     * @param {HTMLInputElement} inputEl - date 或 datetime-local 类型的 input 元素
     */
    function init(inputEl) {
        if (!inputEl || inputEl._dtToggleInit) return;
        inputEl._dtToggleInit = true;

        // 包裹容器
        var wrap = document.createElement('div');
        wrap.className = 'dt-toggle-wrap';
        inputEl.parentNode.insertBefore(wrap, inputEl);
        wrap.appendChild(inputEl);

        // 切换按钮
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'dt-toggle-btn';
        btn.setAttribute('aria-label', '切换时间精度模式');
        wrap.appendChild(btn);

        // 根据已有值自动检测初始模式
        var isPrecise = inputEl.value && inputEl.value.indexOf('T') !== -1;
        setMode(inputEl, wrap, btn, isPrecise);

        // 点击切换
        btn.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            var nowPrecise = !wrap.classList.contains('dt-precise');
            setMode(inputEl, wrap, btn, nowPrecise);
        });
    }

    /**
     * 设置模式
     * @param {HTMLInputElement} inputEl
     * @param {HTMLElement} wrap
     * @param {HTMLButtonElement} btn
     * @param {boolean} precise - true=精确模式，false=按日模式
     */
    function setMode(inputEl, wrap, btn, precise) {
        var val = inputEl.value || '';
        if (precise) {
            // 切换到精确模式：补 T00:00
            wrap.classList.add('dt-precise');
            btn.classList.add('active');
            btn.innerHTML = CLOCK_SVG_FILLED;
            btn.title = '切换到按日选择';
            inputEl.type = 'datetime-local';
            if (val && val.indexOf('T') === -1) {
                inputEl.value = val + 'T00:00';
            }
        } else {
            // 切换到按日模式：截取前 10 字符
            wrap.classList.remove('dt-precise');
            btn.classList.remove('active');
            btn.innerHTML = CLOCK_SVG_OUTLINE;
            btn.title = '切换到精确时间';
            if (val && val.indexOf('T') !== -1) {
                inputEl.value = val.substring(0, 10);
            }
            inputEl.type = 'date';
        }
    }

    /**
     * 批量初始化
     * @param {string} selector - CSS 选择器，选中所有需要添加切换功能的 input
     */
    function initAll(selector) {
        var inputs = document.querySelectorAll(selector);
        for (var i = 0; i < inputs.length; i++) {
            init(inputs[i]);
        }
    }

    // 暴露到全局
    global.DateTimeToggle = {
        init: init,
        initAll: initAll
    };

    // HTMX 内容交换后自动初始化新插入的日期输入框
    document.addEventListener('htmx:afterSwap', function (evt) {
        var target = evt.detail.target;
        var inputs = target.querySelectorAll('.date-field input[type="date"], .date-field input[type="datetime-local"]');
        for (var i = 0; i < inputs.length; i++) {
            if (!inputs[i]._dtToggleInit) {
                init(inputs[i]);
            }
        }
    });

})(window);
