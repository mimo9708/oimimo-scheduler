/* Spec 23 小票打印机 — 单源渲染器（卡 95 T23.7/T23.10）
   D5：预览 / 编辑 / 导出共用本文件唯一渲染源 rcRender(host, draft)。
   计算口径 rcCalc 与服务端 db.calc_receipt_totals（spec §3.4 冻结公式）完全同式，改一处必须同步另一处。
   样式一律 rc- 前缀，禁止复用 base.html 统计弹窗的 .receipt 族。 */
(function() {
'use strict';

// ── 四预设变量包（spec §3.6）：paper/ink 为预设默认值，草稿 style.paper/ink 优先覆盖 ──
var RC_PRESETS = {
    list:  { label: '简洁', font: '"Courier New", Courier, monospace',
             paper: '#fdfcf8', ink: '#1a1a1a', titleSize: '1.4rem',  weight: '700',
             letterSpacing: '0.12em', sep: 'solid',  pad: '24px', width: '420px' },
    retro: { label: '复古商店', font: '"Courier New", Courier, monospace',
             paper: '#e9f5ec', ink: '#1a1a1a', titleSize: '1.8rem',  weight: '800',
             letterSpacing: '0.08em', sep: 'dashed', pad: '26px', width: '420px' },
    hand:  { label: '手写涂鸦', font: '"Segoe Print", "Bradley Hand", "Comic Sans MS", "Ma Shan Zheng", cursive',
             paper: '#f6efe3', ink: '#2b2118', titleSize: '2rem',    weight: '700',
             letterSpacing: '0.04em', sep: 'wave',   pad: '26px', width: '420px' },
    mono:  { label: '极简黑白', font: '"Courier New", Courier, monospace',
             paper: '#ffffff', ink: '#111111', titleSize: '1.2rem',  weight: '400',
             letterSpacing: '0.34em', sep: 'dotted', pad: '26px', width: '420px' }
};
var DEFAULT_BG = '/static/paper-texture.png';  // D10：无自定义背景时的默认纸纹理

// ── 金额格式化（对齐 price_fmt 口径：¥ + 去尾零；小票 0 元显示 ¥0，不转「面议」）──
function rcFmt(n) {
    n = Number(n) || 0;
    return '¥' + n.toFixed(2).replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '');
}

function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── 冻结公式（Spec 24，与服务端 db.calc_receipt_totals 同口径）──
// 单品小计：（单价×数量 + Σ附加）×单品倍率 ×单品折扣（amount 直减 / rate 中文折数/10）；赠品恒 0
function rcItemSub(it) {
    if (it.is_gift) return 0;
    var sub = (Number(it.price) || 0) * (Number(it.qty) || 0);
    (it.extras || []).forEach(function(ex) {
        sub += (Number(ex.price) || 0) * (Number(ex.qty) || 0);
    });
    sub *= Number(it.multiplier) || 1;
    var dt = it.discount_type || 'none';
    var dv = Number(it.discount_value) || 0;
    if (dt === 'amount') sub = Math.max(0, sub - dv);
    else if (dt === 'rate') sub = sub * dv / 10;
    return sub;
}

function rcCalc(items, meta) {
    meta = meta || {};
    var total = 0;
    (items || []).forEach(function(it) {
        total += rcItemSub(it);
    });
    var multiplier = Number(meta.multiplier) || 1;
    var discType = meta.discount_type || 'none';
    var discValue = Number(meta.discount_value) || 0;
    var deposit = Number(meta.deposit) || 0;
    var multed = total * multiplier;
    var grand;
    if (discType === 'amount') grand = multed - discValue;
    else if (discType === 'rate') grand = multed * discValue / 10;
    else grand = multed;
    return { total: total, multed: multed, grand: grand, balance: grand - deposit,
             has_mult: multiplier !== 1,
             has_discount: discValue > 0 && (discType === 'amount' || discType === 'rate'),
             has_deposit: deposit > 0,
             multiplier: multiplier, discount_type: discType, discount_value: discValue,
             deposit: deposit };
}

// 行是否有效内容（默认草稿的空行/全空行 → 预览跳过，spec §6 空票显示占位）
function filled(it) {
    return !!(String(it.name || '').trim() || (Number(it.price) || 0) > 0 ||
              (it.extras || []).length);
}

function barcodeText() {
    var d = new Date();
    function p(n) { return String(n).padStart(2, '0'); }
    return 'R-' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) +
           p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
}

// ── 小票本体样式（预览/编辑共用；scoped 到 .rc-paper 内，全部 rc- 前缀）──
var RC_CSS = [
/* 响应式包裹（2026-08-13 用户需求 1）：viewport 量宽 + scale 随列宽缩放纸张，窗口变化不错乱 */
'.rc-viewport { width: 100%; overflow: hidden; display: flex; justify-content: center;',
'    padding: var(--space-lg, 24px) 0 56px; }',
'.rc-scale { transform-origin: top center; flex: 0 0 auto; }',
'.rc-paper {',
'    position: relative; width: var(--rc-width, 420px);',
'    background-color: var(--rc-paper-color, #fdfcf8);',
'    background-size: cover; background-position: center; background-blend-mode: multiply;',
'    color: var(--rc-ink-color, #1a1a1a);',
'    padding: 22px var(--rc-pad, 24px) 18px; border-radius: 2px;',
'    box-shadow: 0 10px 34px rgba(0,0,0,0.18); box-sizing: border-box;',
'    word-break: break-all;',
'}',
/* 锯齿裁切：radial-gradient mask 上下挖半圆缺口（app.css .receipt 同款手法，rc- 前缀重写） */
'.rc-paper.rc-zigzag {',
'    -webkit-mask-image:',
'        radial-gradient(circle 6px at 10px -3px, transparent 5.5px, #000 6px),',
'        radial-gradient(circle 6px at 10px calc(100% + 3px), transparent 5.5px, #000 6px);',
'    -webkit-mask-size: 20px 100%; -webkit-mask-repeat: repeat-x; -webkit-mask-composite: source-over;',
'    mask-image:',
'        radial-gradient(circle 6px at 10px -3px, transparent 5.5px, #000 6px),',
'        radial-gradient(circle 6px at 10px calc(100% + 3px), transparent 5.5px, #000 6px);',
'    mask-size: 20px 100%; mask-repeat: repeat-x; mask-composite: intersect;',
'}',
'.rc-paper img { max-width: 100%; }',
/* 主图正片叠底：白底融入纸色/纹理，呈现抠图效果（2026-08-13 用户需求） */
'.rc-shop-img { display: block; width: 62%; margin: 2px auto 12px; image-rendering: pixelated; mix-blend-mode: multiply; }',
/* footer 插图（2026-08-13 用户需求 3）：总计与感谢语之间，同款正片叠底 */
'.rc-footer-img { display: block; width: 52%; margin: 14px auto 0; image-rendering: pixelated; mix-blend-mode: multiply; }',
'.rc-shop { text-align: center; }',
'.rc-title { font-size: var(--rc-title-size, 1.4rem); font-weight: var(--rc-title-weight, 700);',
'    letter-spacing: var(--rc-title-ls, 0.12em); line-height: 1.3; }',
'.rc-sub { font-size: 0.74rem; opacity: 0.75; margin-top: 5px; letter-spacing: 0.14em; }',
'.rc-info { margin-top: 10px; font-size: 0.76rem; display: flex; flex-direction: column; gap: 2px; }',
'.rc-info-row { display: flex; justify-content: space-between; gap: 12px; opacity: 0.85; }',
'.rc-info-row b { font-weight: 600; opacity: 0.7; flex-shrink: 0; }',
'.rc-sep { margin: 13px 0 11px; border: none; }',
'.rc-sep.solid  { border-top: 1px solid currentColor; }',
'.rc-sep.dashed { border-top: 2px dashed currentColor; }',
'.rc-sep.dotted { border-top: 1px dotted currentColor; }',
'.rc-sep.wave   { border: none; height: 5px;',
'    background: url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' width=\'12\' height=\'5\'%3E%3Cpath d=\'M0 3.5 Q 3 0.5 6 3.5 T 12 3.5\' fill=\'none\' stroke=\'currentColor\' stroke-width=\'1.3\'/%3E%3C/svg%3E") repeat-x; }',
'.rc-table-head, .rc-row, .rc-extra-row {',
'    display: grid; grid-template-columns: 1fr auto auto auto;',
'    gap: 3px 10px; font-size: 0.85rem; line-height: 1.5; align-items: baseline;',
'}',
'.rc-table-head { font-size: 0.74rem; opacity: 0.7; margin-bottom: 7px; letter-spacing: 0.08em; }',
'.rc-table-head span:nth-child(2), .rc-cell-num { text-align: right; white-space: nowrap; }',
'.rc-row { padding: 3px 0; }',
'.rc-name { overflow-wrap: anywhere; }',
'.rc-badge { display: inline-block; margin-left: 6px; padding: 0 4px; border: 1px solid currentColor;',
'    font-size: 0.72em; opacity: 0.75; letter-spacing: 0; vertical-align: baseline; white-space: nowrap; }',
'.rc-row.rc-gift .rc-name::before { content: "[赠品] "; font-size: 0.9em; opacity: 0.9; }',
'.rc-row.rc-gift .rc-gift-origin { text-decoration: line-through; opacity: 0.55; font-size: 0.82em; }',
'/* Spec 32 卡 154：小票是纸面场景（纸底不随主题变深），禁用主题 success token（深色主题下被派生为浅绿，打白纸 ratio 1.7 不可见），固定纸面深绿 */',
'.rc-row.rc-gift .rc-subtotal { color: #15803d; font-weight: 700; }',
'.rc-empty { text-align: center; opacity: 0.6; padding: 14px 0; font-size: 0.85rem; }',
'.rc-extra-row { padding-left: 1.1em; font-size: 0.78rem; opacity: 0.85; }',
'.rc-extra-row .rc-name::before { content: "• "; }',
'.rc-stats { font-size: 0.88rem; }',
'.rc-stat-row { display: flex; justify-content: space-between; gap: 12px; padding: 2.5px 0; }',
'.rc-stat-row b { font-weight: 600; }',
'.rc-grand { font-size: 1.2rem; font-weight: 800; letter-spacing: 0.03em; padding: 4px 0; }',
'.rc-small { font-size: 0.78rem; opacity: 0.82; }',
'.rc-footer { text-align: center; margin-top: 15px; font-size: 0.8rem; opacity: 0.8; }',
'.rc-barcode-wrap { text-align: center; margin-top: 13px; }',
'.rc-barcode-wrap svg { max-width: 100%; height: auto; }'
].join('\n');

