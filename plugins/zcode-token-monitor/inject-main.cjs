/* tokmon inject-main —— ZCode 主进程 loader（被 app.asar 入口尾部 dynamic import 加载）。
 *
 * 职责：
 *   1) 常驻子进程 `python3 agent.py serve`（stdin/stdout 行协议），崩溃自动重启；
 *   2) fs.watch 监听用量库目录（去抖 300ms）+ 1.5s 限频 + 30s 心跳 → 向 serve 请求快照；
 *   3) 每个窗口首次加载时注入 overlay.js（mtime 热更新），数据到达时推给全部窗口。
 * 日志走 stderr（[tokmon] 前缀），运行日志文件 ~/.zcode/token-monitor/inject.log。
 */
(function () {
  "use strict";
  var path = require("path");
  var fs = require("fs");
  var os = require("os");
  var { spawn } = require("child_process");

  var electron;
  try { electron = require("electron"); } catch (e) { return; }   // 非 Electron 环境静默退出
  var app = electron.app, BrowserWindow = electron.BrowserWindow, ipcMain = electron.ipcMain;
  if (!app) return;

  /* 每窗口活跃会话：客户端渲染端经自带 IPC 通道上报（CUA PiP 同款通道）。
   * 追加监听器不影响原有处理；值为 "" = 已同步但当前无会话。 */
  var activeSid = new Map();    // webContents.id → 会话 id 或 ""
  try {
    ipcMain.on("zcode:sync-active-task-session", function (e, sid) {
      if (!e || !e.sender || e.sender.isDestroyed()) return;
      var s = typeof sid === "string" ? sid.trim() : "";
      var val = s && s.length <= 255 && /^[A-Za-z0-9_-]+$/.test(s) ? s : "";
      var prev = activeSid.get(e.sender.id);
      activeSid.set(e.sender.id, val);
      if (prev !== val) {
        LOG("sid update wc=" + e.sender.id + " → " + val);
        try {
          e.sender.executeJavaScript(
            "window.__tokmonSetSid && window.__tokmonSetSid(" + JSON.stringify(val) + ");", true
          ).catch(function () { });
        } catch (err) { }
        poke();   // 会话切换：尽快按新会话拉一次（限频器仍生效）
      }
    });
    LOG("ipc sid listener attached");
  } catch (e) { LOG("ipc hook failed: " + e.message); }

  var HERE = __dirname;
  var OVERLAY = path.join(HERE, "overlay.js");
  var AGENT = path.join(HERE, "agent.py");
  var DB_DIR = path.join(os.homedir(), ".zcode", "cli", "db");
  var LOGF = path.join(HERE, "inject.log");

  function LOG() {
    var args = Array.prototype.slice.call(arguments);
    try {
      console.error.apply(console, ["[tokmon]"].concat(args));
      var line = "[" + new Date().toISOString() + "] " + args.join(" ") + "\n";
      if (LOG_SIZE < 262144) { fs.appendFileSync(LOGF, line); LOG_SIZE += line.length; }
    } catch (e) { }
  }
  var LOG_SIZE = 0;
  try { LOG_SIZE = fs.statSync(LOGF).size; } catch (e) { }

  /* ---------- overlay 源码（mtime 热更新） ---------- */
  var overlaySrc = "", overlayMtime = 0;
  function loadOverlay(force) {
    try {
      var st = fs.statSync(OVERLAY);
      if (force || st.mtimeMs !== overlayMtime) {
        overlaySrc = fs.readFileSync(OVERLAY, "utf8");
        overlayMtime = st.mtimeMs;
        LOG("overlay loaded " + st.size + "B mtime=" + Math.round(st.mtimeMs));
      }
    } catch (e) { LOG("overlay read failed: " + e.message); }
  }

  /* ---------- 常驻 serve 子进程 ---------- */
  var proc = null, buf = "", busy = false, retimer = 0, restartTimer = 0;
  var lastAsk = 0, lastPush = null, restarts = 0, pushCount = 0;
  var quitting = false;

  function ask(sinceMs) {
    var now = Date.now();
    if (now - lastAsk < 1400) {
      /* 限频：被拦下的请求安排补发，绝不丢弃（否则 db 连续写入时永远刷不出新数据） */
      if (!retimer) {
        retimer = setTimeout(function () {
          retimer = 0;
          ask(lastSince);
        }, 1500 - (now - lastAsk) + 20);
      }
      return;
    }
    lastAsk = now;
    if (!proc || proc.killed || proc.exitCode !== null) { startServe(); }
    if (!proc) return;
    /* 把各窗口当前会话 id 一并请求：agent 按 id 精确查本地库，
     * 本地查不到的交给远端各主机（同一趟 SSH 内 --sid 命中）。 */
    var sids = [];
    activeSid.forEach(function (v) {
      if (v && sids.indexOf(v) < 0) sids.push(v);
    });
    try {
      proc.stdin.write(JSON.stringify({ since: sinceMs, sids: sids.slice(0, 8) }) + "\n");
    } catch (e) { LOG("stdin write failed: " + e.message); }
  }

  function startServe() {
    if (quitting) return;
    if (restartTimer) { clearTimeout(restartTimer); restartTimer = 0; }
    if (proc) { try { proc.kill(); } catch (e) { } proc = null; }
    var py = "python3";
    try {
      var cfg = JSON.parse(fs.readFileSync(path.join(HERE, "config.json"), "utf8"));
      if (cfg.python_path) py = cfg.python_path;
    } catch (e) { }
    var child = spawn(py, [AGENT, "serve"], { stdio: ["pipe", "pipe", "pipe"] });
    proc = child;
    restarts++;
    LOG("serve started pid=" + child.pid + " restarts=" + restarts);
    buf = "";
    child.on("error", function (err) {
      LOG("serve spawn error: " + err.message);
      if (proc === child) { proc = null; scheduleRestart(); }
    });
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", function (chunk) {
      buf += chunk;
      var idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        var line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        var payload;
        try { payload = JSON.parse(line); } catch (e) { continue; }
        try { pushAll(payload); } catch (e) { LOG("pushAll failed: " + e.message); }
      }
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", function (chunk) {
      var t = String(chunk).trim();
      if (t) LOG("serve stderr: " + t.slice(0, 300));
    });
    child.on("exit", function (code, sig) {
      LOG("serve exited code=" + code + " sig=" + sig);
      /* 只有当前进程退出才清引用/安排重启；旧进程的迟到 exit 不得清掉新进程，
       * 否则新进程变孤儿（stdin 被回收 → 立即退出）形成重启风暴。 */
      if (proc !== child) { LOG("stale serve exit ignored"); return; }
      proc = null;
      if (!quitting) scheduleRestart();
    });
    // 起手立即请求一次
    lastAsk = 0;
    ask(lastSince);
  }

  function scheduleRestart() {
    if (quitting || restartTimer) return;
    restartTimer = setTimeout(function () {
      restartTimer = 0;
      if (!proc && !quitting) startServe();
    }, 3000);
  }

  /* ---------- 窗口管理 ----------
   * 每个 webContents 只按 overlay 版本注入一次；数据推送绝不再执行 overlay 源码，
   * 否则 overlay 的 cleanup 会删除并重建输入卡片内的条，造成页面布局抖动。 */
  var injected = new Map();    // webContents.id → 已注入的 overlayMtime

  function injectOverlay(wc) {
    if (!wc || wc.isDestroyed()) return;
    loadOverlay(false);
    if (!overlaySrc) return;
    try {
      var stamp = overlayMtime;
      injected.set(wc.id, stamp);
      wc.executeJavaScript(
        "(function(){try{" + overlaySrc + "}catch(e){}})();" +
        "window.__tokmonSetSid && window.__tokmonSetSid(" + JSON.stringify(activeSid.get(wc.id) || "") + ");" +
        "window.__tokmonUpdate && window.__tokmonUpdate(" + safeJson(lastPush) + ");", true
      ).catch(function () { injected.delete(wc.id); });
    } catch (e) {
      injected.delete(wc.id);
      LOG("inject failed: " + e.message);
    }
  }

  function safeJson(obj) {
    if (obj == null) return "null";
    return JSON.stringify(obj).replace(/\u2028/g, "\\u2028").replace(/\u2029/g, "\\u2029");
  }

  function pushAll(payload) {
    lastPush = payload;
    pushCount++;
    loadOverlay(false);   // 仅检测文件版本；只有版本变化才触发真正的重新注入
    var js = "window.__tokmonUpdate && window.__tokmonUpdate(" + safeJson(payload) + ");";
    BrowserWindow.getAllWindows().forEach(function (win) {
      try {
        var wc = win.webContents;
        if (wc.isLoadingMainFrame()) {
          wc.once("did-finish-load", function () { injectOverlay(wc); });
          return;
        }
        if (injected.get(wc.id) !== overlayMtime) injectOverlay(wc);
        wc.executeJavaScript(
          "window.__tokmonSetSid && window.__tokmonSetSid(" + JSON.stringify(activeSid.get(wc.id) || "") + ");" + js, true
        ).catch(function () { });
      } catch (e) { }
    });
    if (pushCount % 20 === 1) LOG("push #" + pushCount + " windows=" + BrowserWindow.getAllWindows().length);
  }

  function watchWindows() {
    BrowserWindow.getAllWindows().forEach(bindWindow);
    app.on("browser-window-created", function (e, win) { bindWindow(win); });
  }
  function bindWindow(win) {
    try {
      var wc = win.webContents;
      wc.on("did-finish-load", function () {
        injected.delete(wc.id);   // 页面真的重载了，旧注入标记失效
        loadOverlay(false);
        injectOverlay(wc);
        if (lastPush) {
          wc.executeJavaScript("window.__tokmonUpdate && window.__tokmonUpdate(" + safeJson(lastPush) + ");", true).catch(function () { });
        }
      });
      win.on("closed", function () {
        try { activeSid.delete(wc.id); } catch (e) { }
        try { injected.delete(wc.id); } catch (e) { }
      });
    } catch (e) { }
  }

  /* ---------- 触发：db 文件变化（去抖+限频） + 心跳 ---------- */
  var lastSince = Date.now() - 60000;    // 首次拉 60s 窗口的增量
  var debounce = 0;

  function poke() {
    if (busy) return;
    busy = true;
    setTimeout(function () {
      busy = false;
      ask(lastSince);
      lastSince = Date.now();
    }, 300);
  }

  function watchDb() {
    try {
      fs.watch(DB_DIR, { persistent: false }, function (evt, file) {
        if (!file || file.indexOf("db.sqlite") !== 0) return;   // 只关心 db.sqlite/-wal/-shm
        if (debounce) return;
        debounce = setTimeout(function () { debounce = 0; poke(); }, 300);
      });
      LOG("watching " + DB_DIR);
    } catch (e) { LOG("fs.watch failed: " + e.message + "（仅心跳轮询）"); }
  }

  app.whenReady().then(function () {
    LOG("inject-main ready dir=" + HERE);
    loadOverlay(true);
    watchWindows();
    watchDb();
    startServe();
    setInterval(function () { if (!quitting) poke(); }, 5000);   // 心跳兜底：fs.watch 事件丢失也能 ~5s 刷新
  }).catch(function (e) { LOG("whenReady failed: " + e.message); });

  app.on("before-quit", function () {
    quitting = true;
    if (proc) {
      try { proc.stdin.end(); } catch (e) { }
      try { proc.kill(); } catch (e) { }
      proc = null;
    }
  });
})();
