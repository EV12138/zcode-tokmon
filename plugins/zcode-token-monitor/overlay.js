/* tokmon overlay —— ZCode 输入框下方的 token 用量/速率悬浮条（渲染进程，IIFE，零依赖）。
 *
 * 挂载方式：position:fixed 胶囊条，跟随 composer（textarea/contenteditable）矩形，
 *   放在输入卡片下方 6px，绝不覆盖输入框中心矩形（覆盖会导致 Electron 判定输入框
 *   不可见而闪烁——原插件 design-notes 的坑）。
 * 主题：全部引用客户端语义变量 var(--color-*, 兜底值)，自动适配亮/暗主题。
 * 样式表挂在 bar 元素内部而非 <head>：React 会清理 head 里的外来 style。
 * 热更新：inject-main 重新 eval 本文件时，旧实例通过 __tokmonLoaded 标记自清理。
 * 数据：window.__tokmonUpdate(payload) 由主进程泵推送
 *   payload = {local: snapshot(+delta), remote: {key: {ok,label,latency_ms,data|error,ageMs}}, ts}
 */
(function () {
  "use strict";

  var VERSION = "tokmon-bar-v1";

  function cleanup() {
    try { clearInterval(window.__tokmonCoordTimer); } catch (e) { }
    try { clearTimeout(tipTimer); } catch (e) { }
    try {
      if (window.__tokmonPointerHandler) {
        document.removeEventListener("mousemove", window.__tokmonPointerHandler, true);
        window.__tokmonPointerHandler = null;
      }
    } catch (e) { }
    try { hideTip(); } catch (e) { }
    try {
      if (window.__tokmonWinHide) {
        window.removeEventListener("scroll", window.__tokmonWinHide, true);
        window.removeEventListener("resize", window.__tokmonWinHide);
        window.__tokmonWinHide = null;
      }
    } catch (e) { }
    var olds = document.querySelectorAll(".tokmon-root,.tokmon-panel,.tokmon-tip");
    for (var i = 0; i < olds.length; i++) olds[i].remove();
    /* instances 与 __tokmonInstances 是同一数组引用，必须原地遍历/清空 */
    try {
      var arr = window.__tokmonInstances || [];
      for (var k = 0; k < arr.length; k++) { try { arr[k].dispose(); } catch (e) { } }
      arr.length = 0;
    } catch (e) { }
  }
  if (window.__tokmonLoaded) cleanup();
  window.__tokmonLoaded = true;

  var payload = null;
  var instances = [];
  /* 主进程按窗口上报的活跃会话 id（原生 IPC zcode:sync-active-task-session） */
  var activeSid = window.__tokmonActiveSid || "";
  /* 当前聊天窗口选中的模型（切换后立即生效，不必等新请求落库） */
  var selectedModel = null, selectedKey = "";
  var activeComposer = null;
  var LAST_SESSION_PREFIX = "zcode-v4-last-session:v1:";
  var MODEL_SELECTION_PREFIX = "zcode-model-selection-recent-v1:";

  /* ---------- 格式化 ---------- */
  function fmtTok(n) {
    n = Number(n) || 0;
    if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(n);
  }
  function fmtTps(t) { return (t && t > 0) ? (Math.round(t * 10) / 10) : "—"; }

  /* ---------- 数据取值 ---------- */
  function localPart() { return (payload && payload.local) || {}; }
  function remoteList() {
    var r = (payload && payload.remote) || {}, out = [];
    for (var k in r) if (Object.prototype.hasOwnProperty.call(r, k)) { r[k]._key = k; out.push(r[k]); }
    out.sort(function (a, b) { return (a.label || "").localeCompare(b.label || ""); });
    return out;
  }
  function runningAny(sessList, sid) {
    var list = sessList || [];
    if (sid) {
      for (var i = 0; i < list.length; i++) {
        if (list[i].sid === sid && list[i].running) return list[i];
      }
      return null;
    }
    for (var j = 0; j < list.length; j++) if (list[j].running) return list[j];
    return null;
  }
  /* 本 pane 的会话：优先按 composer 祖先链 data-session-id 匹配活跃会话；
   * 匹配不到（非活跃/找不到属性）退回全局 current。 */
  function sessIdOf(composer) {
    for (var p = composer; p && p !== document.body; p = p.parentElement) {
      if (p.hasAttribute && p.hasAttribute("data-session-id")) return p.getAttribute("data-session-id") || "";
    }
    return "";
  }
  /* 会话数据来源，优先级：本地 sidStats（精确）> 本地活跃列表 > 远端 sidStats > 远端活跃列表。
   * 本地库与远端库的会话 id 不重叠，切换本地/远端会话必须按 id 分流。 */
  function sessionView(sid) {
    var loc = localPart();
    if (!sid) return null;
    var st = loc.sidStats && loc.sidStats[sid];
    if (st) return { sess: st, src: null, hostToday: loc.today };
    var sess = loc.sessions || [];
    for (var i = 0; i < sess.length; i++) {
      if (sess[i].sid === sid) return { sess: sess[i], src: null, hostToday: loc.today };
    }
    var remotes = remoteList();
    for (var j = 0; j < remotes.length; j++) {
      var r = remotes[j];
      if (!r.ok) continue;
      var d = r.data || {};
      var rs = (d.sidStats && d.sidStats[sid]) || null;
      if (!rs) {
        var list = d.sessions || [];
        for (var k = 0; k < list.length; k++) {
          if (list[k].sid === sid) { rs = list[k]; break; }
        }
      }
      if (rs) return { sess: rs, src: r, hostToday: d.today || {} };
    }
    return null;
  }
  /* 本窗口/pane 的会话归属：composer 祖先链 > 主进程 IPC 上报 > 空（退全局） */
  function mySessionSid(composer) {
    return sessIdOf(composer) || activeSid || "";
  }
  /* 从模型选择按钮读取当前模型（ZCode 的 data-testid 稳定标记） */
  function selectionFromPicker() {
    function visible(node) {
      var r = node.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < window.innerHeight;
    }
    var el = null;
    if (activeComposer) {
      var node = activeComposer;
      for (var up = 0; up < 6 && node; up++, node = node.parentElement) {
        el = node.querySelector && node.querySelector('[data-testid="chat-model-select-trigger"]');
        if (el) break;
      }
    }
    if (!el) {
      var list = document.querySelectorAll('[data-testid="chat-model-select-trigger"]');
      for (var i = 0; i < list.length; i++) {
        if (visible(list[i])) { el = list[i]; break; }
      }
    }
    if (!el) return null;
    var label = (el.getAttribute("aria-label") || el.innerText || "").trim();
    if (!label) return null;
    label = label.split("\n")[0].replace(/[⌄▾\s]+$/, "").trim();
    var id = label.split("/").pop().trim();
    if (!id || (!/\//.test(label) && /模型|选择|model/i.test(id))) return null;
    return { modelId: id, providerId: "", label: label, source: "picker" };
  }
  /* 从 localStorage 读当前会话的模型选择（带精确 providerId） */
  function selectionFromStorage(sid) {
    try {
      if (!sid) return null;
      var suffix = "";
      for (var i = 0; i < localStorage.length; i++) {
        var key = localStorage.key(i);
        if (key && key.indexOf(LAST_SESSION_PREFIX) === 0 && localStorage.getItem(key) === sid) {
          suffix = key.slice(LAST_SESSION_PREFIX.length);
          break;
        }
      }
      if (!suffix) return null;
      var raw = localStorage.getItem(MODEL_SELECTION_PREFIX + suffix);
      if (!raw) return null;
      var sel = (JSON.parse(raw) || {}).modelSelection;
      if (!sel || !sel.modelId) return null;
      return { modelId: sel.modelId, providerId: sel.providerId || "", label: sel.modelId, source: "storage" };
    } catch (e) { return null; }
  }
  function readSelectedModel() {
    var picker = selectionFromPicker();
    var stored = selectionFromStorage(activeSid);
    if (picker && stored && picker.modelId.toLowerCase() === stored.modelId.toLowerCase()) {
      return { modelId: stored.modelId, providerId: stored.providerId, label: picker.label, source: "storage" };
    }
    return picker || stored;
  }
  /* 切换模型后立即重算上下文；只在选择真正变化时重绘 */
  function refreshSelectedModel() {
    var sel = readSelectedModel();
    var key = sel ? ((sel.providerId || "") + "|" + sel.modelId).toLowerCase() : "";
    if (key === selectedKey) return;
    selectedKey = key;
    selectedModel = sel;
    for (var i = 0; i < instances.length; i++) {
      try { instances[i].render(); } catch (e) { }
    }
    refreshPanel();
  }
  /* 模型上下文窗口：优先用数据泵按该会话模型配置解析出的 contextWindow；
   * 没带（旧远端 agent）时才回退到 settings.contextWindows 映射。
   * provider 已知时优先匹配 “provider/model”，避免同名模型串窗口。 */
  function contextWindowFor(model, hint, provider, winsOverride) {
    var h = Number(hint);
    if (isFinite(h) && h > 0) return h;
    if (!model) return 0;
    var wins = winsOverride ||
      (payload && payload.settings && payload.settings.contextWindows) || {};
    var m = String(model).toLowerCase();
    if (provider) {
      var pk = String(provider).toLowerCase() + "/" + m;
      for (var p in wins) {
        if (String(p).toLowerCase() === pk) return Number(wins[p]) || 0;
      }
    }
    for (var e in wins) {
      if (String(e).toLowerCase() === m) return Number(wins[e]) || 0;
    }
    if (wins[model] != null) return Number(wins[model]) || 0;
    var best = 0, bestLen = -1;
    for (var k in wins) {
      var key = String(k).toLowerCase();
      if (key && key.indexOf("/") < 0 && m.indexOf(key) >= 0 && key.length > bestLen) {
        best = Number(wins[k]) || 0;
        bestLen = key.length;
      }
    }
    return best;
  }
  /* 当前数据源的窗口表：远端优先用远主机 agent 带回的本机模型配置 */
  function contextWindowsFor(view, isRemote) {
    var base = (payload && payload.settings && payload.settings.contextWindows) || {};
    if (!isRemote || !view || !view.src || !view.src.data) return base;
    var remote = view.src.data.contextWindows;
    if (!remote) return base;
    var merged = {};
    for (var k in base) merged[k] = base[k];
    for (var r in remote) merged[r] = remote[r];
    return merged;
  }
  function fmtPct(v) {
    var n = Number(v);
    return (v == null || !isFinite(n)) ? "—" : n.toFixed(1) + "%";
  }
  /* 缓存命中率：cache_read / input；input 为 0 时返回 null（不显示单位） */
  function hitPct(cache, input) {
    return input ? (cache || 0) * 100.0 / input : null;
  }
  function hitUnit(p) {
    return (p == null || isNaN(p)) ? "" : "· " + fmtPct(p);
  }
  /* 速率色阶（用户指定）：极慢 <15 / 慢 15~30 / 一般 30~50 / 良好 50~80 / 快 80~150 / 极快 >150 */
  var RATE_TIERS = [
    {max: 15, color: "#DC2626", label: "极慢", range: "< 15 t/s"},
    {max: 30, color: "#EA580C", label: "慢", range: "15 ~ 30 t/s"},
    {max: 50, color: "#F59E0B", label: "一般", range: "30 ~ 50 t/s"},
    {max: 80, color: "#84CC16", label: "良好", range: "50 ~ 80 t/s"},
    {max: 150, color: "#16A34A", label: "快", range: "80 ~ 150 t/s"},
    {max: Infinity, color: "#0891B2", label: "极快", range: "> 150 t/s"}
  ];
  function rateTone(tps) {
    if (!tps || tps <= 0) return {color: "", label: "—", range: "暂无数据"};
    if (tps < 15) return RATE_TIERS[0];
    if (tps < 30) return RATE_TIERS[1];
    if (tps < 50) return RATE_TIERS[2];
    if (tps < 80) return RATE_TIERS[3];
    if (tps <= 150) return RATE_TIERS[4];
    return RATE_TIERS[5];
  }
  /* 上下文占用色阶：充足 <50% / 正常 50~70% / 偏高 70~85% / 接近上限 85~95% / 即将超限 >=95% */
  var CONTEXT_TIERS = [
    {max: 30, color: "#16A34A", label: "充足", range: "< 30%"},
    {max: 50, color: "#84CC16", label: "正常", range: "30% ~ 50%"},
    {max: 70, color: "#F59E0B", label: "偏高", range: "50% ~ 70%"},
    {max: 85, color: "#EA580C", label: "接近上限", range: "70% ~ 85%"},
    {max: Infinity, color: "#DC2626", label: "即将超限", range: "≥ 85%"}
  ];
  function contextTone(pct) {
    if (pct == null || isNaN(pct)) return {color: "", label: "未知", range: "未匹配窗口"};
    for (var i = 0; i < CONTEXT_TIERS.length; i++) {
      if (pct < CONTEXT_TIERS[i].max) return CONTEXT_TIERS[i];
    }
    return CONTEXT_TIERS[CONTEXT_TIERS.length - 1];
  }
  /* 色块图例：色块 + 等级 + 范围，当前档位高亮 */
  function tierLegend(tiers, currentLabel, title) {
    return {
      title: title,
      legend: tiers.map(function (t) {
        return {color: t.color, label: t.label, range: t.range, current: t.label === currentLabel};
      })
    };
  }

  /* ---------- DOM 骨架 ---------- */
  var CSS = (
    /* 基础：跟随客户端主题（--color-*），数字等宽对齐，留白优先 */
    ".tokmon-root{position:fixed;box-sizing:border-box;z-index:50;" +
    "display:flex;align-items:center;justify-content:center;white-space:nowrap;max-width:96vw;" +
    "font-family:var(--font-mono,ui-monospace,Consolas,Menlo,monospace);" +
    "font-size:11.5px;line-height:1.2;letter-spacing:.01em;" +
    "font-variant-numeric:tabular-nums;font-feature-settings:'tnum' 1;" +
    "color:var(--color-foreground-subtle,#8791a3);" +
    "border:1px solid var(--color-border,rgba(255,255,255,.07));border-radius:10px;" +
    "padding:5px 10px;user-select:none;cursor:default;" +
    "backdrop-filter:blur(14px) saturate(1.3);-webkit-backdrop-filter:blur(14px) saturate(1.3);" +
    "box-shadow:0 4px 14px rgba(0,0,0,.28)}" +
    /* in-card：嵌入输入卡片末行，去掉装饰、只留呼吸感 */
    ".tokmon-root.tokmon-in{position:static;width:100%;max-width:none;z-index:auto;" +
    "background:none;border:0;border-radius:0;box-shadow:none;" +
    "backdrop-filter:none;-webkit-backdrop-filter:none;padding:6px 2px 3px;opacity:.96;" +
    "justify-content:flex-start}" +
    ".tokmon-root.tokmon-in .tokmon-end{margin-left:auto}" +
    /* 数据组：图标 + 数值 + 单位，组间用细分隔线 */
    ".tokmon-grp{display:inline-flex;align-items:center;gap:6px;padding:1px 8px;" +
    "border-radius:7px;transition:background-color .12s ease,color .12s ease}" +
    ".tokmon-grp.tokmon-cfg{cursor:pointer;padding:1px 6px;color:var(--color-foreground-subtle,#7e8899)}" +
    ".tokmon-grp.tokmon-cfg:hover{background:var(--color-hover,rgba(255,255,255,.06));" +
    "color:var(--color-foreground,#e9edf4)}" +
    ".tokmon-ic{display:inline-flex;align-items:center;justify-content:center;" +
    "color:var(--color-foreground-subtle,#7e8899);opacity:.8;flex:0 0 auto}" +
    ".tokmon-ic svg{display:block}" +
    ".tokmon-val{font-weight:600;color:var(--color-foreground,#e9edf4)}" +
    ".tokmon-unit{font-size:10px;font-weight:400;margin-left:-2px;" +
    "color:var(--color-foreground-subtle,#7e8899)}" +
    ".tokmon-div{width:1px;height:10px;flex:0 0 auto;margin:0 2px;opacity:.7;" +
    "background:var(--color-border,rgba(255,255,255,.09))}" +
    ".tokmon-live .tokmon-val,.tokmon-live .tokmon-ic{color:var(--color-success,#4ade80)}" +
    ".tokmon-live .tokmon-ic{opacity:1}" +
    ".tokmon-err .tokmon-val,.tokmon-err .tokmon-ic{color:var(--color-danger,#f87171)}" +
    ".tokmon-warn .tokmon-val,.tokmon-warn .tokmon-ic{color:var(--color-warning,#fbbf24)}" +
    ".tokmon-grp.tokmon-rate .tokmon-val{color:var(--tokmon-rate-color,var(--color-foreground,#e9edf4))}" +
    ".tokmon-grp.tokmon-rate .tokmon-ic{color:var(--tokmon-rate-color,var(--color-foreground-subtle,#7e8899))}" +
    ".tokmon-grp.tokmon-ctx .tokmon-val{color:var(--tokmon-ctx-color,var(--color-foreground,#e9edf4))}" +
    ".tokmon-grp.tokmon-ctx .tokmon-ic{color:var(--tokmon-ctx-color,var(--color-foreground-subtle,#7e8899))}" +
    ".tokmon-dot{width:5px;height:5px;border-radius:50%;background:currentColor;flex:0 0 auto}" +
    ".tokmon-pulse{animation:tokmon-p 1.3s ease-in-out infinite}" +
    ".tokmon-tip{position:fixed;z-index:9999;pointer-events:none;max-width:300px;" +
    "padding:8px 10px;border-radius:9px;font-size:11px;line-height:1.55;text-align:left;" +
    "font-family:-apple-system,'PingFang SC',system-ui,sans-serif;white-space:normal;" +
    "color:var(--color-foreground,#e9edf4);background:var(--color-background,#12151d);" +
    "border:1px solid var(--color-border,rgba(255,255,255,.10));" +
    "box-shadow:0 10px 28px rgba(0,0,0,.45);opacity:0}" +
    ".tokmon-tip.on{opacity:1}" +
    ".tokmon-tip-h{font-size:11.5px;font-weight:600;letter-spacing:.02em;margin-bottom:3px}" +
    ".tokmon-tip-l{color:var(--color-foreground-subtle,#8791a3);margin-top:2px}" +
    ".tokmon-tip-legend{margin-top:6px;padding-top:6px;border-top:1px solid var(--color-border,rgba(255,255,255,.08))}" +
    ".tokmon-tip-legend-title{font-size:10px;font-weight:600;letter-spacing:.03em;color:var(--color-foreground-subtle,#8791a3);margin-bottom:3px}" +
    ".tokmon-tip-legend-row{display:flex;align-items:center;gap:6px;padding:1px 3px;border-radius:4px;font-size:10.5px;line-height:1.5}" +
    ".tokmon-tip-legend-cur{background:var(--color-hover,rgba(255,255,255,.06))}" +
    ".tokmon-tip-swatch{width:9px;height:9px;border-radius:2px;flex:0 0 auto;box-shadow:inset 0 0 0 1px rgba(255,255,255,.15)}" +
    ".tokmon-tip-legend-label{color:var(--color-foreground-subtle,#8791a3)}" +
    ".tokmon-tip-legend-cur .tokmon-tip-legend-label{color:var(--color-foreground,#e9edf4);font-weight:600}" +
    ".tokmon-tip-legend-range{margin-left:auto;color:var(--color-foreground-subtle,#8791a3);font-variant-numeric:tabular-nums}" +
    "@keyframes tokmon-p{0%,100%{opacity:1}50%{opacity:.45}}" +
    /* 明细浮层 */
    ".tokmon-panel{position:fixed;z-index:60;min-width:250px;max-width:min(420px,92vw);" +
    "font-size:12px;line-height:1.5;color:var(--color-foreground,#e9edf4);" +
    "background:var(--color-background,#12151d);border:1px solid var(--color-border,rgba(255,255,255,.1));" +
    "border-radius:12px;padding:12px 14px;box-shadow:0 16px 40px rgba(0,0,0,.48);" +
    "max-height:60vh;overflow:auto;white-space:normal;font-variant-numeric:tabular-nums}" +
    ".tokmon-panel h4{margin:0 0 6px;font-size:11px;font-weight:600;letter-spacing:.04em;" +
    "color:var(--color-foreground-subtle,#8791a3)}" +
    ".tokmon-panel .row{display:flex;justify-content:space-between;gap:16px;padding:2px 0}" +
    ".tokmon-panel .row span:first-child{color:var(--color-foreground-subtle,#8791a3)}" +
    ".tokmon-panel .row span:last-child{font-weight:600}" +
    ".tokmon-panel .sub{margin-top:10px;padding-top:8px;" +
    "border-top:1px solid var(--color-border,rgba(255,255,255,.08))}" +
    ".tokmon-panel .muted{color:var(--color-foreground-subtle,#7e8899);font-weight:400}"
  );

  /* 线性图标（跟随 currentColor，避免彩色 emoji 与客户端风格冲突） */
  var ICONS = {
    bolt: '<svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor" aria-hidden="true">' +
      '<path d="M9.2 1.4 3.8 9.1h3.1l-.6 5.5 5.9-7.7H9z"/></svg>',
    chat: '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" ' +
      'stroke-width="1.5" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M3 3.6h10a1 1 0 0 1 1 1v5.2a1 1 0 0 1-1 1H7.6L4.7 13v-2.2H3a1 1 0 0 1-1-1V4.6a1 1 0 0 1 1-1z"/></svg>',
    box: '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" ' +
      'stroke-width="1.4" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M8 1.8 14 5v6l-6 3.2L2 11V5z"/><path d="M2 5l6 3 6-3M8 8v6"/></svg>',
    cloud: '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" ' +
      'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      '<path d="M4.6 12.4h6.9a2.6 2.6 0 0 0 .5-5.2 4 4 0 0 0-7.7-.6 2.7 2.7 0 0 0 .3 5.8z"/></svg>',
    gauge: '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" ' +
      'stroke-width="1.4" stroke-linecap="round" aria-hidden="true">' +
      '<path d="M2.4 11.6a6 6 0 1 1 11.2 0"/><path d="M8 11.6 10.7 7.4"/></svg>',
    globe: '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" ' +
      'stroke-width="1.3" stroke-linecap="round" aria-hidden="true">' +
      '<circle cx="8" cy="8" r="5.6"/><path d="M2.4 8h11.2"/>' +
      '<path d="M8 2.4c1.5 1.6 2.3 3.5 2.3 5.6s-.8 4-2.3 5.6M8 2.4C6.5 4 5.7 5.9 5.7 8s.8 4 2.3 5.6"/></svg>',
    sliders: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" ' +
      'stroke-width="1.5" stroke-linecap="round" aria-hidden="true">' +
      '<path d="M2 5h12M2 11h12"/>' +
      '<circle cx="6" cy="5" r="1.7" fill="currentColor" stroke="none"/>' +
      '<circle cx="10.5" cy="11" r="1.7" fill="currentColor" stroke="none"/></svg>'
  };

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function divider() { return el("span", "tokmon-div"); }
  /* 清空条内容但保留内嵌 <style>——CSS 挂在条内部（防 React 清理 head），
   * 之前无条件清空会把样式表一起删掉，导致整条退回默认样式。 */
  function clearBar(bar) {
    var keep = null;
    for (var i = 0; i < bar.childNodes.length; i++) {
      if (bar.childNodes[i].tagName === "STYLE") { keep = bar.childNodes[i]; break; }
    }
    while (bar.firstChild) bar.removeChild(bar.firstChild);
    if (keep) bar.appendChild(keep);
  }
  /* 一个数据组：图标 + 数值 + 单位；opts: tone/pulse/dot/title/click/clickable */
  /* ---------- 悬浮说明卡片 ---------- */
  var tipEl = null, tipTimer = 0, tipTarget = null, tipRaf = 0;
  var lastPointer = { x: -1, y: -1 };

  function ensureTipEl() {
    if (tipEl && tipEl.isConnected) return tipEl;
    tipEl = el("div", "tokmon-tip");
    document.body.appendChild(tipEl);
    return tipEl;
  }
  function hideTip() {
    clearTimeout(tipTimer);
    tipTimer = 0;
    tipTarget = null;
    if (tipRaf) { cancelAnimationFrame(tipRaf); tipRaf = 0; }
    if (tipEl) tipEl.classList.remove("on");
  }
  function showTip(target, sections) {
    var t = ensureTipEl();
    tipTarget = target;
    t.textContent = "";
    for (var i = 0; i < sections.length; i++) {
      var sec = sections[i];
      if (sec && sec.legend) {
        var box = el("div", "tokmon-tip-legend");
        if (sec.title) box.appendChild(el("div", "tokmon-tip-legend-title", sec.title));
        sec.legend.forEach(function (item) {
          var row = el("div", "tokmon-tip-legend-row" + (item.current ? " tokmon-tip-legend-cur" : ""));
          var sw = el("span", "tokmon-tip-swatch");
          sw.style.background = item.color;
          row.appendChild(sw);
          row.appendChild(el("span", "tokmon-tip-legend-label", item.label));
          row.appendChild(el("span", "tokmon-tip-legend-range", item.range));
          box.appendChild(row);
        });
        t.appendChild(box);
      } else {
        t.appendChild(el("div", i === 0 ? "tokmon-tip-h" : "tokmon-tip-l", sec == null ? "" : String(sec)));
      }
    }
    t.classList.remove("on");
    t.style.left = "-9999px";
    t.style.top = "-9999px";
    var tw = t.offsetWidth, th = t.offsetHeight;
    var r = target.getBoundingClientRect();
    var left = Math.max(8, Math.min(r.left + r.width / 2 - tw / 2, window.innerWidth - tw - 8));
    var top = r.top - th - 8;
    if (top < 8) top = Math.min(r.bottom + 8, window.innerHeight - th - 8);
    t.style.left = left + "px";
    t.style.top = Math.max(8, top) + "px";
    t.classList.add("on");
  }
  /* 视口滚动/尺寸变化：提示开着就跟着目标重定位，不再一律关掉。
   * 流式输出时聊天区会持续自动滚动，旧逻辑会把悬停提示瞬间隐藏。 */
  function repositionTip(target) {
    if (!tipEl || !target || !target.isConnected) return;
    var r = target.getBoundingClientRect();
    if (r.bottom < 0 || r.top > window.innerHeight) { hideTip(); return; }
    var tw = tipEl.offsetWidth, th = tipEl.offsetHeight;
    var left = Math.max(8, Math.min(r.left + r.width / 2 - tw / 2, window.innerWidth - tw - 8));
    var top = r.top - th - 8;
    if (top < 8) top = Math.min(r.bottom + 8, window.innerHeight - th - 8);
    tipEl.style.left = left + "px";
    tipEl.style.top = Math.max(8, top) + "px";
  }
  function onViewportChange() {
    if (tipEl && tipEl.classList.contains("on") && tipTarget && tipTarget.isConnected) {
      if (tipRaf) return;
      tipRaf = requestAnimationFrame(function () {
        tipRaf = 0;
        if (tipEl && tipEl.classList.contains("on")) repositionTip(tipTarget);
      });
      return;
    }
    hideTip();
  }
  function scheduleTip(target, sections) {
    clearTimeout(tipTimer);
    tipTimer = setTimeout(function () { showTip(target, sections); }, 140);
  }
  function bindBarTips(bar) {
    /* 组级 mouseenter/mouseleave 在 makeGroup 里绑定；这里只处理点击抑制 */
    bar.addEventListener("mousedown", function () {
      bar.__tipSuppressed = true;
      hideTip();
    });
    bar.addEventListener("mouseleave", function () {
      bar.__tipSuppressed = false;
    });
  }
  function refreshOpenTip(bar) {
    var g = bar.__tipTarget;
    if (g && g.__tip && tipEl && tipEl.classList.contains("on")) showTip(g, g.__tip());
  }
  /* 条被 React 重插/重建后，指针不动时 mouseenter 不会重发；按指针位置补一次 */
  function restoreTipAtPointer(bar) {
    if (!bar || bar.__tipSuppressed) return;
    if (lastPointer.x < 0 || lastPointer.y < 0) return;
    var node = document.elementFromPoint(lastPointer.x, lastPointer.y);
    var g = node && node.closest ? node.closest(".tokmon-grp") : null;
    if (!g || !bar.contains(g) || !g.__tip) return;
    if (bar.__tipTarget === g && tipEl && tipEl.classList.contains("on")) return;
    bar.__tipTarget = g;
    scheduleTip(g, g.__tip());
  }

  /* ---------- 条内数据组（创建一次，后续只更新数值） ---------- */
  function makeGroup(iconHtml, unitText, opts) {
    opts = opts || {};
    var g = el("span", "tokmon-grp" + (opts.clickable ? " tokmon-cfg" : "") + (opts.end ? " tokmon-end" : ""));
    var ui = { g: g };
    if (iconHtml) {
      ui.ic = el("span", "tokmon-ic");
      ui.ic.innerHTML = iconHtml;
      g.appendChild(ui.ic);
    }
    if (opts.dot) {
      ui.dot = el("span", "tokmon-dot");
      g.appendChild(ui.dot);
    }
    ui.val = el("span", "tokmon-val", "");
    g.appendChild(ui.val);
    ui.unit = el("span", "tokmon-unit", unitText || "");
    g.appendChild(ui.unit);
    /* 用 mouseenter/leave（不冒泡、不受子节点文本更新影响）替代 mouseover/out，
     * 避免任务执行时数字刷新触发 mouseout 把提示关掉。 */
    g.addEventListener("mouseenter", function () {
      var bar = g.closest(".tokmon-root");
      if (!bar || bar.__tipSuppressed) return;
      if (bar.__tipTarget === g && tipEl && tipEl.classList.contains("on")) return;
      bar.__tipTarget = g;
      if (g.__tip) scheduleTip(g, g.__tip());
      else hideTip();
    });
    g.addEventListener("mouseleave", function () {
      var bar = g.closest(".tokmon-root");
      if (!bar) return;
      bar.__tipSuppressed = false;
      if (bar.__tipTarget === g) { bar.__tipTarget = null; hideTip(); }
    });
    return ui;
  }
  function setGroup(ui, value, unit, o) {
    o = o || {};
    ui.g.style.display = "";
    ui.val.textContent = value == null ? "" : String(value);
    ui.unit.textContent = unit || "";
    ui.g.classList.toggle("tokmon-live", !!o.live);
    ui.g.classList.toggle("tokmon-err", !!o.err);
    ui.g.classList.toggle("tokmon-warn", !!o.warn);
    if (ui.ic) ui.ic.classList.toggle("tokmon-pulse", !!o.pulse);
    if (ui.dot) ui.dot.style.color = o.dot || "";
    ui.g.__tip = o.tip
      ? (function (secs) { return function () { return secs; }; })(o.tip)
      : null;
  }
  function createBarUI(bar) {
    clearBar(bar);
    var ui = {};
    ui.rate = makeGroup(ICONS.bolt, "t/s");
    ui.rate.g.classList.add("tokmon-rate");
    bar.appendChild(ui.rate.g);
    ui.sep1 = divider(); bar.appendChild(ui.sep1);
    ui.session = makeGroup(ICONS.chat, "");
    bar.appendChild(ui.session.g);
    ui.sep2 = divider(); bar.appendChild(ui.sep2);
    ui.ctx = makeGroup(ICONS.gauge, "");
    ui.ctx.g.classList.add("tokmon-ctx");
    bar.appendChild(ui.ctx.g);
    ui.sep3 = divider(); bar.appendChild(ui.sep3);
    ui.today = makeGroup(ICONS.box, "");
    bar.appendChild(ui.today.g);
    ui.sep4 = divider(); bar.appendChild(ui.sep4);
    ui.allToday = makeGroup(ICONS.globe, "");     // 本机 + 所有 SSH 远程的今日合计
    bar.appendChild(ui.allToday.g);
    ui.sep5 = divider(); bar.appendChild(ui.sep5);
    ui.remote = makeGroup(ICONS.cloud, "", { dot: true });
    bar.appendChild(ui.remote.g);
    ui.settings = makeGroup(ICONS.sliders, "", { clickable: true, end: true });
    bar.appendChild(ui.settings.g);
    ui.settings.g.__tip = function () {
      return ["查看明细", "打开或关闭明细面板：当前会话、今日构成与各远程主机详情。"];
    };
    ui.settings.g.addEventListener("click", function () {
      togglePanel(ui.settings.g, bar.__sid);   // 以齿轮为锚点：弹窗贴着按钮，不跑到卡片左缘
    });
    return ui;
  }

  /* ---------- 明细浮层 ---------- */
  var panel = null, panelAnchor = null, panelSid = null;
  function closePanel() { if (panel) { panel.remove(); panel = null; } }
  function refreshPanel() {
    if (!panel) return;
    var anchor = panelAnchor, sid = panelSid;
    closePanel();
    togglePanel(anchor, sid);
  }
  function togglePanel(anchor, sid) {
    if (panel) { closePanel(); return; }
    panelAnchor = anchor; panelSid = sid;
    panel = el("div", "tokmon-panel");
    document.body.appendChild(panel);

    var loc = localPart();
    var view = sid ? sessionView(sid) : null;
    var cur = (view && view.sess) || loc.current || null;
    var isRemote = !!(view && view.src);
    var today = (isRemote && view.hostToday) ? view.hostToday : (loc.today || {});
    var baseModel = cur && cur.model;
    var selModel = (selectedModel && selectedModel.modelId) || "";
    var selProvider = selectedModel ? (selectedModel.providerId || "") : "";
    var selMatches = !!(selModel && baseModel &&
      selModel.toLowerCase() === String(baseModel).toLowerCase());
    var ctxModel = selModel || baseModel;
    var ctxWin = contextWindowFor(ctxModel, (!selModel || selMatches) ? (cur && cur.contextWindow) : 0,
      selModel ? selProvider : "", contextWindowsFor(view, isRemote));
    var ctxRawPct = (ctxWin && cur && cur.context) ? cur.context * 100.0 / ctxWin : null;
    var ctxRawR = (ctxRawPct == null) ? null : Math.round(ctxRawPct * 10) / 10;
    var ctxTone = contextTone(ctxRawR);   // 颜色按实际占用率分级
    var remotes = remoteList();
    var remoteToday = 0, remoteOk = 0, remoteErr = 0, remoteCache = 0, remoteInput = 0;
    remotes.forEach(function (r) {
      if (r.ok) {
        remoteOk++;
        var rt = (r.data || {}).today || {};
        remoteToday += rt.total || 0;
        remoteCache += rt.cache_read || 0;
        remoteInput += rt.input || 0;
      } else remoteErr++;
    });
    var localToday = (loc.today || {}).total || 0;
    var localTodayCache = (loc.today || {}).cache_read || 0;
    var localTodayInput = (loc.today || {}).input || 0;
    var allHit = hitPct(localTodayCache + remoteCache, localTodayInput + remoteInput);
    var todayHit = hitPct(today.cache_read, today.input);

    panel.appendChild(el("h4", null, "全部今日（本机 + SSH 远程）"));
    addRow(panel, "合计", fmtTok(localToday + remoteToday) + " tok" +
      (allHit == null ? "" : " · 命中 " + fmtPct(allHit)));
    addRow(panel, "构成", "本机 " + fmtTok(localToday) + " · 远程 " + fmtTok(remoteToday));
    addRow(panel, "缓存命中", fmtPct(allHit) + "（本机 " +
      fmtPct(hitPct(localTodayCache, localTodayInput)) + " · 远程 " +
      fmtPct(hitPct(remoteCache, remoteInput)) + "）");
    addRow(panel, "远程可达", remotes.length
      ? remoteOk + " / " + remotes.length + " 台" + (remoteErr ? "（失败未计入）" : "")
      : "未配置");

    var srcSec = el("div", "sub");
    panel.appendChild(srcSec);
    srcSec.appendChild(el("h4", null, isRemote
      ? "当前会话 · ☁ " + (view.src.label || view.src.host)
      : "当前会话 · 本机"));
    addRow(srcSec, "本数据源今日", fmtTok(today.total) + " tok（" + (today.requests || 0) +
      " 次请求）" + (todayHit == null ? "" : " · 命中 " + fmtPct(todayHit)));
    if (cur) {
      var r0 = cur.rate || {};
      addRow(srcSec, "会话", (cur.title || cur.sid.slice(0, 16)) + " · " + fmtTok(cur.totalTokens || cur.total) +
        " tok" + (cur.cacheHitRate == null ? "" : " · 命中 " + fmtPct(cur.cacheHitRate)));
      addRow(srcSec, "上下文长度", cur.context
        ? fmtTok(cur.context) + " tok" +
          (ctxWin ? " / " + fmtTok(ctxWin) + " · " + fmtPct(ctxRawR) : "") +
          (ctxWin ? " · " + ctxTone.label : "") +
          (selModel && baseModel && !selMatches ? " · 已切换模型" : "")
        : "暂无完成请求");
      addRow(srcSec, "缓存命中 / 模型", fmtPct(cur.cacheHitRate) + " · " + (ctxModel || cur.model || "—"));
      addRow(srcSec, "会话速率", "最近加权 " + fmtTps(r0.tps) + " · 最近 5 次峰值 " + fmtTps(r0.peak) +
        " tok/s · " + rateTone(r0.tps).label);
      if (cur.todayTps > 0) {
        addRow(srcSec, "今日均速", fmtTps(cur.todayTps) + " tok/s（当天加权平均）");
      }
      if (cur.todayPeak > 0) {
        addRow(srcSec, "今日峰值", fmtTps(cur.todayPeak) + " tok/s（当天最高单条）");
      }
      if (cur.turnRate) {
        addRow(srcSec, "上一轮", fmtTps(cur.turnRate.tps) + " tok/s（" + fmtTok(cur.turnRate.ratedOutput || 0) + " tok）");
      }
      addRow(srcSec, "请求 / 状态", (cur.requests || 0) + " 次 · " +
        (cur.running ? "⚡ 生成中 " + (cur.runningElapsedSec || 0) + "s" : "idle " + (cur.idleSec || 0) + "s"));
    }
    if (remotes.length) {
      var sec = el("div", "sub");
      sec.appendChild(el("h4", null, "SSH 远程主机"));
      remotes.forEach(function (r) {
        var head = el("div", "row");
        head.appendChild(el("span", "muted", r.label || r._key));
        var st = el("span", null, r.ok ? "✓ " + (r.latency_ms != null ? r.latency_ms + "ms" : "") : "✗ 失败");
        st.className = r.ok ? "" : "tokmon-err";
        head.appendChild(st);
        sec.appendChild(head);
        if (r.ok) {
          var d = r.data || {}, t = d.today || {}, dc = d.current || null;
          addRow(sec, "  今日", fmtTok(t.total) + " tok（" + (t.requests || 0) + " 次）" +
            (t.input ? " · 命中 " + fmtPct(hitPct(t.cache_read, t.input)) : ""));
          if (dc) addRow(sec, "  当前会话", fmtTok(dc.total) + " tok · " + fmtTps((dc.rate || {}).tps) + " tok/s");
          var dl = d.delta;
          if (dl && dl.requests) addRow(sec, "  窗口速率", fmtTps(dl.tps) + " tok/s（" + dl.requests + " 次完成）");
          var ss = d.sessions || [];
          for (var i = 0; i < Math.min(ss.length, 5); i++) {
            var s = ss[i];
            addRow(sec, "  " + (s.title || s.sid.slice(0, 14)),
              fmtTok(s.totalTokens) + " · " + fmtTps(s.lastTps) + " t/s" + (s.running ? " ⚡" : ""));
          }
        } else {
          var errRow = el("div", "row");
          errRow.appendChild(el("span", "muted", "  错误"));
          errRow.appendChild(el("span", "tokmon-err", String(r.error || "").slice(0, 90)));
          sec.appendChild(errRow);
        }
      });
      panel.appendChild(sec);
    }
    var foot = el("div", "sub");
    foot.appendChild(el("div", "muted",
      "速率 = output tokens ÷ 生成时长（思考段从 started_at 起算）· 远程 ~3s 轮询 · " + VERSION));
    panel.appendChild(foot);

    var r = anchor.getBoundingClientRect();
    var pw = panel.offsetWidth, ph = panel.offsetHeight;
    var cx = r.left + r.width / 2;
    var left = cx - pw / 2;                       // 默认居中于锚点
    if (cx > window.innerWidth / 2) left = r.right - pw;   // 靠右的锚点：面板右缘对齐
    left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
    panel.style.left = left + "px";
    var below = r.top - ph - 8;
    panel.style.top = (below > 8 ? below : Math.min(r.bottom + 8, window.innerHeight - ph - 8)) + "px";

    setTimeout(function () {
      document.addEventListener("mousedown", function onDoc(e) {
        if (panel && !panel.contains(e.target)) { closePanel(); }
        if (!panel) document.removeEventListener("mousedown", onDoc, true);
      }, true);
    }, 0);
  }
  function addRow(box, k, v) {
    var row = el("div", "row");
    row.appendChild(el("span", "muted", k));
    row.appendChild(el("span", null, v));
    box.appendChild(row);
  }

  /* ---------- 条内容渲染 ---------- */
  function renderBar(bar, sid) {
    var ui = bar.__ui;
    if (!ui) return;
    bar.__sid = sid;
    var loc = localPart();
    var view = sid ? sessionView(sid) : null;
    var mine = view && view.sess;
    var cur = mine || loc.current || null;
    var isRemote = !!(view && view.src);
    var srcName = isRemote ? (view.src.label || view.src.host || "") : "";
    var mineTag = view ? (isRemote ? "本会话（☁ " + srcName + "）" : "本会话") : "最近会话";
    var today = (isRemote && view.hostToday) ? view.hostToday : (loc.today || {});
    var delta = loc.delta || null;
    var live = null;
    if (mine && mine.running) live = mine;
    else if (!isRemote) live = runningAny(loc.sessions, sid || "");

    /* 速率：按当前会话 id 精确取（本地或远端），会话不存在时才退全局窗口速率 */
    var tps = 0;
    var rateTip = ["生成速率"];
    if (mine) {
      tps = mine.lastTps || ((mine.rate || {}).tps) || 0;
      rateTip.push("当前会话最近一次有效请求的输出速度：output tokens ÷ 生成时长。");
      rateTip.push("当前 " + fmtTps(tps) + " t/s · 最近 5 次峰值 " + fmtTps((mine.rate || {}).peak) +
        " t/s · " + (mine.requests || 0) + " 次请求");
      rateTip.push("当前速率取最近一次有效请求；最近 5 次峰值取最近 5 条完成请求中的最高单条。");
      if (mine.todayTps > 0 || mine.todayPeak > 0) {
        rateTip.push("今日均速 " + fmtTps(mine.todayTps) + " t/s · 今日峰值 " +
          fmtTps(mine.todayPeak) + " t/s（当天 " + (mine.todayRequests || 0) + " 次完成）");
        rateTip.push("今日均速 = 当天所有完成请求的加权平均（总输出 ÷ 总生成时长）；今日峰值取当天最高单条。");
      }
      rateTip.push("有思考段的请求从开始生成起算；显示 — 表示暂无有效样本。");
    } else if (delta && delta.requests) {
      tps = delta.tps;
      rateTip.push("未匹配到当前会话，显示全局窗口速率（上次推送后完成的请求）。");
      rateTip.push("当前 " + fmtTps(tps) + " t/s · 窗口峰值 " + fmtTps(delta.peak) + " t/s · " + delta.requests + " 次完成");
      rateTip.push("窗口峰值取上次推送后完成请求中的最高单条，不等同于当天峰值。");
    } else if (cur && cur.rate && cur.rate.tps) {
      tps = cur.rate.tps;
      rateTip.push("最近 " + cur.rate.requests + " 次请求的加权速率。");
      rateTip.push("当前 " + fmtTps(tps) + " t/s · 最近 5 次峰值 " + fmtTps(cur.rate.peak) + " t/s");
      rateTip.push("最近 5 次峰值取最近 5 条完成请求中的最高单条。");
      if (cur.todayTps > 0 || cur.todayPeak > 0) {
        rateTip.push("今日均速 " + fmtTps(cur.todayTps) + " t/s · 今日峰值 " +
          fmtTps(cur.todayPeak) + " t/s（当天 " + (cur.todayRequests || 0) + " 次完成）");
        rateTip.push("今日均速 = 当天所有完成请求的加权平均（总输出 ÷ 总生成时长）；今日峰值取当天最高单条。");
      }
    } else {
      rateTip.push("暂无有效样本：请求完成并落库后会显示。");
    }
    if (live) rateTip.push("⚡ 闪动表示当前正在生成。");
    var rt = rateTone(tps);
    if (tps > 0) rateTip.push("当前等级：" + rt.label);
    rateTip.push(tierLegend(RATE_TIERS, tps > 0 ? rt.label : "", "速率色阶"));
    setGroup(ui.rate, fmtTps(tps), "t/s", { live: !!live, pulse: !!live, tip: rateTip });
    if (rt.color) ui.rate.g.style.setProperty("--tokmon-rate-color", rt.color);
    else ui.rate.g.style.removeProperty("--tokmon-rate-color");

    /* 会话累计 + 上下文长度 */
    var hasSession = !!(mine || cur);
    if (mine) {
      setGroup(ui.session, fmtTok(mine.totalTokens), hitUnit(mine.cacheHitRate), {
        tip: ["本会话累计",
          "当前会话累计消耗的 token（输入 + 输出，含历史上下文；不含子代理，子代理为独立会话）。",
          "来源：" + mineTag + " · " + (mine.requests || 0) + " 次请求 · 模型 " + (mine.model || "—"),
          "缓存命中 " + fmtPct(mine.cacheHitRate) + "（cache read / input）",
          mine.running ? "状态：⚡ 生成中 " + (mine.runningElapsedSec || 0) + "s"
                       : "状态：idle " + (mine.idleSec || 0) + "s"]
      });
    } else if (cur) {
      setGroup(ui.session, fmtTok(cur.total), hitUnit(cur.cacheHitRate), {
        tip: ["最近活跃会话",
          "最近活跃会话「" + (cur.title || cur.sid.slice(0, 16)) + "」累计消耗的 token。",
          "上下文 " + fmtTok(cur.context) + " · " + cur.requests + " 次请求",
          "上轮速率 " + fmtTps((cur.turnRate || {}).tps) + " t/s"]
      });
    }
    ui.session.g.style.display = hasSession ? "" : "none";
    ui.sep1.style.display = hasSession ? "" : "none";
    ui.sep2.style.display = hasSession ? "" : "none";

    var ctxVal = mine ? (mine.context || 0) : ((cur && cur.context) || 0);
    var baseModel = mine ? mine.model : (cur && cur.model);
    var selModel = (selectedModel && selectedModel.modelId) || "";
    var selProvider = selectedModel ? (selectedModel.providerId || "") : "";
    var selMatches = !!(selModel && baseModel &&
      selModel.toLowerCase() === String(baseModel).toLowerCase());
    var useSessionWindow = !selModel || selMatches;
    var ctxModel = selModel || baseModel;
    var ctxHint = useSessionWindow ? (mine ? mine.contextWindow : (cur && cur.contextWindow)) : 0;
    var ctxWin = contextWindowFor(ctxModel, ctxHint, selModel ? selProvider : "",
      contextWindowsFor(view, isRemote));
    var ctxSrc = useSessionWindow
      ? (mine ? mine.contextWindowSource : (cur && cur.contextWindowSource))
      : (ctxWin ? "model-selection" : "");
    var ctxRawPct = (ctxWin && ctxVal) ? ctxVal * 100.0 / ctxWin : null;
    var ctxRawR = (ctxRawPct == null) ? null : Math.round(ctxRawPct * 10) / 10;
    var ct = contextTone(ctxRawR);   // 颜色按实际占用率分级，避免 45% 仍显示绿色
    if (hasSession) {
      var srcLabels = {
        "zcode-rules": "你的模型配置",
        "zcode-config": "ZCode 模型目录",
        "model-selection": "你的模型配置",
        "plugin-config": "插件 config 覆盖",
        "default": "插件默认值"
      };
      var ctxTip = ["上下文长度",
        "当前会话最近一次完成请求的输入 token 数（含缓存读），也是下一轮请求的基础上下文。",
        "当前 " + (ctxVal ? fmtTok(ctxVal) + " tok" : "暂无完成请求") +
          (ctxWin ? " / " + fmtTok(ctxWin) + "（" + fmtPct(ctxRawR) + "）" : "") +
          (ctxModel ? " · 模型 " + ctxModel : ""),
        ctxWin ? "占用等级（颜色分级）：" + ct.label : "未找到该模型的上下文窗口配置，仅显示绝对值。",
        ctxWin ? "窗口取自" + (srcLabels[ctxSrc] || "ZCode 模型目录") + "；数字与颜色都按实际占用率计算。"
               : "可在 ZCode 的模型配置里设置上下文长度，或在本插件 config 的 context_windows 覆盖。",
        "会话压缩后会自动回落；显示 — 表示还没有完成的请求。"].filter(function (x) { return x; });
      if (selModel && baseModel && !selMatches) {
        ctxTip.push("已切换到 " + ctxModel + "，窗口已按新模型更新；上下文用量仍来自最近一次完成请求，发送后会重新统计。");
      }
      if (ctxWin) {
        ctxTip.push(tierLegend(CONTEXT_TIERS, ct.label, "颜色分级（实际占用率）"));
      }
      if (mine && mine.running) ctxTip.push("⚡ 生成中，完成后更新。");
      setGroup(ui.ctx, ctxVal ? fmtTok(ctxVal) : "—",
        ctxWin && ctxVal ? "· " + fmtPct(ctxRawR) : "", { tip: ctxTip });
      if (ct.color) ui.ctx.g.style.setProperty("--tokmon-ctx-color", ct.color);
      else ui.ctx.g.style.removeProperty("--tokmon-ctx-color");
      ui.ctx.g.style.display = "";
      ui.sep3.style.display = "";
    } else {
      ui.ctx.g.style.display = "none";
      ui.sep3.style.display = "none";
    }

    /* 今日：跟随会话所在数据源 */
    var todayHit = hitPct(today.cache_read, today.input);
    setGroup(ui.today, fmtTok(today.total), hitUnit(todayHit), {
      tip: ["今日总量",
        (isRemote ? "远端主机 " + srcName + " 今日" : "本机今日") +
          "（本地零点起）所有会话合计（含子代理），含输入、输出与缓存读取。",
        "输入 " + fmtTok(today.input) + " · 输出 " + fmtTok(today.output) +
          " · 缓存读 " + fmtTok(today.cache_read),
        "缓存命中 " + fmtPct(todayHit) + " · " + (today.requests || 0) + " 次请求"]
    });

    /* 全部今日 = 本机今日 + 所有可达 SSH 远程今日 */
    var remotes = remoteList();
    var remoteToday = 0, remoteOk = 0, remoteErr = 0, bestTps = 0, errTxt = "";
    var remoteCache = 0, remoteInput = 0;
    remotes.forEach(function (r) {
      if (r.ok) {
        remoteOk++;
        var rd = r.data || {};
        var rt = rd.today || {};
        remoteToday += rt.total || 0;
        remoteCache += rt.cache_read || 0;
        remoteInput += rt.input || 0;
        var dl0 = rd.delta;
        var t0 = (dl0 && dl0.requests && dl0.tps) || ((rd.current || {}).rate || {}).tps || 0;
        if (t0 > bestTps) bestTps = t0;
      } else { remoteErr++; if (!errTxt) errTxt = String(r.error || ""); }
    });
    var localToday = (loc.today || {}).total || 0;
    var localTodayCache = (loc.today || {}).cache_read || 0;
    var localTodayInput = (loc.today || {}).input || 0;
    var allHit = hitPct(localTodayCache + remoteCache, localTodayInput + remoteInput);
    var allTip = ["今日总量（全部）",
      "本机今日 + 所有可达 SSH 远程主机今日的 token 合计（含子代理与缓存读取）。",
      "构成：本机 " + fmtTok(localToday) + " · 远程 " + fmtTok(remoteToday) +
        (remotes.length ? "（" + remoteOk + "/" + remotes.length + " 台可达）" : "（未配置远程）"),
      "缓存命中 " + fmtPct(allHit) + "（本机 " +
        fmtPct(hitPct(localTodayCache, localTodayInput)) + " · 远程 " +
        fmtPct(hitPct(remoteCache, remoteInput)) + "）"];
    if (remoteErr) allTip.push("有 " + remoteErr + " 台远程查询失败，未计入合计。");
    setGroup(ui.allToday, fmtTok(localToday + remoteToday), hitUnit(allHit), {
      warn: remoteErr > 0,
      tip: allTip
    });

    /* 远程健康 */
    if (remotes.length) {
      var okN = remoteOk, errN = remoteErr;
      var remoteTip = ["远程监控",
        "成功查询 " + okN + " / 共 " + remotes.length + " 台 SSH 主机。绿点=全部正常，红点=有失败。"];
      if (bestTps > 0) remoteTip.push("远端窗口速率 " + fmtTps(bestTps) + " t/s（该主机全部会话）。");
      if (errN) remoteTip.push("错误：" + errTxt.slice(0, 120));
      else remoteTip.push("点击右侧滑杆查看各主机明细。");
      setGroup(ui.remote, okN + "/" + remotes.length,
        bestTps > 0 ? "· " + fmtTps(bestTps) + " t/s" : "", {
          dot: errN ? "#f87171" : (okN ? "#4ade80" : "#8791a3"),
          err: errN > 0,
          tip: remoteTip
        });
      ui.sep5.style.display = "";
    } else {
      ui.remote.g.style.display = "none";
      ui.sep5.style.display = "none";
    }

    refreshOpenTip(bar);
  }

  /* ---------- 实例：一个 composer 一条（优先嵌入输入卡片内部，兜底 fixed 悬浮） ---------- */
  function findCard(composer) {
    /* 从输入框向上找「卡片容器」：有圆角的边框/背景盒，宽度包住 composer。
     * ZCode 界面改版频繁，不认类名，只认视觉特征。 */
    var cr = composer.getBoundingClientRect();
    var p = composer.parentElement;
    for (var i = 0; p && i < 8 && p !== document.body; i++, p = p.parentElement) {
      var cs = window.getComputedStyle(p);
      var radius = parseFloat(cs.borderRadius) || 0;
      var boxed = radius >= 8 && (cs.borderStyle !== "none" && cs.borderWidth !== "0px" ||
        cs.backgroundColor !== "rgba(0, 0, 0, 0)" || cs.backgroundImage !== "none");
      var r = p.getBoundingClientRect();
      var wraps = r.width >= cr.width * 0.9 && r.width <= cr.width * 1.6 &&
        r.bottom >= cr.bottom - 4 && r.top <= cr.top + 4;
      if (boxed && wraps && p.clientHeight > cr.height) return p;
    }
    return null;
  }

  function spawnInstance(composer) {
    var bar = el("div", "tokmon-root");
    var style = document.createElement("style");
    style.textContent = CSS;
    bar.appendChild(style);
    bar.__ui = createBarUI(bar);
    bindBarTips(bar);

    var disposed = false, mode = "fixed", host = null, mo = null, posTimer = 0;
    var mySid = mySessionSid(composer);   // 本 pane 会话（composer 祖先链 / 主进程 IPC 上报）
    renderBar(bar, mySid);
    MODES.count++;

    function attach() {
      var card = findCard(composer);
      if (card) {
        mode = "in";
        host = card;
        bar.classList.add("tokmon-in");
        card.appendChild(bar);
        /* React 重渲染可能移除外来子节点：卡片子树变化时若条被移除则重插 */
        if (mo) mo.disconnect();
        mo = new MutationObserver(function () {
          if (disposed) return;
          if (bar.parentElement !== card && card.isConnected) {
            card.appendChild(bar);
            setTimeout(function () { restoreTipAtPointer(bar); }, 0);
          }
        });
        mo.observe(card, { childList: true });
      } else {
        mode = "fixed";
        host = document.body;
        document.body.appendChild(bar);
      }
      MODES.last = mode;
    }

    function position() {   /* 仅 fixed 形态需要跟随；含屏幕外保护 */
      if (disposed || mode !== "fixed") return;
      var c = composer;
      if (!c.isConnected) return;
      var r = c.getBoundingClientRect();
      if (r.width <= 40 || r.bottom <= 0 || r.top > window.innerHeight) return;
      var top = r.bottom + 6;
      var bh = bar.offsetHeight || 24;
      if (top + bh > window.innerHeight - 4) top = Math.max(4, r.top - bh - 6);  /* 底部放不下 → 放输入框上方 */
      bar.style.top = top + "px";
      var left = r.left;
      var bw = bar.offsetWidth;
      if (left + bw > window.innerWidth - 8) left = Math.max(8, window.innerWidth - bw - 8);
      bar.style.left = left + "px";
      if (tipEl && tipEl.classList.contains("on") && bar.__tipTarget) repositionTip(bar.__tipTarget);
      else if (!bar.__tipSuppressed) setTimeout(function () { restoreTipAtPointer(bar); }, 0);
    }

    attach();
    if (mode === "fixed") {
      posTimer = setInterval(position, 500);
      window.addEventListener("resize", position);
    }
    position();
    setTimeout(function () { restoreTipAtPointer(bar); }, 0);

    return {
      _el: null,
      refreshSid: function () { mySid = sessIdOf(composer); },
      render: function () {
        if (disposed) return;
        var s = mySessionSid(composer);
        if (s !== mySid) mySid = s;   // pane 切了会话/主进程上报变了，跟着换
        renderBar(bar, mySid);
      },
      dispose: function () {
        disposed = true;
        if (bar.__tipTarget) { bar.__tipTarget = null; hideTip(); }
        if (mo) mo.disconnect();
        if (posTimer) clearInterval(posTimer);
        window.removeEventListener("resize", position);
        MODES.count--;
        bar.remove();
      }
    };
  }
  var MODES = window.__tokmonModes = { count: 0, last: "" };

  /* ---------- 协调器：600ms 扫描可见 composer ---------- */
  var SEL = 'textarea, [contenteditable="true"], [role="textbox"], .ProseMirror, .ql-editor';
  var STATS = window.__tokmonStats = { sel: 0, vis: 0, lower: 0, inst: 0, payload: 0, err: "" };

  function tick() {
    try {
      STATS.sel = STATS.vis = STATS.lower = 0;
      var best = null, bestTop = -1;
      var all = document.querySelectorAll(SEL);
      STATS.sel = all.length;
      for (var i = 0; i < all.length; i++) {
        var e = all[i];
        if (e.closest && e.closest('[role="dialog"]')) continue;    // 弹窗内不是聊天输入框
        if (e.closest && e.closest(".tokmon-root,.tokmon-panel")) continue;
        var r = e.getBoundingClientRect();
        if (!(r.width > 40 && r.height > 8 && r.bottom > 0 && r.top < window.innerHeight)) continue;
        STATS.vis++;
        if (r.top < window.innerHeight * 0.45) continue;            // 输入框在视口下半部
        STATS.lower++;
        if (r.top > bestTop) { best = e; bestTop = r.top; }
      }
      if (best) activeComposer = best;
      refreshSelectedModel();
      if (!best) return;
      /* 单窗口单条策略：只保留锚在当前 composer 上的实例，其余原地销毁。
       * 原地 splice 保证 window.__tokmonInstances 引用始终有效（热更新清理用）。 */
      for (var k = instances.length - 1; k >= 0; k--) {
        var inst = instances[k];
        if (inst._el !== best || !inst._el || !inst._el.isConnected) {
          try { inst.dispose(); } catch (e) { }
          instances.splice(k, 1);
        }
      }
      if (instances.length === 0) {
        var fresh = spawnInstance(best);
        fresh._el = best;
        instances.push(fresh);
      }
      STATS.inst = instances.length;
    } catch (e) { STATS.err = String(e && e.message || e); }
  }

  function onPointerMove(e) {
    lastPointer.x = e.clientX;
    lastPointer.y = e.clientY;
  }
  document.addEventListener("mousemove", onPointerMove, true);
  window.__tokmonPointerHandler = onPointerMove;

  refreshSelectedModel();
  tick();
  window.__tokmonCoordTimer = setInterval(tick, 600);
  /* 菜单选择/点击后立刻检查，不等 600ms 轮询 */
  document.addEventListener("click", function () { setTimeout(refreshSelectedModel, 60); }, true);
  document.addEventListener("keydown", function () { setTimeout(refreshSelectedModel, 60); }, true);
  window.__tokmonWinHide = onViewportChange;
  window.addEventListener("scroll", window.__tokmonWinHide, true);
  window.addEventListener("resize", window.__tokmonWinHide);

  /* ---------- 主进程数据入口 ---------- */
  window.__tokmonUpdate = function (p) {
    payload = p;
    STATS.payload = p ? Date.now() : 0;
    refreshSelectedModel();
    for (var i = 0; i < instances.length; i++) instances[i].render();
  };
  window.__tokmonSetSid = function (sid) {
    activeSid = sid || "";
    refreshSelectedModel();
    for (var i = 0; i < instances.length; i++) instances[i].render();
  };
  window.__tokmonInstances = instances;
})();