function ensureCss() {
    if (document.getElementById('rc-style')) return;
    var st = document.createElement('style');
    st.id = 'rc-style';
    st.textContent = RC_CSS;
    document.head.appendChild(st);
}

function bgUrl(style) {
    return style.bg_path ? '/uploads/' + style.bg_path : DEFAULT_BG;
}
function imgUrl(style) {
    return style.image_path ? '/uploads/' + style.image_path : '';
}
function footerImgUrl(style) {
    return style.footer_image_path ? '/uploads/' + style.footer_image_path : '';
}

/* 响应式适配（2026-08-13 用户需求 1）：纸张随容器宽度等比缩放。
   transform scale 保持内部布局比例（不挤压换行）；scaleBox 手动补偿高度收掉缩放后的布局占位。 */
function rcFitPaper(host) {
    var viewport = host.querySelector('.rc-viewport');
    var scaleBox = host.querySelector('.rc-scale');
    var paper = scaleBox && scaleBox.firstElementChild;
    if (!viewport || !scaleBox || !paper) return;
    scaleBox.style.transform = 'none';
    scaleBox.style.height = '';
    var avail = viewport.clientWidth;
    var pw = paper.offsetWidth;
    if (!avail || !pw) return;
    var s = Math.min(1, avail / pw);
    if (s < 0.995) {
        scaleBox.style.transform = 'scale(' + s + ')';
        scaleBox.style.height = (paper.offsetHeight * s) + 'px';
    }
}
var rcObserver = null;
function rcObserveFit(host) {
    if (typeof ResizeObserver === 'undefined') return;
    if (!rcObserver) {
        rcObserver = new ResizeObserver(function(entries) {
            entries.forEach(function(en) { rcFitPaper(en.target); });
        });
    }
    rcObserver.observe(host);  // 重复 observe 同一 target 安全（规范幂等）
}

// ── 唯一渲染入口：预览页/编辑页实时预览/导出前均走这里 ──
function rcRender(host, draft) {
    if (!host) return;
    ensureCss();
    draft = draft || {};
    var items = draft.items || [];
    var meta = draft.meta || {};
    var style = draft.style || {};
    var preset = RC_PRESETS[style.preset] || RC_PRESETS.list;
    var calc = rcCalc(items, meta);

    var paper = style.paper || preset.paper;
    var ink = style.ink || preset.ink;

    var html = '';
    if (style.image_path) {
        // 主图被外部删除 → onerror 移除不裂图（spec §6）
        html += '<img class="rc-shop-img" src="' + esc(imgUrl(style)) + '" alt="主图" ' +
                'onerror="this.remove()">';
    }
    html += '<div class="rc-shop">' +
            '<div class="rc-title">' + (esc(meta.shop_name) || '— 未命名小店 —') + '</div>' +
            (meta.subtitle ? '<div class="rc-sub">' + esc(meta.subtitle) + '</div>' : '') +
            '</div>';

    var infoRows = '';
    if (meta.order_date) infoRows += '<div class="rc-info-row"><b>DATE</b><span>' + esc(meta.order_date) + '</span></div>';
    if (meta.order_no)   infoRows += '<div class="rc-info-row"><b>NO.</b><span>' + esc(meta.order_no) + '</span></div>';
    if (meta.contact)    infoRows += '<div class="rc-info-row"><b>TEL</b><span>' + esc(meta.contact) + '</span></div>';
    if (infoRows) html += '<div class="rc-info">' + infoRows + '</div>';

    html += '<hr class="rc-sep ' + preset.sep + '">';

    var visible = items.filter(filled);
    if (!visible.length) {
        html += '<div class="rc-empty">— 暂无制品 —</div>';
    } else {
        html += '<div class="rc-table">' +
                '<div class="rc-table-head"><span>制品</span><span>单价</span><span>数量</span><span>小计</span></div>';
        visible.forEach(function(it) {
            var gift = !!it.is_gift;
            var sub = rcItemSub(it);  // Spec 24：含单品倍率与折扣的小计
            // 角标：×N 标签（单品倍率）+ 折扣标记（N 折 / −¥金额），客户看得懂价格构成
            var badge = '';
            var mult = Number(it.multiplier) || 1;
            if (!gift && (mult !== 1 || String(it.mult_label || '').trim())) {
                badge += '<span class="rc-badge">×' + mult + (String(it.mult_label || '').trim() ? ' ' + esc(it.mult_label) : '') + '</span>';
            }
            if (!gift && it.discount_type === 'rate' && (Number(it.discount_value) || 0) > 0) {
                badge += '<span class="rc-badge">' + (Number(it.discount_value) || 0) + ' 折</span>';
            } else if (!gift && it.discount_type === 'amount' && (Number(it.discount_value) || 0) > 0) {
                badge += '<span class="rc-badge">−' + rcFmt(it.discount_value) + '</span>';
            }
            html += '<div class="rc-row' + (gift ? ' rc-gift' : '') + '">' +
                    '<span class="rc-name">' + esc(it.name) + badge + '</span>' +
                    '<span class="rc-cell-num">' +
                    (gift ? '<span class="rc-gift-origin">' + rcFmt(it.price) + '</span>' : rcFmt(it.price)) +
                    '</span>' +
                    '<span class="rc-cell-num">×' + (Number(it.qty) || 0) + '</span>' +
                    '<span class="rc-cell-num rc-subtotal">' + rcFmt(sub) + '</span>' +
                    '</div>';
            (it.extras || []).forEach(function(ex) {
                html += '<div class="rc-extra-row">' +
                        '<span class="rc-name">' + esc(ex.name) + '</span>' +
                        '<span class="rc-cell-num">+' + rcFmt(ex.price) + '</span>' +
                        '<span class="rc-cell-num">×' + (Number(ex.qty) || 0) + '</span>' +
                        '<span class="rc-cell-num"></span></div>';
            });
        });
        html += '</div>';
    }

    html += '<hr class="rc-sep ' + preset.sep + '">';
    html += '<div class="rc-stats">' +
            '<div class="rc-stat-row"><span>合计</span><b>' + rcFmt(calc.total) + '</b></div>';
    if (calc.has_mult) {
        // 2026-08-13 用户需求 2b：倍率行文案可自定义（label 默认「倍率」；expr 的 {n} 替换为数值，空串=不显示乘数）
        var mLabel = String(meta.mult_label == null ? '倍率' : meta.mult_label).trim() || '倍率';
        var mExprRaw = meta.mult_expr == null ? '×{n}' : String(meta.mult_expr);
        var mExpr = mExprRaw.split('{n}').join(String(calc.multiplier)).trim();
        var mText = (mExpr ? mLabel + ' ' + mExpr : mLabel) + '（全部制品）';
        html += '<div class="rc-stat-row"><span>' + esc(mText) + '</span><b>' + rcFmt(calc.multed) + '</b></div>';
    }
    if (calc.has_discount) {
        var discLabel = calc.discount_type === 'rate'
            ? '折扣（' + calc.discount_value + ' 折）'
            : '折扣';
        var discAmount = calc.discount_type === 'rate'
            ? calc.multed - calc.grand
            : calc.discount_value;
        html += '<div class="rc-stat-row"><span>' + discLabel + '</span><b>−' + rcFmt(discAmount) + '</b></div>';
    }
    html += '<div class="rc-stat-row rc-grand"><span>总计</span><b>' + rcFmt(calc.grand) + '</b></div>';
    if (calc.has_deposit) {
        html += '<div class="rc-stat-row rc-small"><span>已收定金</span><b>−' + rcFmt(calc.deposit) + '</b></div>' +
                '<div class="rc-stat-row rc-small"><span>尾款</span><b>' + rcFmt(calc.balance) + '</b></div>';
    }
    html += '</div>';

    if (style.footer_image_path) {
        // footer 插图：总计区与感谢语之间（用户需求 3）；onerror 移除不裂图
        html += '<img class="rc-footer-img" src="' + esc(footerImgUrl(style)) + '" alt="footer 插图" ' +
                'onerror="this.remove()">';
    }
    if (meta.footer) {
        html += '<div class="rc-footer">' + esc(meta.footer) + '</div>';
    }
    if (style.barcode) {
        html += '<div class="rc-barcode-wrap"><svg class="rc-barcode"></svg></div>';
    }

    host.innerHTML = '<div class="rc-viewport"><div class="rc-scale"><div class="rc-paper' +
                     (style.zigzag ? ' rc-zigzag' : '') + '"></div></div></div>';
    var paperEl = host.querySelector('.rc-paper');
    // 样式一律 DOM API 赋值：内联 style 字符串拼接会被 font-family 双引号截断属性（实测致背景失效 + 导出 XML 非法）
    paperEl.style.setProperty('--rc-paper-color', paper);
    paperEl.style.setProperty('--rc-ink-color', ink);
    paperEl.style.setProperty('--rc-title-size', preset.titleSize);
    paperEl.style.setProperty('--rc-title-weight', preset.weight);
    paperEl.style.setProperty('--rc-title-ls', preset.letterSpacing);
    paperEl.style.setProperty('--rc-pad', preset.pad);
    paperEl.style.setProperty('--rc-width', preset.width);
    paperEl.style.fontFamily = preset.font;
    paperEl.style.backgroundImage = "url('" + bgUrl(style) + "')";
    paperEl.innerHTML = html;

    if (style.barcode && typeof JsBarcode !== 'undefined') {
        try {
            JsBarcode(paperEl.querySelector('.rc-barcode'), barcodeText(), {
                format: 'CODE128', width: 1.4, height: 36, displayValue: true,
                fontSize: 10, margin: 4, background: 'transparent', lineColor: ink
            });
        } catch (e) {
            console.error('[receipt] barcode 渲染失败', e);
        }
    }

    // 响应式：渲染后按容器宽度缩放纸张；并挂 ResizeObserver 跟踪列宽/窗口变化
    rcFitPaper(host);
    rcObserveFit(host);
}

/* ═══ PNG 导出（卡 95 T23.10：自 pricelist_preview.html foreignObject 导出器移植改写）═══ */

var EXPORT_SCALE = 2;
var exporting = false;

/* 跳过属性黑名单（2026-08-13 导出 PNG 错位修复）：
   1) 尺寸快照类：width/height（原有）+ inline-size/block-size + min-/max- 逻辑尺寸 + flex-basis。
      这些 computed 值是「页面上下文字体度量算出的内容宽度」，冻结后 SVG 栅格化上下文
      （中文回退字体/字距取整与页面有亚像素差异）内容超 1px 即折行 → 标签逐字竖排错位。
   2) 定位类：position/top/left/right/bottom（原有，尺寸由内容自然撑开）。
   3) 文字方向类：writing-mode/text-orientation（页面恒 horizontal-tb，防 SVG 上下文解析差异）。 */
var CLONE_SKIP = {
    'width': 1, 'height': 1, 'position': 1,
    'top': 1, 'left': 1, 'right': 1, 'bottom': 1,
    'inline-size': 1, 'block-size': 1,
    'min-inline-size': 1, 'max-inline-size': 1,
    'min-block-size': 1, 'max-block-size': 1,
    'flex-basis': 1,
    'writing-mode': 1, 'text-orientation': 1, '-webkit-text-orientation': 1
};

// 克隆小票并内联全部计算样式（foreignObject 内无样式表，逐节点搬运）
function buildStyledClone(src) {
    var clone = src.cloneNode(true);
    // ::before 伪元素内容物化（不随 computed style 复制，导出会丢「•」「[赠品]」前缀）
    Array.prototype.forEach.call(clone.querySelectorAll('.rc-extra-row .rc-name'), function(n) {
        n.insertBefore(document.createTextNode('\u2022 '), n.firstChild);
    });
    Array.prototype.forEach.call(clone.querySelectorAll('.rc-row.rc-gift .rc-name'), function(n) {
        n.insertBefore(document.createTextNode('[赠品] '), n.firstChild);
    });
    var srcNodes = [src];
    var cloneNodes = [clone];
    function collect(el, arr) {
        for (var i = 0; i < el.children.length; i++) {
            arr.push(el.children[i]);
            collect(el.children[i], arr);
        }
    }
    collect(src, srcNodes);
    collect(clone, cloneNodes);
    for (var i = 0; i < srcNodes.length; i++) {
        var cs = window.getComputedStyle(srcNodes[i]);
        var st = cloneNodes[i].style;
        for (var j = 0; j < cs.length; j++) {
            var prop = cs[j];
            if (CLONE_SKIP[prop]) continue;
            var val = cs.getPropertyValue(prop);
            // 外部资源 url() 剥离后重新内联；data: URI 无画布污染风险，保留（hand 预设波浪分隔线）
            if (val.indexOf('url(') !== -1 && val.indexOf('data:') === -1) val = 'none';
            st.setProperty(prop, val);
        }
        cloneNodes[i].removeAttribute('class');
        if (cloneNodes[i].tagName === 'IMG') {
            cloneNodes[i].setAttribute('width', String(srcNodes[i].clientWidth || 56));
            cloneNodes[i].setAttribute('height', String(srcNodes[i].clientHeight || 56));
        }
    }
    // mask 锯齿在 foreignObject 渲染不可靠 → 始终移除 mask。
    // Spec 28 D6（Task 128）：zigzag 启用时保留原 padding，由 canvas 层补绘锯齿（导出与预览 1:1）；
    // 未启用则维持「直边 + 上下留白」既有形态不变
    var zigzag = src.classList.contains('rc-zigzag');
    clone.style.setProperty('-webkit-mask-image', 'none');
    clone.style.setProperty('mask-image', 'none');
    if (!zigzag) {
        clone.style.padding = '30px ' + (window.getComputedStyle(src).paddingRight || '24px') + ' 26px';
    }
    clone.setAttribute('xmlns', 'http://www.w3.org/1999/xhtml');
    clone.style.width = src.clientWidth + 'px';
    clone.style.margin = '0';
    // 内联 SVG（条码）序列化进 SVG 文档须显式带 xmlns
    clone.querySelectorAll('svg').forEach(function(sv) {
        sv.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    });
    return clone;
}

// 任意外部图片 → data-URI（同源 fetch，防 foreignObject 画布污染）
function fetchAsDataUri(url) {
    return fetch(url).then(function(r) { return r.blob(); })
        .then(function(blob) {
            return new Promise(function(resolve, reject) {
                var fr = new FileReader();
                fr.onload = function() { resolve(fr.result); };
                fr.onerror = function() { reject(new Error('read failed')); };
                fr.readAsDataURL(blob);
            });
        });
}

// 背景图 fetch→data-URI 内联（小票背景是核心视觉，不可置空）；失败回退纯色纸底
function inlineBackground(clone, url) {
    if (!url) return Promise.resolve();
    return fetchAsDataUri(url).then(function(dataUri) {
        clone.style.backgroundImage = 'url("' + dataUri + '")';
        clone.style.backgroundSize = 'cover';
        clone.style.backgroundPosition = 'center';
    }).catch(function() {
        clone.style.backgroundImage = 'none';
    });
}

// 主图 <img> 逐个转 data-URI；失败移除不裂图
function inlineImages(clone) {
    var imgs = Array.prototype.slice.call(clone.querySelectorAll('img'));
    return imgs.reduce(function(p, img) {
        return p.then(function() {
            var src = img.getAttribute('src');
            if (!src || src.indexOf('data:') === 0) return;
            return fetchAsDataUri(src).then(function(dataUri) {
                img.setAttribute('src', dataUri);
            }).catch(function() { img.remove(); });
        });
    }, Promise.resolve());
}

/* Spec 28 D6（Task 128）：canvas 层补绘锯齿边 —— CSS mask 在 foreignObject 渲染不可靠，
   导出时以 destination-out 挖半圆弓形，参数与 .rc-zigzag 的 radial-gradient 等效：
   circle 6px at 10px -3px / 周期 20px（上边圆心 y=-3、下边圆心 y=h+3，均悬于画布外，
   只在画布内留下深 3px 的弓形缺口）。scale 为坐标缩放因子：在已 ctx.scale(EXPORT_SCALE)
   的变换坐标系下传 1（CSS 像素），即等效锯齿参数同步放大 */
function _drawZigzagEdge(ctx, width, height, scale) {
    var toothRadius = 6 * scale;
    var toothSpacing = 20 * scale;
    var toothOffsetY = 3 * scale;
    ctx.save();
    ctx.globalCompositeOperation = 'destination-out';
    ctx.beginPath();
    for (var cx = 10 * scale; cx - toothRadius < width; cx += toothSpacing) {
        // moveTo 断开子路径，避免 arc 间被直线填充串联
        ctx.moveTo(cx + toothRadius, -toothOffsetY);
        ctx.arc(cx, -toothOffsetY, toothRadius, 0, Math.PI * 2);
        ctx.moveTo(cx + toothRadius, height + toothOffsetY);
        ctx.arc(cx, height + toothOffsetY, toothRadius, 0, Math.PI * 2);
    }
    ctx.fillStyle = '#000';
    ctx.fill();
    ctx.restore();
}

function rcRenderToBlob() {
    var src = document.querySelector('#rc-app .rc-paper');
    if (!src) return Promise.reject(new Error('小票未渲染'));
    var bgSrcUrl = window.getComputedStyle(src).backgroundImage;
    var m = bgSrcUrl.match(/url\("?([^")]+)"?\)/);
    var clone = buildStyledClone(src);
    var w = src.clientWidth;
    var h = src.clientHeight;
    return inlineBackground(clone, m ? m[1] : null)
        .then(function() { return inlineImages(clone); })
        .then(function() {
            var xhtml = new XMLSerializer().serializeToString(clone);
            var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + w + '" height="' + h + '">' +
                      '<foreignObject width="100%" height="100%">' + xhtml + '</foreignObject></svg>';
            // data: URI 加载：Chrome 对含 foreignObject 的 SVG 走 blob: URL 会污染画布
            var dataUrl = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
            return new Promise(function(resolve, reject) {
                var image = new Image();
                image.onload = function() {
                    var canvas = document.createElement('canvas');
                    canvas.width = w * EXPORT_SCALE;
                    canvas.height = h * EXPORT_SCALE;
                    var ctx = canvas.getContext('2d');
                    ctx.scale(EXPORT_SCALE, EXPORT_SCALE);
                    ctx.drawImage(image, 0, 0);
                    // Spec 28 D6（Task 128）：zigzag 启用时 canvas 层补绘锯齿；
                    // ctx 已按 EXPORT_SCALE 缩放，以 CSS 像素坐标（scale=1）绘制即等效参数同步放大
                    if (src.classList.contains('rc-zigzag')) _drawZigzagEdge(ctx, w, h, 1);
                    canvas.toBlob(function(blob) {
                        if (blob) resolve(blob); else reject(new Error('toBlob failed'));
                    }, 'image/png');
                };
                image.onerror = function() { reject(new Error('svg render failed')); };
                image.src = dataUrl;
            });
        });
}

function dateStamp() {
    var d = new Date();
    return '' + d.getFullYear() + String(d.getMonth() + 1).padStart(2, '0') +
           String(d.getDate()).padStart(2, '0');
}

function withBusy(btn, fn) {
    if (exporting) return;
    exporting = true;
    if (btn) btn.disabled = true;
    fn().catch(function(err) {
        console.error('[receipt export]', err);
        showToast('导出失败：' + (err && err.message ? err.message : '未知错误'), 'error');
    }).then(function() {
        exporting = false;
        if (btn) btn.disabled = false;
    });
}

function downloadBlob(blob) {
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = '小票-' + dateStamp() + '.png';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function() { URL.revokeObjectURL(a.href); }, 3000);
}

window.rc_downloadPng = function(btn) {
    withBusy(btn, function() {
        return rcRenderToBlob().then(function(blob) {
            downloadBlob(blob);
            showToast('PNG 已下载', 'success');
        });
    });
};

window.rc_copyImage = function(btn) {
    withBusy(btn, function() {
        if (!navigator.clipboard || typeof ClipboardItem === 'undefined') {
            // 浏览器不支持图片剪贴板 → 回退下载（价目表同款处理）
            return rcRenderToBlob().then(function(blob) {
                downloadBlob(blob);
                showToast('当前浏览器不支持复制图片，已改为下载', 'info');
            });
        }
        return rcRenderToBlob().then(function(blob) {
            return navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
        }).then(function() {
            showToast('图片已复制到剪贴板', 'success');
        }).catch(function() {
            // 剪贴板权限失败 → 回退下载
            return rcRenderToBlob().then(function(blob) {
                downloadBlob(blob);
                showToast('复制失败，已改为下载', 'info');
            });
        });
    });
};

window.rcCalc = rcCalc;
window.rcFmt = rcFmt;
window.rcRender = rcRender;
window.RC_PRESETS = RC_PRESETS;
})();

/* ═══ 编辑模式（卡 96 T23.12-14）：左栏全控件 + 右栏实时预览 + 草稿整体保存（D4）═══ */
(function() {
'use strict';

window.rcEditorInit = function() {
    var state = JSON.parse(JSON.stringify(window.RC_DRAFT || {}));
    state.items = state.items || [];
    state.meta = state.meta || {};
    state.style = state.style || {};
    var dirty = false;
    window.__rcDirty = false;   // 卡 156：脏状态提升 window——rcEditorInit 在 boosted 重进时再次调用，beforeunload 只绑一次须跨实例读取
    var sortable = null;

    function $(id) { return document.getElementById(id); }
    function esc(s) {
        return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
    function preview() { window.rcRender($('rc-app'), state); }
    function setDirty() {
        if (!dirty) { dirty = true; window.__rcDirty = true; }
        if (typeof markDataDirty === 'function') markDataDirty();
        var btn = $('rc-save-btn');
        if (btn) btn.textContent = '● 保存草稿（未保存）';
    }

    // 未保存离开提示（spec §6 防丢）——卡 156：boosted 导航重进本页会再次执行 rcEditorInit，
    // 监听只绑一次；脏状态经 window.__rcDirty 跨实例读取（旧实例闭包随旧 DOM 失效）
    if (!window.__rcBeforeUnloadBound) {
        window.__rcBeforeUnloadBound = true;
        window.addEventListener('beforeunload', function(e) {
            if (window.__rcDirty) { e.preventDefault(); e.returnValue = ''; }
        });
    }

    // ── 制品行 DOM（结构变化才重建；输入事件不重建以保焦点）──
    function multBtnHtml(it, preset) {
        // Spec 24：倍率快捷按钮（无/预设们），active 按数值匹配
        var active = Number(it.multiplier || 1) === Number(preset.value);
        return '<button type="button" class="rc-i-mult-btn' + (active ? ' active' : '') + '"' +
               ' data-mult="' + preset.value + '" data-label="' + escAttr(preset.label) + '">' +
               esc(preset.label) + ' ×' + preset.value + '</button>';
    }
    function renderItems() {
        var host = $('rc-items');
        if (!host) return;
        var presets = window.RC_MULT_PRESETS || [];
        var html = '';
        state.items.forEach(function(it, idx) {
            var gift = !!it.is_gift;
            var dis = gift ? ' disabled' : '';
            html += '<div class="rc-item-row" data-idx="' + idx + '">' +
                '<div class="rc-item-main">' +
                '<span class="rc-drag" title="拖拽排序"><i data-lucide="grip-vertical"></i></span>' +
                '<input class="rc-i-name form-input" list="rc-pl-datalist" maxlength="50" placeholder="制品名称" value="' + escAttr(it.name) + '">' +
                '<input class="rc-i-price form-input" type="number" min="0" max="999999" step="0.01" placeholder="单价" value="' + (it.price || 0) + '">' +
                '<input class="rc-i-qty form-input" type="number" min="0" max="9999" step="0.5" placeholder="数量" value="' + (it.qty == null ? 1 : it.qty) + '">' +
                '<label class="rc-check" title="赠品"><input type="checkbox" class="rc-i-gift"' + (it.is_gift ? ' checked' : '') + '>赠</label>' +
                '<button type="button" class="rc-i-del" title="删除制品"><i data-lucide="trash-2"></i></button>' +
                '</div>' +
                // Spec 24：单品倍率/折扣行（赠品行置灰）
                '<div class="rc-item-calc">' +
                '<div class="rc-calc-group">' +
                '<span class="rc-calc-label">倍率</span>' +
                '<button type="button" class="rc-i-mult-btn' + (Number(it.multiplier || 1) === 1 ? ' active' : '') + '" data-mult="1" data-label=""' + dis + '>无 ×1</button>' +
                presets.map(function(p) { return multBtnHtml(it, p); }).join('') +
                '<input class="rc-i-mult form-input" type="number" min="0.1" max="99" step="0.1" title="单品倍率（可手改）" value="' + (it.multiplier || 1) + '"' + dis + '>' +
                '</div>' +
                '<div class="rc-calc-group">' +
                '<span class="rc-calc-label">打折</span>' +
                '<select class="rc-i-dtype form-input"' + dis + '>' +
                '<option value="none"' + ((it.discount_type || 'none') === 'none' ? ' selected' : '') + '>无</option>' +
                '<option value="amount"' + (it.discount_type === 'amount' ? ' selected' : '') + '>直减金额</option>' +
                '<option value="rate"' + (it.discount_type === 'rate' ? ' selected' : '') + '>按折数</option>' +
                '</select>' +
                '<input class="rc-i-dvalue form-input" type="number" min="0" step="0.1" placeholder="' +
                (it.discount_type === 'rate' ? '如 8.8 折' : '金额') + '" value="' + (it.discount_value || 0) + '"' +
                ((it.discount_type || 'none') === 'none' || gift ? ' disabled' : '') + '>' +
                '</div>' +
                '</div>';
            (it.extras || []).forEach(function(ex, xi) {
                html += '<div class="rc-extra-edit" data-xi="' + xi + '">' +
                    '<input class="rc-x-name form-input" maxlength="50" placeholder="附加服务" value="' + escAttr(ex.name) + '">' +
                    '<input class="rc-x-price form-input" type="number" min="0" max="999999" step="0.01" placeholder="加价" value="' + (ex.price || 0) + '">' +
                    '<input class="rc-x-qty form-input" type="number" min="0" max="9999" step="0.5" placeholder="数量" value="' + (ex.qty == null ? 1 : ex.qty) + '">' +
                    '<button type="button" class="rc-x-del" title="删除附加"><i data-lucide="x"></i></button>' +
                    '</div>';
            });
            html += '<button type="button" class="rc-add-extra">＋ 附加服务</button>' +
                    '</div>';
        });
        host.innerHTML = html || '<div class="rc-tpl-empty">暂无制品，点下方「＋ 添加制品」</div>';
        if (window.lucide) lucide.createIcons();
        initSortable();
    }

    function escAttr(s) {
        return esc(s).replace(/'/g, '&#39;');
    }

    function initSortable() {
        if (typeof Sortable === 'undefined') return;
        if (sortable) { try { sortable.destroy(); } catch (e) {} }
        sortable = Sortable.create($('rc-items'), {
            handle: '.rc-drag', animation: 150, draggable: '.rc-item-row',
            onEnd: function() {
                var rows = $('rc-items').querySelectorAll('.rc-item-row');
                var next = [];
                rows.forEach(function(r) { next.push(state.items[parseInt(r.dataset.idx, 10)]); });
                state.items = next;
                renderItems(); preview(); setDirty();
            }
        });
    }

    // ── 制品区事件委托 ──
    $('rc-items').addEventListener('input', function(e) {
        var row = e.target.closest('.rc-item-row');
        if (!row) return;
        var it = state.items[parseInt(row.dataset.idx, 10)];
        if (!it) return;
        var t = e.target;
        if (t.classList.contains('rc-i-name')) it.name = t.value;
        else if (t.classList.contains('rc-i-price')) it.price = parseFloat(t.value) || 0;
        else if (t.classList.contains('rc-i-qty')) it.qty = parseFloat(t.value) || 0;
        else if (t.classList.contains('rc-i-mult')) {
            // Spec 24：倍率手改 → active 高亮同步（值匹配预设按钮才亮）
            it.multiplier = parseFloat(t.value) || 1;
            row.querySelectorAll('.rc-i-mult-btn').forEach(function(b) {
                b.classList.toggle('active', Number(b.dataset.mult) === Number(it.multiplier || 1));
            });
        }
        else if (t.classList.contains('rc-i-dvalue')) it.discount_value = parseFloat(t.value) || 0;
        var xrow = t.closest('.rc-extra-edit');
        if (xrow) {
            var ex = it.extras[parseInt(xrow.dataset.xi, 10)];
            if (ex) {
                if (t.classList.contains('rc-x-name')) ex.name = t.value;
                else if (t.classList.contains('rc-x-price')) ex.price = parseFloat(t.value) || 0;
                else if (t.classList.contains('rc-x-qty')) ex.qty = parseFloat(t.value) || 0;
            }
        }
        preview(); setDirty();
    });
    $('rc-items').addEventListener('change', function(e) {
        var row = e.target.closest('.rc-item-row');
        if (!row) return;
        var it = state.items[parseInt(row.dataset.idx, 10)];
        if (!it) return;
        if (e.target.classList.contains('rc-i-gift')) {
            it.is_gift = e.target.checked;
            if (it.is_gift) {  // 赠品不参与倍率折扣 → 归一并重建（控件置灰）
                it.multiplier = 1; it.mult_label = '';
                it.discount_type = 'none'; it.discount_value = 0;
            }
            renderItems(); preview(); setDirty();
        } else if (e.target.classList.contains('rc-i-dtype')) {
            it.discount_type = e.target.value;
            if (it.discount_type === 'none') it.discount_value = 0;
            renderItems(); preview(); setDirty();
        } else if (e.target.classList.contains('rc-i-name')) {
            // D13 datalist 联动：选中价目表制品 → 自动填单价（price_max 存在填起价）
            var opt = document.querySelector('#rc-pl-datalist option[value="' + CSS.escape(e.target.value) + '"]');
            if (opt) {
                it.price = parseFloat(opt.dataset.price) || 0;
                row.querySelector('.rc-i-price').value = it.price;
                preview(); setDirty();
            }
        }
    });
    $('rc-items').addEventListener('click', function(e) {
        var row = e.target.closest('.rc-item-row');
        if (!row) return;
        var idx = parseInt(row.dataset.idx, 10);
        var it = state.items[idx];
        if (e.target.closest('.rc-i-del')) {
            state.items.splice(idx, 1);
            renderItems(); preview(); setDirty();
        } else if (e.target.closest('.rc-x-del')) {
            var xrow = e.target.closest('.rc-extra-edit');
            it.extras.splice(parseInt(xrow.dataset.xi, 10), 1);
            renderItems(); preview(); setDirty();
        } else if (e.target.closest('.rc-add-extra')) {
            it.extras = it.extras || [];
            it.extras.push({ name: '', price: 0, qty: 1 });
            renderItems(); preview(); setDirty();
        } else if (e.target.closest('.rc-i-mult-btn')) {
            // Spec 24：倍率快捷按钮（无/商用/买断预设）→ 带值+标签，可再手改数值
            var btn = e.target.closest('.rc-i-mult-btn');
            it.multiplier = parseFloat(btn.dataset.mult) || 1;
            it.mult_label = btn.dataset.label || '';
            renderItems(); preview(); setDirty();
        }
    });
    $('rc-add-item').addEventListener('click', function() {
        state.items.push({ name: '', price: 0, qty: 1, is_gift: false, extras: [],
                           multiplier: 1, mult_label: '', discount_type: 'none', discount_value: 0 });
        renderItems(); preview(); setDirty();
    });

    // ── 店铺信息 / 计算参数：input 即重渲染 ──
    // 2026-08-13 用户需求 2b：mult_label/mult_expr 整单倍率行文案（空串 expr=不显示乘数）
    [['rc-f-shop', 'shop_name'], ['rc-f-sub', 'subtitle'], ['rc-f-no', 'order_no'],
     ['rc-f-date', 'order_date'], ['rc-f-contact', 'contact'], ['rc-f-footer', 'footer'],
     ['rc-f-multlabel', 'mult_label'], ['rc-f-multexpr', 'mult_expr']]
        .forEach(function(pair) {
            var el = $(pair[0]);
            if (!el) return;  // 防 DOM 缺失时整个编辑器初始化崩（控件可选）
            el.addEventListener('input', function() {
                state.meta[pair[1]] = this.value; preview(); setDirty();
            });
        });
    [['rc-f-mult', 'multiplier'], ['rc-f-dep', 'deposit']]
        .forEach(function(pair) {
            var el = $(pair[0]);
            if (!el) return;
            el.addEventListener('input', function() {
                state.meta[pair[1]] = parseFloat(this.value) || 0; preview(); setDirty();
            });
        });
    // Spec 24：整单折扣双形态（直减金额 / 按折数）
    function syncMetaDiscControls() {
        var t = state.meta.discount_type || 'none';
        $('rc-f-disc-type').value = t;
        $('rc-f-disc').value = state.meta.discount_value || 0;
        $('rc-f-disc').disabled = t === 'none';
        $('rc-f-disc').placeholder = t === 'rate' ? '如 8.8 折' : '金额';
    }
    $('rc-f-disc-type').addEventListener('change', function() {
        state.meta.discount_type = this.value;
        if (this.value === 'none') state.meta.discount_value = 0;
        syncMetaDiscControls(); preview(); setDirty();
    });
    $('rc-f-disc').addEventListener('input', function() {
        state.meta.discount_value = parseFloat(this.value) || 0; preview(); setDirty();
    });

    // ── 倍率快捷预设管理（2026-08-13 用户需求 2a）：内联增删改，防抖落库 settings（全局配置非本单数据）──
    var mpresets = JSON.parse(JSON.stringify(window.RC_MULT_PRESETS || []));
    var mpSaveTimer = null;
    function renderMultPresets() {
        var host = $('rc-mp-list');
        if (!host) return;
        host.innerHTML = mpresets.map(function(p, i) {
            return '<div class="rc-mp-row" data-i="' + i + '">' +
                '<input class="rc-mp-name form-input" maxlength="20" placeholder="标签（如 商用）" value="' + escAttr(p.label) + '">' +
                '<input class="rc-mp-value form-input" type="number" min="0.1" max="99" step="0.1" title="倍率值" value="' + p.value + '">' +
                '<button type="button" class="rc-x-del rc-mp-del" title="删除预设"><i data-lucide="x"></i></button>' +
                '</div>';
        }).join('') || '<div class="rc-tpl-empty">暂无预设，点下方「＋ 添加预设」</div>';
        if (window.lucide) lucide.createIcons();
    }
    function syncMultPresets() {
        // 同步制品行快捷按钮 + 防抖保存（过滤空标签行）
        window.RC_MULT_PRESETS = mpresets;
        renderItems();
        if (mpSaveTimer) clearTimeout(mpSaveTimer);
        mpSaveTimer = setTimeout(function() {
            var clean = mpresets.filter(function(p) { return String(p.label || '').trim(); })
                .map(function(p) { return { label: String(p.label).trim(), value: parseFloat(p.value) || 1 }; });
            dirtyFetch('/tools/receipt/mult-presets', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ presets: clean })
            }).then(function(r) { return r.json(); })
              .then(function(d) {
                  if (d.success) showToast('倍率预设已保存', 'success');
                  else showToast(d.error || '预设保存失败', 'error');
              })
              .catch(function() { showToast('预设保存失败', 'error'); });
        }, 600);
    }
    if ($('rc-mp-list')) {
        $('rc-mp-list').addEventListener('input', function(e) {
            var row = e.target.closest('.rc-mp-row');
            if (!row) return;
            var p = mpresets[parseInt(row.dataset.i, 10)];
            if (!p) return;
            if (e.target.classList.contains('rc-mp-name')) p.label = e.target.value;
            else if (e.target.classList.contains('rc-mp-value')) p.value = parseFloat(e.target.value) || 1;
            syncMultPresets();  // 不重建预设列表自身以保输入焦点；制品行按钮即时刷新
        });
        $('rc-mp-list').addEventListener('click', function(e) {
            var btn = e.target.closest('.rc-mp-del');
            if (!btn) return;
            var row = btn.closest('.rc-mp-row');
            mpresets.splice(parseInt(row.dataset.i, 10), 1);
            renderMultPresets(); syncMultPresets();
        });
        $('rc-mp-add').addEventListener('click', function() {
            mpresets.push({ label: '', value: 2 });
            renderMultPresets(); syncMultPresets();
        });
    }

    // ── 样式区 ──
    function syncStyleControls() {
        $('rc-f-paper').value = state.style.paper || '#fdfcf8';
        $('rc-f-ink').value = state.style.ink || '#1a1a1a';
        $('rc-f-barcode').checked = state.style.barcode !== false;
        $('rc-f-zigzag').checked = state.style.zigzag !== false;
        document.querySelectorAll('.rc-preset-card').forEach(function(b) {
            b.classList.toggle('active', b.dataset.preset === (state.style.preset || 'list'));
        });
        var mode = state.style.image_mode || 'dither';
        document.querySelectorAll('input[name="rc-imgmode"]').forEach(function(r) {
            r.checked = r.value === mode;
        });
        var thumb = $('rc-img-thumb');
        if (state.style.image_path) {
            thumb.src = '/uploads/' + state.style.image_path;
            thumb.hidden = false; $('rc-img-del').hidden = false;
        } else {
            thumb.hidden = true; $('rc-img-del').hidden = true;
        }
        $('rc-bg-img').src = state.style.bg_path ? '/uploads/' + state.style.bg_path : '/static/paper-texture.png';
        // 2026-08-13 用户需求 3：footer 插图缩略图 + 模式 radio 同步
        var fthumb = $('rc-fimg-thumb');
        if (fthumb) {
            if (state.style.footer_image_path) {
                fthumb.src = '/uploads/' + state.style.footer_image_path;
                fthumb.hidden = false; $('rc-fimg-del').hidden = false;
            } else {
                fthumb.hidden = true; $('rc-fimg-del').hidden = true;
            }
            var fmode = state.style.footer_image_mode || 'color';
            document.querySelectorAll('input[name="rc-fimgmode"]').forEach(function(r) {
                r.checked = r.value === fmode;
            });
        }
    }
    $('rc-presets').addEventListener('click', function(e) {
        var btn = e.target.closest('.rc-preset-card');
        if (!btn) return;
        state.style.preset = btn.dataset.preset;
        // 切预设 → 整组变量切换（纸色/墨色回该预设默认，可再自定义）
        state.style.paper = window.RC_PRESETS[btn.dataset.preset].paper;
        state.style.ink = window.RC_PRESETS[btn.dataset.preset].ink;
        syncStyleControls(); preview(); setDirty();
    });
    $('rc-f-paper').addEventListener('input', function() { state.style.paper = this.value; preview(); setDirty(); });
    $('rc-f-ink').addEventListener('input', function() { state.style.ink = this.value; preview(); setDirty(); });
    $('rc-f-barcode').addEventListener('change', function() { state.style.barcode = this.checked; preview(); setDirty(); });
    $('rc-f-zigzag').addEventListener('change', function() { state.style.zigzag = this.checked; preview(); setDirty(); });

    // ── footer 插图上传 / 删除（2026-08-13 用户需求 3：总计与感谢语之间；target=footer，同主图三模式管线）──
    var fimgOpSeq = 0;
    if ($('rc-fimg-upload-btn')) {
        $('rc-fimg-upload-btn').addEventListener('click', function() { $('rc-fimg-file').click(); });
        $('rc-fimg-file').addEventListener('change', function() {
            var file = this.files && this.files[0];
            this.value = '';
            if (!file) return;
            var mode = (document.querySelector('input[name="rc-fimgmode"]:checked') || {}).value || 'color';
            var fd = new FormData();
            fd.append('image', file); fd.append('mode', mode); fd.append('target', 'footer');
            var seq = ++fimgOpSeq;
            showToast('上传中…', 'info');
            dirtyFetch('/tools/receipt/upload-image', { method: 'POST', body: fd })
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    if (seq !== fimgOpSeq) return;
                    if (!d.success) { showToast(d.error || '上传失败', 'error'); return; }
                    state.style.footer_image_path = d.image_url.replace(/^\/uploads\//, '');
                    state.style.footer_image_mode = mode;
                    syncStyleControls(); preview(); setDirty();
                    var modeLabel = mode === 'gray' ? '灰度' : (mode === 'color' ? '彩色' : '抖动印刷');
                    showToast('footer 插图已上传（' + modeLabel + '）', 'success');
                })
                .catch(function() { showToast('上传失败', 'error'); });
        });
        $('rc-fimg-del').addEventListener('click', function() {
            var seq = ++fimgOpSeq;
            dirtyFetch('/tools/receipt/remove-image?target=footer', { method: 'POST' })
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    if (seq !== fimgOpSeq) return;
                    if (!d.success) { showToast(d.error || '删除失败', 'error'); return; }
                    state.style.footer_image_path = '';
                    syncStyleControls(); preview(); setDirty();
                    showToast('footer 插图已删除', 'success');
                });
        });
    }

    // ── 主图上传（mode 二选一）/ 删除（opSeq 守卫防上传/删除竞态）──
    var imgOpSeq = 0;
    $('rc-img-upload-btn').addEventListener('click', function() { $('rc-img-file').click(); });
    $('rc-img-file').addEventListener('change', function() {
        var file = this.files && this.files[0];
        this.value = '';
        if (!file) return;
        var mode = (document.querySelector('input[name="rc-imgmode"]:checked') || {}).value || 'dither';
        var fd = new FormData();
        fd.append('image', file); fd.append('mode', mode);
        var seq = ++imgOpSeq;
        showToast('上传中…', 'info');
        dirtyFetch('/tools/receipt/upload-image', { method: 'POST', body: fd })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (seq !== imgOpSeq) return;  // 期间有删除等更新操作 → 丢弃过期回调
                if (!d.success) { showToast(d.error || '上传失败', 'error'); return; }
                state.style.image_path = d.image_url.replace(/^\/uploads\//, '');
                state.style.image_mode = mode;
                syncStyleControls(); preview(); setDirty();
                var modeLabel = mode === 'gray' ? '灰度' : (mode === 'color' ? '彩色' : '抖动印刷');
                showToast('主图已上传（' + modeLabel + '）', 'success');
            })
            .catch(function() { showToast('上传失败', 'error'); });
    });
    $('rc-img-del').addEventListener('click', function() {
        var seq = ++imgOpSeq;
        dirtyFetch('/tools/receipt/remove-image', { method: 'POST' })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (seq !== imgOpSeq) return;
                if (!d.success) { showToast(d.error || '删除失败', 'error'); return; }
                state.style.image_path = '';
                syncStyleControls(); preview(); setDirty();
                showToast('主图已删除', 'success');
            });
    });

    // ── 背景上传 / 恢复默认（§3.7，同用 opSeq 守卫）──
    var bgOpSeq = 0;
    $('rc-bg-upload-btn').addEventListener('click', function() { $('rc-bg-file').click(); });
    $('rc-bg-file').addEventListener('change', function() {
        var file = this.files && this.files[0];
        this.value = '';
        if (!file) return;
        var fd = new FormData();
        fd.append('image', file);
        var seq = ++bgOpSeq;
        showToast('上传中…', 'info');
        dirtyFetch('/tools/receipt/upload-bg', { method: 'POST', body: fd })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (seq !== bgOpSeq) return;
                if (!d.success) { showToast(d.error || '上传失败', 'error'); return; }
                state.style.bg_path = d.bg_url.replace(/^\/uploads\//, '');
                syncStyleControls(); preview(); setDirty();
                showToast('背景已更换', 'success');
            })
            .catch(function() { showToast('上传失败', 'error'); });
    });
    $('rc-bg-reset').addEventListener('click', function() {
        var seq = ++bgOpSeq;
        dirtyFetch('/tools/receipt/remove-bg', { method: 'POST' })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (seq !== bgOpSeq) return;
                if (!d.success) { showToast(d.error || '操作失败', 'error'); return; }
                state.style.bg_path = '';
                syncStyleControls(); preview(); setDirty();
                showToast('已恢复默认纹理', 'success');
            });
    });

    // ── 模板：保存 / 应用 / 删除（D14 只存样式文案）──
    function renderTemplates() {
        var host = $('rc-tpl-list');
        var tpls = window.RC_TEMPLATES || [];
        if (!tpls.length) { host.innerHTML = '<div class="rc-tpl-empty">暂无模板</div>'; return; }
        host.innerHTML = tpls.map(function(t) {
            return '<div class="rc-tpl-row" data-tid="' + t.id + '">' +
                '<span class="rc-tpl-name">' + esc(t.name) + '</span>' +
                '<button type="button" class="rc-tpl-apply">应用</button>' +
                '<button type="button" class="rc-tpl-del">删除</button></div>';
        }).join('');
    }
    $('rc-tpl-save').addEventListener('click', function() {
        var name = $('rc-tpl-name').value.trim();
        if (!name) { showToast('请先输入模板名称', 'error'); return; }
        var cfg = {
            // 深拷贝：存引用会被后续编辑污染（实测：改墨色后应用模板无法还原）
            style: JSON.parse(JSON.stringify(state.style)),
            texts: { shop_name: state.meta.shop_name || '', subtitle: state.meta.subtitle || '',
                     contact: state.meta.contact || '', footer: state.meta.footer || '' }
        };
        dirtyFetch('/tools/receipt/templates', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name, config: cfg })
        }).then(function(r) { return r.json(); })
          .then(function(d) {
              if (!d.success) { showToast(d.error || '保存失败', 'error'); return; }
              window.RC_TEMPLATES = (window.RC_TEMPLATES || []).concat(
                  [{ id: d.id, name: name, config: cfg }]);
              renderTemplates();
              $('rc-tpl-name').value = '';
              showToast('模板已保存', 'success');
          });
    });
    $('rc-tpl-list').addEventListener('click', function(e) {
        var row = e.target.closest('.rc-tpl-row');
        if (!row) return;
        var tid = parseInt(row.dataset.tid, 10);
        var tpl = (window.RC_TEMPLATES || []).filter(function(t) { return t.id === tid; })[0];
        if (e.target.classList.contains('rc-tpl-del')) {
            if (!confirm('删除模板「' + (tpl && tpl.name) + '」？')) return;
            dirtyFetch('/tools/receipt/templates/' + tid + '/delete', { method: 'POST' })
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    if (!d.success) { showToast(d.error || '删除失败', 'error'); return; }
                    window.RC_TEMPLATES = (window.RC_TEMPLATES || []).filter(function(t) { return t.id !== tid; });
                    renderTemplates();
                    showToast('模板已删除', 'success');
                });
        } else if (e.target.classList.contains('rc-tpl-apply')) {
            dirtyFetch('/tools/receipt/templates/' + tid + '/apply', { method: 'POST' })
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    if (!d.success) { showToast(d.error || '应用失败', 'error'); return; }
                    // 服务端已合并落库；客户端镜像同一合并逻辑刷新状态
                    var cfg = (tpl && tpl.config) || {};
                    if (cfg.style) {
                        Object.keys(cfg.style).forEach(function(k) { state.style[k] = cfg.style[k]; });
                    }
                    if (d.bg_fallback) state.style.bg_path = '';
                    var texts = cfg.texts || {};
                    ['shop_name', 'subtitle', 'contact', 'footer'].forEach(function(k) {
                        if (texts[k] != null) state.meta[k] = texts[k];
                    });
                    fillMetaControls(); syncStyleControls(); preview(); setDirty();
                    showToast(d.bg_fallback ? '模板背景文件缺失，已用默认纹理' : '模板已应用',
                              d.bg_fallback ? 'info' : 'success');
                });
        }
    });

    // ── 草稿整体保存（D4）──
    window.rc_saveDraft = function(btn) {
        dirtyFetch('/tools/receipt/draft', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(state)
        }).then(function(r) { return r.json().then(function(d) { return { ok: r.ok, d: d }; }); })
          .then(function(res) {
              if (res.ok && res.d.success) {
                  dirty = false;
                  btn.textContent = '保存草稿';
                  showToast('草稿已保存', 'success');
              } else {
                  var msg = '保存失败';
                  if (res.d.errors && res.d.errors.length) {
                      var e0 = res.d.errors[0];
                      msg = '校验失败：' + (e0.loc || []).join('.') + ' ' + e0.msg;
                  } else if (res.d.error) { msg = res.d.error; }
                  showToast(msg, 'error');
              }
          })
          .catch(function() { showToast('保存失败', 'error'); });
    };

    function fillMetaControls() {
        $('rc-f-shop').value = state.meta.shop_name || '';
        $('rc-f-sub').value = state.meta.subtitle || '';
        $('rc-f-no').value = state.meta.order_no || '';
        $('rc-f-date').value = state.meta.order_date || '';
        $('rc-f-contact').value = state.meta.contact || '';
        $('rc-f-footer').value = state.meta.footer || '';
        // 2026-08-13 用户需求 2b：整单倍率行文案回写（null → 默认值，与渲染/后端口径一致）
        $('rc-f-multlabel').value = state.meta.mult_label == null ? '倍率' : state.meta.mult_label;
        $('rc-f-multexpr').value = state.meta.mult_expr == null ? '×{n}' : state.meta.mult_expr;
        $('rc-f-mult').value = state.meta.multiplier == null ? 1 : state.meta.multiplier;
        syncMetaDiscControls();  // Spec 24：折扣形态下拉 + 折扣值（含 disabled/placeholder）
        $('rc-f-dep').value = state.meta.deposit || 0;
    }

    // ── 初始化──
    fillMetaControls();
    syncStyleControls();
    renderItems();
    renderTemplates();
    preview();
    if (window.lucide) lucide.createIcons();
};

// ═══════════════════════════════════════════════════════════
// 预览页模板侧边栏（2026-08-13 用户需求 2a）：点击即应用并落库
// ═══════════════════════════════════════════════════════════
window.rcPreviewSidebarInit = function() {
    var listHost = document.getElementById('rc-tpl-side-list');
    var appHost = document.getElementById('rc-app');
    if (!listHost || !appHost) return;
    // 本函数独立作用域，自带转义（不依赖编辑器 IIFE 的局部 esc）
    function escHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    var tpls = window.RC_TEMPLATES || [];
    if (!tpls.length) {
        listHost.innerHTML = '<div class="rc-tpl-side-empty">暂无模板<br>可在编辑页保存</div>';
        return;
    }
    listHost.innerHTML = tpls.map(function(t) {
        return '<button type="button" class="rc-tpl-side-item" data-tid="' + t.id + '" title="' +
               escHtml(t.name) + '">' + escHtml(t.name) + '</button>';
    }).join('');
    listHost.addEventListener('click', function(e) {
        var btn = e.target.closest('.rc-tpl-side-item');
        if (!btn) return;
        var tid = parseInt(btn.dataset.tid, 10);
        var tpl = tpls.filter(function(t) { return t.id === tid; })[0];
        dirtyFetch('/tools/receipt/templates/' + tid + '/apply', { method: 'POST' })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (!d.success) { showToast(d.error || '应用失败', 'error'); return; }
                // 服务端已合并落库；客户端镜像同一合并逻辑（与编辑页 apply 同口径）
                var cfg = (tpl && tpl.config) || {};
                if (cfg.style) {
                    Object.keys(cfg.style).forEach(function(k) { window.RC_DRAFT.style[k] = cfg.style[k]; });
                }
                if (d.bg_fallback) window.RC_DRAFT.style.bg_path = '';
                var texts = cfg.texts || {};
                ['shop_name', 'subtitle', 'contact', 'footer'].forEach(function(k) {
                    if (texts[k] != null) window.RC_DRAFT.meta[k] = texts[k];
                });
                rcRender(appHost, window.RC_DRAFT);
                listHost.querySelectorAll('.rc-tpl-side-item').forEach(function(b) {
                    b.classList.toggle('active', b === btn);
                });
                showToast('已切换到模板「' + (tpl ? tpl.name : '') + '」', 'success');
            })
            .catch(function() { showToast('应用失败', 'error'); });
    });
};
})();
