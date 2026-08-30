#!/usr/bin/env node
// QLab 轻量 WebUI —— 人类只读看板，单文件零依赖（计划见 knowledge/notes/webui_plan.md）。
// 硬约束：不写任何容器文件 / 容器内零常驻进程 / 只复用只读查询 / 无操作入口。
// 用法: node scripts/webui.js [--port 8099] [--interval 10] [--slow-interval 30]
// 环境: QLAB_CONTAINER（默认 hermes-1679f5b2，仅容器模式）、QLAB_NOTIFY_DIR（默认 <repo>/notify）
// 默认【本机直跑】（不加 docker）：直接在本机跑 python -m pipeline.*（cwd=QLAB_ROOT），
// 通知状态读 <repo>/notify/bridge_state.json。容器模式仅在显式 QLAB_WEBUI_CONTAINER=1 时启用。
const http = require('node:http');
const { execFile, execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const argN = (name, dflt) => {
  const i = process.argv.indexOf(name);
  return (i >= 0 && process.argv[i + 1] && !isNaN(Number(process.argv[i + 1])))
    ? Number(process.argv[i + 1]) : dflt;
};
const PORT = argN('--port', Number(process.env.QLAB_WEBUI_PORT) || 8099);
const FAST_MS = argN('--interval', 10) * 1000;
const SLOW_MS = argN('--slow-interval', 30) * 1000;
const CONTAINER = process.env.QLAB_CONTAINER || 'hermes-1679f5b2';
// 仓库根（scripts/webui.js 的上一级）与 venv python（没有则退 python3）
const QLAB_ROOT = process.env.QLAB_ROOT || path.resolve(__dirname, '..');
const NOTIFY_DIR = process.env.QLAB_NOTIFY_DIR || path.join(QLAB_ROOT, 'notify');
const PY_LOCAL = fs.existsSync(path.join(QLAB_ROOT, '.venv', 'bin', 'python'))
  ? path.join(QLAB_ROOT, '.venv', 'bin', 'python') : 'python3';
// 本机直跑为默认；容器模式仅 QLAB_WEBUI_CONTAINER=1 显式开启（且 docker 可达）。
// QLAB_WEBUI_LOCAL=1 仍可强制本机（兼容旧用法）。
let useLocal = process.env.QLAB_WEBUI_LOCAL === '1' || process.env.QLAB_WEBUI_CONTAINER !== '1';
if (!useLocal) {
  try { execFileSync('docker', ['exec', '-i', CONTAINER, 'true'], { stdio: 'ignore', timeout: 10000 }); }
  catch (e) { useLocal = true; }
}

const state = {
  ts: null, queue: [], events: [], board: [], boardAll: [], claims: [],
  heartbeat: '', bridge: null, errors: {}, lastSlow: 0,
};
const detailCache = new Map();  // key -> {ts, data} 按需查询 60s 缓存

function dockerExec(args, opts) {
  return execFileSync('docker', ['exec', '-i', CONTAINER].concat(args),
    Object.assign({ encoding: 'utf8', timeout: 120000 }, opts || {})).trim();
}
// 只读 CLI 全部压线程：容器 pids.max=256，多个 python 并发时 OpenBLAS 默认
// 16 线程会 pthread_create 失败（Resource temporarily unavailable）。这些命令
// 只是 sqlite 读，单线程足够。本机模式同样压线程。
const THREAD_ENV = 'env OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 ';
function localRunSync(args, opts) {
  return execFileSync(PY_LOCAL, args,
    Object.assign({ encoding: 'utf8', timeout: 120000, cwd: QLAB_ROOT }, opts || {})).trim();
}
function localRunAsync(args, opts, cb) {
  execFile(PY_LOCAL, args,
    Object.assign({ encoding: 'utf8', timeout: 120000, cwd: QLAB_ROOT }, opts || {}), cb);
}
// cmd 形如 'python -m pipeline.queue status --json'：本机去掉前导 python，
// 用 venv 解释器在 QLAB_ROOT 下执行。
function cmdArgs(cmd) {
  return cmd.trim().split(/\s+/).slice(1);
}
function containerCmd(cmd) {
  if (useLocal) return localRunSync(cmdArgs(cmd));
  return dockerExec(['sh', '-c', 'cd /root/quant && ' + THREAD_ENV + cmd]);
}
function safe(key, fn) {
  try {
    const v = fn();
    delete state.errors[key];
    return v;
  } catch (e) {
    state.errors[key] = String(e.message || e).slice(0, 200);
    return null;
  }
}

// 后台刷新全部走异步 execFile：慢查询（board/claims，秒级到十几秒）不再阻塞
// 事件循环——队列/心跳的 10s 快刷新永远准点，HTTP 响应也永远即时。
function runCmdAsync(key, cmd, onOut) {
  if (useLocal) {
    localRunAsync(cmdArgs(cmd), {}, (err, stdout) => {
      if (err) { handleExecErr(key, err); return; }
      handleExecOk(key);
      try { onOut(stdout.trim()); } catch (e) { /* 解析失败留旧值 */ }
    });
    return;
  }
  execFile('docker', ['exec', '-i', CONTAINER, 'sh', '-c', 'cd /root/quant && ' + THREAD_ENV + cmd],
    { encoding: 'utf8', timeout: 120000 }, (err, stdout) => {
      if (err) {
        handleExecErr(key, err);
        return;
      }
      handleExecOk(key);
      try { onOut(stdout.trim()); } catch (e) { /* 解析失败留旧值 */ }
    });
}

// 串成两条命令链（每条链 = 一个 sh、多个顺序执行的 python）：并发 python 从
// 6 个降到 2 个，容器 pids.max=256 压力最小化；异步执行仍不阻塞事件循环。
const SPLIT = '===QLAB_SPLIT===';

// 容器忙（pids 满 / fork 失败）时自动退避：不打扰研究、不刷错误横幅。
// 退避 1/2/4/8/10 分钟封顶，任一命令成功即复位。
let backoffUntil = 0;
let backoffStep = 0;
function inBackoff() { return Date.now() < backoffUntil; }
function noteBackoff() {
  backoffStep = Math.min(backoffStep + 1, 5);
  backoffUntil = Date.now() + Math.min(60000 * Math.pow(2, backoffStep), 600000);
  state.errors.backoff = '容器 pids 已满/繁忙（fork 失败），自动退避至 '
    + new Date(backoffUntil).toISOString().slice(11, 19) + '，不打扰正在运行的研究';
}
function resetBackoff() {
  if (!backoffStep) return;
  backoffStep = 0;
  backoffUntil = 0;
  delete state.errors.backoff;
}
function handleExecErr(key, err) {
  const msg = String(err.message || err);
  state.errors[key] = msg.slice(0, 200);
  if (/Cannot fork|temporarily unavailable|pthread/i.test(msg)) noteBackoff();
}
function handleExecOk(key) {
  delete state.errors[key];
  resetBackoff();
}

function runChainAsync(key, cmds, onParts) {
  if (useLocal) {
    const parts = [];
    const next = (i) => {
      if (i >= cmds.length) { onParts(parts); return; }
      localRunAsync(cmdArgs(cmds[i]), {}, (err, stdout) => {
        if (err) { handleExecErr(key, err); next(i + 1); return; }
        handleExecOk(key);
        try { parts.push(String(stdout || '').trim()); } catch (e) { /* 忽略 */ }
        next(i + 1);
      });
    };
    next(0);
    return;
  }
  execFile('docker', ['exec', '-i', CONTAINER, 'sh', '-c',
    'cd /root/quant && ' + THREAD_ENV + cmds.map((c) => c + ' || true ; echo ' + SPLIT + ' ; ').join(' ')],
    { encoding: 'utf8', timeout: 360000 }, (err, stdout) => {
      if (err) {
        handleExecErr(key, err);
        return;
      }
      handleExecOk(key);
      try {
        onParts(stdout.split(SPLIT).map((p) => p.trim()).filter((p) => p));
      } catch (e) { /* 解析失败留旧值 */ }
    });
}

function refreshFast() {
  if (inBackoff()) return;
  // 全部按时间倒序（最新在前）：队列 created_at、事件 id、台账 start_time、claims 更新时间
  runChainAsync('queue',
    ['python -m pipeline.queue status --json',
     'python -m pipeline.queue events --limit 60'],
    (parts) => {
      if (parts[0]) {
        state.queue = JSON.parse(parts[0]).slice().sort((a, b) =>
          String(b.created_at || '').localeCompare(String(a.created_at || '')));
      }
      if (parts[1]) state.events = JSON.parse(parts[1]).slice().reverse();
    });
  if (useLocal) {
    try {
      state.heartbeat = fs.readFileSync(path.join(QLAB_ROOT, 'results/queue/heartbeat'), 'utf8').trim() || '';
    } catch (e) { state.heartbeat = ''; }
  } else {
    runCmdAsync('heartbeat', 'cat results/queue/heartbeat 2>/dev/null || true',
      (out) => { state.heartbeat = out || ''; });
  }
  const bs = safe('bridge', () => JSON.parse(fs.readFileSync(path.join(NOTIFY_DIR, 'bridge_state.json'), 'utf8')));
  if (bs) state.bridge = bs;
  state.ts = new Date().toISOString();
}

function refreshSlow() {
  if (inBackoff()) return;
  const byStart = (rows) => rows.slice().sort((a, b) =>
    (b.start_time || 0) - (a.start_time || 0));
  runChainAsync('board',
    ['python -m pipeline.board --json --formal',
     'python -m pipeline.board --json',
     'python -m pipeline.kb claims'],
    (parts) => {
      if (parts[0]) state.board = byStart(JSON.parse(parts[0]));
      if (parts[1]) state.boardAll = byStart(JSON.parse(parts[1]));
      if (parts[2]) {
        state.claims = JSON.parse(parts[2]).slice().sort((a, b) =>
          String(b.updated_at || b.created_at || '').localeCompare(
            String(a.updated_at || a.created_at || '')));
      }
    });
  state.lastSlow = Date.now();
}

const RUN_SCRIPT = [
'import sys, json',
'sys.path.insert(0, "/root/quant")',
'from pipeline import registry',
'rid = sys.argv[1]',
'c = registry.client()',
'try:',
'    r = c.get_run(rid)',
'except Exception as e:',
'    print(json.dumps({"run_id": rid, "found": False, "error": str(e)[:200]}, ensure_ascii=False))',
'    raise SystemExit(0)',
'exp_name = r.info.experiment_id',
'try:',
'    exp_name = c.get_experiment(r.info.experiment_id).name',
'except Exception:',
'    pass',
'print(json.dumps({"run_id": rid, "found": True, "experiment": exp_name,',
'                   "status": r.info.status, "start_time": r.info.start_time,',
'                   "end_time": r.info.end_time, "params": dict(r.data.params),',
'                   "metrics": dict(r.data.metrics), "tags": dict(r.data.tags)},',
'      ensure_ascii=False, default=str))',
].join('\n');

function runDetail(rid) {
  // 只读 on-demand：stdin 喂脚本，不落任何容器文件；无 shell 引号层。
  // 本机模式：cwd=QLAB_ROOT，`python -` 时 cwd 在 sys.path，pipeline 可导入。
  if (useLocal) return localRunSync(['-', rid], { input: RUN_SCRIPT });
  return dockerExec(['sh', '-c', THREAD_ENV + 'python - ' + JSON.stringify(rid)], { input: RUN_SCRIPT });
}

function cached(key, ttl, fn) {
  const hit = detailCache.get(key);
  if (hit && Date.now() - hit.ts < ttl) return hit.data;
  const data = fn();
  detailCache.set(key, { ts: Date.now(), data });
  return data;
}

refreshFast();
refreshSlow();
setInterval(refreshFast, FAST_MS);
setInterval(refreshSlow, SLOW_MS);

const HTML = "<!doctype html>\n<html lang=\"zh\">\n<head>\n<meta charset=\"utf-8\">\n<title>QLab 看板</title>\n<style>\n  body { font-family: system-ui, 'Segoe UI', sans-serif; margin: 0; background:#f5f6f8; color:#222; }\n  header { position:sticky; top:0; z-index:5; background:#1c2836; color:#fff; padding:10px 20px; display:flex; gap:16px; align-items:baseline; }\n  header h1 { font-size:18px; margin:0; }\n  header .meta { font-size:12px; color:#9fb0c0; }\n  main { padding: 16px 20px; max-width: 1400px; margin: 0 auto; }\n  section { background:#fff; border:1px solid #e3e6ea; border-radius:6px; padding:12px 16px; margin-bottom:16px; }\n  h2 { font-size:14px; margin:0 0 8px; color:#1c2836; }\n  .cards { display:flex; flex-wrap:wrap; gap:8px; }\n  .card { border:1px solid #e3e6ea; border-radius:6px; padding:8px 14px; min-width:110px; }\n  .card .n { font-size:18px; font-weight:600; }\n  .card .l { font-size:11px; color:#667; }\n  .st-queued { color:#6a737d; } .st-running { color:#1565c0; } .st-blocked { color:#e65100; }\n  .st-done { color:#2e7d32; } .st-failed { color:#c62828; } .st-cancelled { color:#8e24aa; }\n  table { border-collapse:collapse; width:100%; font-size:12px; }\n  th, td { border-bottom:1px solid #eef0f2; padding:4px 8px; text-align:left; vertical-align:top; }\n  th { color:#667; font-weight:600; white-space:nowrap; }\n  tbody tr.clickable { cursor:pointer; }\n  tbody tr.clickable:hover td { background:#f0f4fa; }\n  input, select { font-size:12px; padding:3px 6px; margin-right:8px; border:1px solid #ccd2d9; border-radius:4px; }\n  .err { background:#fdecea; color:#c62828; padding:6px 10px; border-radius:4px; font-size:12px; margin-bottom:10px; }\n  .toolbar { margin-bottom:8px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }\n  .pager { margin-top:6px; font-size:12px; color:#556; display:flex; align-items:center; gap:8px; }\n  .pager button { font-size:12px; padding:2px 10px; border:1px solid #ccd2d9; background:#fff; border-radius:4px; cursor:pointer; }\n  .pager button:disabled { color:#bbb; cursor:default; }\n  footer { font-size:11px; color:#889; padding: 0 20px 20px; }\n  #modal-backdrop { position:fixed; inset:0; background:rgba(20,30,40,.45); z-index:10; display:none; }\n  #modal { position:fixed; left:50%; top:5%; transform:translateX(-50%); width:min(900px, 92vw); max-height:88vh; overflow:auto;\n           background:#fff; border-radius:8px; z-index:11; display:none; box-shadow:0 8px 40px rgba(0,0,0,.35); }\n  #modal .mhead { position:sticky; top:0; background:#1c2836; color:#fff; padding:10px 16px; display:flex; justify-content:space-between; align-items:center; }\n  #modal .mhead button { background:none; border:none; color:#fff; font-size:20px; cursor:pointer; }\n  #modal .mbody { padding:12px 16px; }\n  .kv { width:100%; font-size:12px; }\n  .kv td:first-child { color:#667; white-space:nowrap; width:160px; }\n  pre.mono { background:#f4f5f7; padding:8px; border-radius:4px; font-size:11px; overflow:auto; max-height:300px; }\n  .mh2 { font-size:13px; font-weight:600; margin:10px 0 4px; color:#1c2836; }\n</style>\n</head>\n<body>\n<header>\n  <h1>QLab 看板</h1>\n  <span class=\"meta\" id=\"ts\">--</span>\n  <span class=\"meta\">只读旁观 · 操作仍走 agent/命令行</span>\n</header>\n<main>\n  <div id=\"errs\"></div>\n  <section>\n    <h2>总览</h2>\n    <div class=\"cards\" id=\"counts\"></div>\n    <div class=\"cards\" id=\"sys\" style=\"margin-top:8px\"></div>\n  </section>\n  <section>\n    <h2>队列</h2>\n    <div class=\"toolbar\">\n      <input id=\"qbatch\" placeholder=\"batch 过滤\">\n      <select id=\"qstatus\">\n        <option value=\"\">全部状态</option>\n        <option>queued</option><option>running</option><option>blocked</option>\n        <option>done</option><option>failed</option><option>cancelled</option>\n      </select>\n      <label>每页 <select id=\"qsize\"><option>20</option><option>50</option><option>100</option></select></label>\n      <span class=\"pager\" id=\"qpager\"></span>\n    </div>\n    <table id=\"qtable\"><thead><tr>\n      <th>JOB</th><th>批次</th><th>EXP</th><th>RUNNER</th><th>状态</th><th>ATT</th><th>错误/备注</th>\n    </tr></thead><tbody></tbody></table>\n  </section>\n  <section>\n    <h2>台账（board）</h2>\n    <div class=\"toolbar\">\n      <select id=\"bview\"><option value=\"board\">正式行（FINISHED 非 smoke）</option><option value=\"boardAll\">全部行</option></select>\n      <label>每页 <select id=\"bsize\"><option>20</option><option>50</option><option>100</option></select></label>\n      <span class=\"pager\" id=\"bpager\"></span>\n    </div>\n    <table id=\"btable\"><thead><tr>\n      <th>EXP</th><th>状态</th><th>POOL</th><th>rankic</th><th>p</th><th>base_ref</th>\n      <th>n_variants</th><th>p_bonf</th><th>sharpe</th><th>start</th>\n    </tr></thead><tbody></tbody></table>\n  </section>\n  <section>\n    <h2>知识（claims）</h2>\n    <div class=\"toolbar\">\n      <input id=\"csearch\" placeholder=\"搜索\">\n      <select id=\"cstatus\">\n        <option value=\"\">全部状态</option>\n        <option>untested</option><option>confirmed</option><option>falsified</option><option>partial</option>\n      </select>\n      <label>每页 <select id=\"csize\"><option>20</option><option>50</option><option>100</option></select></label>\n      <span class=\"pager\" id=\"cpager\"></span>\n    </div>\n    <table id=\"ctable\"><thead><tr><th>ID</th><th>状态</th><th>内容</th><th>来源</th><th>关联实验</th></tr></thead><tbody></tbody></table>\n  </section>\n  <section>\n    <h2>最近事件（近 60 条）</h2>\n    <table id=\"etable\"><thead><tr><th>TS</th><th>JOB</th><th>EXP</th><th>状态</th><th>信息</th></tr></thead><tbody></tbody></table>\n  </section>\n</main>\n<footer>数据来源（只读）：queue status/events --json · board --json[--formal] · kb claims · heartbeat · bridge_state.json；点击队列行/台账行/claim 行查看详情（按需只读查询，无自动放大）</footer>\n<div id=\"modal-backdrop\"></div>\n<div id=\"modal\"><div class=\"mhead\"><span id=\"m-title\">详情</span><button id=\"m-close\">&times;</button></div><div class=\"mbody\" id=\"m-body\"></div></div>\n<script>\nconst esc = (s) => String(s == null ? '' : s).replace(/[&<>\"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));\nconst stCls = (s) => 'st-' + s;\nconst fmtP = (v) => (typeof v === 'number') ? (v < 0.001 && v !== 0 ? v.toExponential(2) : v.toFixed(4)) : (v == null ? '' : String(v));\nconst fmtTS = (v) => String(v == null ? '' : v).slice(0, 19).replace('T', ' ');\nlet __s = null;\nconst pages = { q: {page: 1}, b: {page: 1}, c: {page: 1} };\n\nasync function load() {\n  try {\n    const r = await fetch('/api/state');\n    render(await r.json());\n  } catch (e) {\n    document.getElementById('errs').innerHTML = '<div class=\"err\">连接看板失败：' + esc(e) + '（服务是否在跑？node scripts/webui.js）</div>';\n  }\n}\n\nfunction pageSize(id) { return Number(document.getElementById(id).value || 20); }\nfunction pagerHtml(pid, page, total, size) {\n  const last = Math.max(1, Math.ceil(total / size));\n  const p = Math.min(Math.max(1, page), last);\n  return '<button onclick=\"goPage(\\'' + pid + '\\',' + (p - 1) + ')\"' + (p <= 1 ? ' disabled' : '') + '>上一页</button>' +\n    '<span>第 ' + p + '/' + last + ' 页 · 共 ' + total + ' 条</span>' +\n    '<button onclick=\"goPage(\\'' + pid + '\\',' + (p + 1) + ')\"' + (p >= last ? ' disabled' : '') + '>下一页</button>';\n}\nwindow.goPage = (pid, n) => { pages[pid].page = n; if (__s) render(__s); };\nwindow.sizeChange = (pid) => { pages[pid].page = 1; if (__s) render(__s); };\n\nfunction slicePage(rows, pid, size) {\n  const p = Math.max(1, pages[pid].page);\n  const last = Math.max(1, Math.ceil(rows.length / size));\n  pages[pid].page = Math.min(p, last);\n  return rows.slice((pages[pid].page - 1) * size, pages[pid].page * size);\n}\n\nfunction openModal(title, html) {\n  document.getElementById('m-title').textContent = title;\n  document.getElementById('m-body').innerHTML = html;\n  document.getElementById('modal').style.display = 'block';\n  document.getElementById('modal-backdrop').style.display = 'block';\n}\nfunction closeModal() {\n  document.getElementById('modal').style.display = 'none';\n  document.getElementById('modal-backdrop').style.display = 'none';\n}\ndocument.getElementById('m-close').onclick = closeModal;\ndocument.getElementById('modal-backdrop').onclick = closeModal;\ndocument.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });\n\nasync function jobDetail(id) {\n  openModal('任务 #' + id, '<div class=\"mbody\">加载中…</div>');\n  try {\n    const r = await fetch('/api/job/' + Number(id));\n    const d = await r.json();\n    const j = d.job || {};\n    const rows = [['状态', j.status], ['批次', j.batch_id], ['EXP', j.exp_id], ['runner', j.runner],\n      ['attempts', j.attempts], ['spec_hash', j.spec_hash], ['spec_path', j.spec_path],\n      ['mlflow_run_id', j.mlflow_run_id], ['data_rev', j.data_rev], ['pgid', j.pgid],\n      ['created', j.created_at], ['started', j.started_at], ['finished', j.finished_at]];\n    let html = '<table class=\"kv\">' + rows.map(([k, v]) =>\n      '<tr><td>' + esc(k) + '</td><td>' + esc(v) + '</td></tr>').join('') + '</table>';\n    if (j.error) html += '<div class=\"mh2\">错误（全文）</div><pre class=\"mono\">' + esc(j.error) + '</pre>';\n    if (j.note) html += '<div class=\"mh2\">备注</div><pre class=\"mono\">' + esc(j.note) + '</pre>';\n    if (d.events && d.events.length) {\n      html += '<div class=\"mh2\">事件链（' + d.events.length + ' 条）</div><table class=\"kv\">' +\n        d.events.map(e => '<tr><td>' + esc(fmtTS(e.ts)) + '</td><td class=\"' + stCls(e.status) + '\">' + esc(e.status) + '</td><td>' + esc(e.error || '') + '</td></tr>').join('') + '</table>';\n    }\n    if (d.log_tail) html += '<div class=\"mh2\">日志尾部（job 日志，近 30 行）</div><pre class=\"mono\">' + esc(d.log_tail) + '</pre>';\n    document.getElementById('m-body').innerHTML = html;\n  } catch (e) {\n    document.getElementById('m-body').innerHTML = '<div class=\"err\">详情加载失败：' + esc(e) + '</div>';\n  }\n}\n\nasync function runDetail(rid) {\n  openModal('台账 run ' + esc(rid), '加载中…');\n  try {\n    const r = await fetch('/api/run/' + encodeURIComponent(rid));\n    const d = await r.json();\n    if (!d.found) { document.getElementById('m-body').innerHTML = '<div class=\"err\">未找到 run</div>'; return; }\n    const kv = (obj) => Object.entries(obj || {}).map(([k, v]) =>\n      '<tr><td>' + esc(k) + '</td><td>' + esc(typeof v === 'object' ? JSON.stringify(v) : v) + '</td></tr>').join('');\n    document.getElementById('m-body').innerHTML =\n      '<div class=\"mh2\">基本信息</div><table class=\"kv\"><tr><td>experiment</td><td>' + esc(d.experiment) + '</td></tr>' +\n      '<tr><td>status</td><td>' + esc(d.status) + '</td></tr><tr><td>start</td><td>' + esc(fmtTS(d.start_time)) + '</td></tr>' +\n      '<tr><td>end</td><td>' + esc(fmtTS(d.end_time)) + '</td></tr></table>' +\n      '<div class=\"mh2\">params</div><table class=\"kv\">' + kv(d.params) + '</table>' +\n      '<div class=\"mh2\">metrics</div><table class=\"kv\">' + kv(d.metrics) + '</table>' +\n      '<div class=\"mh2\">tags</div><table class=\"kv\">' + kv(d.tags) + '</table>';\n  } catch (e) {\n    document.getElementById('m-body').innerHTML = '<div class=\"err\">详情加载失败：' + esc(e) + '</div>';\n  }\n}\n\nfunction claimDetail(c) {\n  openModal('claim ' + esc(c.claim_id), '<table class=\"kv\">' +\n    [['状态', c.status], ['类型', c.ctype], ['来源', c.source], ['创建', c.created_at], ['更新', c.updated_at],\n     ['tags', (c.tags || []).join(', ')], ['关联实验', (c.linked_exp_ids || []).join(', ')]].map(([k, v]) =>\n    '<tr><td>' + esc(k) + '</td><td>' + esc(v) + '</td></tr>').join('') + '</table>' +\n    '<div class=\"mh2\">全文</div><pre class=\"mono\" style=\"white-space:pre-wrap\">' + esc(c.text) + '</pre>');\n}\n\nfunction render(s) {\n  __s = s;\n  document.getElementById('ts').textContent = '更新 ' + (s.ts || '').slice(0, 19).replace('T', ' ') + '（每 10s 队列 / 30s 台账）';\n  const errs = Object.entries(s.errors || {});\n  document.getElementById('errs').innerHTML = errs.length\n    ? '<div class=\"err\">采集失败：' + errs.map(([k, v]) => esc(k) + ': ' + esc(v)).join(' · ') + '</div>' : '';\n  const counts = {};\n  (s.queue || []).forEach(j => { counts[j.status] = (counts[j.status] || 0) + 1; });\n  const order = ['queued', 'running', 'blocked', 'done', 'failed', 'cancelled'];\n  document.getElementById('counts').innerHTML = order.map(st => {\n    const n = counts[st] || 0;\n    return '<div class=\"card\"><div class=\"n ' + stCls(st) + '\">' + n + '</div><div class=\"l\">' + st + '</div></div>';\n  }).join('');\n  const pend = (s.events || []).filter(e => e.status === 'backup_pending');\n  const hbToks = String(s.heartbeat || '').trim().split(/\\s+/);\n  const hbEpoch = hbToks.length ? Number(hbToks[0]) : NaN;\n  const hbAge = isNaN(hbEpoch) ? null : Math.round(Date.now() / 1000 - hbEpoch);\n  const hbTxt = isNaN(hbEpoch) ? '无心跳'\n    : ('心跳 ' + hbToks.join(' ') + ' · ' + hbAge + 's 前' + (hbAge > 300 ? '（stale）' : ''));\n  const br = s.bridge || {};\n  document.getElementById('sys').innerHTML = [\n    '<div class=\"card\"><div class=\"n\">' + esc(hbTxt) + '</div><div class=\"l\">dispatcher 心跳（按主机时钟估算）</div></div>',\n    '<div class=\"card\"><div class=\"n\">' + (pend.length ? '未推送 ' + pend.length + ' 条' : '正常') + '</div><div class=\"l\">备份 push（近 60 事件内 backup_pending）</div></div>',\n    '<div class=\"card\"><div class=\"n\">ack ' + (br.lastAckedId != null ? br.lastAckedId : '-') + ' / done ' + (br.doneAckId != null ? br.doneAckId : '-') + '</div><div class=\"l\">通知桥 marker</div></div>',\n  ].join('');\n  // 队列（分页）\n  const qbatch = document.getElementById('qbatch').value.trim();\n  const qstatus = document.getElementById('qstatus').value;\n  const qall = (s.queue || []).filter(j =>\n    (!qbatch || String(j.batch_id || '').includes(qbatch)) && (!qstatus || j.status === qstatus));\n  const qsize = pageSize('qsize');\n  const qrows = slicePage(qall, 'q', qsize);\n  document.getElementById('qpager').innerHTML = pagerHtml('q', pages.q.page, qall.length, qsize);\n  document.querySelector('#qtable tbody').innerHTML = qrows.map(j => {\n    return '<tr class=\"clickable\" onclick=\"jobDetail(' + j.job_id + ')\"><td>' + j.job_id + '</td><td>' + esc(j.batch_id) + '</td><td>' + esc(j.exp_id) +\n      '</td><td>' + esc(j.runner) + '</td><td class=\"' + stCls(j.status) + '\">' + j.status +\n      '</td><td>' + j.attempts + '</td><td>' + esc((j.error || j.note || '').slice(0, 160)) + '</td></tr>';\n  }).join('') || '<tr><td colspan=7>无匹配任务</td></tr>';\n  // 台账（分页）\n  const bview = document.getElementById('bview').value;\n  const ball = (s[bview] || []);  // 服务端已按 start_time 倒序（最新在前）\n  const bsize = pageSize('bsize');\n  const brows = slicePage(ball, 'b', bsize);\n  document.getElementById('bpager').innerHTML = pagerHtml('b', pages.b.page, ball.length, bsize);\n  document.querySelector('#btable tbody').innerHTML = brows.map(r => {\n    return '<tr class=\"clickable\" onclick=\"runDetail(\\'' + esc(r.run_id) + '\\')\"><td>' + esc(r.exp) + '</td><td class=\"' + stCls(String(r.status || '').toLowerCase()) + '\">' + esc(r.status) +\n      '</td><td>' + esc(r.pool) + '</td><td>' + fmtP(r.rankic_mean) + '</td><td>' + fmtP(r.p_le0) +\n      '</td><td>' + esc(r.base_ref) + '</td><td>' + (r.n_variants == null ? '' : r.n_variants) +\n      '</td><td>' + fmtP(r.p_bonf) + '</td><td>' + fmtP(r.sharpe) + '</td><td>' + esc(String(r.start_time || '').slice(0, 10)) + '</td></tr>';\n  }).join('') || '<tr><td colspan=10>无行</td></tr>';\n  // claims（分页）\n  const cq = document.getElementById('csearch').value.trim().toLowerCase();\n  const cst = document.getElementById('cstatus').value;\n  const call = (s.claims || []).filter(c =>\n    (!cst || c.status === cst) && (!cq || JSON.stringify(c).toLowerCase().includes(cq)));\n  const csize = pageSize('csize');\n  const crows = slicePage(call, 'c', csize);\n  document.getElementById('cpager').innerHTML = pagerHtml('c', pages.c.page, call.length, csize);\n  document.querySelector('#ctable tbody').innerHTML = crows.map(c => {\n    return '<tr class=\"clickable\" onclick=\"claimDetail(window.__claim_' + esc(c.claim_id).replace(/\\W/g, '_') + ')\"><td>' + esc(c.claim_id) + '</td><td>' + esc(c.status) + '</td><td>' + esc(String(c.text || '').slice(0, 120)) +\n      '</td><td>' + esc(c.source) + '</td><td>' + esc((c.linked_exp_ids || []).join(',')) + '</td></tr>';\n  }).join('') || '<tr><td colspan=5>无匹配 claim</td></tr>';\n  (s.claims || []).forEach(c => { window['__claim_' + String(c.claim_id).replace(/\\W/g, '_')] = c; });\n  // 事件\n  document.querySelector('#etable tbody').innerHTML = (s.events || []).map(e => {\n    return '<tr><td>' + esc(e.ts) + '</td><td>' + (e.job_id == null ? '' : e.job_id) + '</td><td>' + esc(e.exp_id || '') +\n      '</td><td class=\"' + stCls(e.status) + '\">' + esc(e.status) + '</td><td>' + esc(String(e.error || '').slice(0, 140)) + '</td></tr>';\n  }).join('') || '<tr><td colspan=5>无事件</td></tr>';\n}\n['qbatch', 'qstatus', 'bview', 'csearch', 'cstatus'].forEach(id => {\n  document.getElementById(id).addEventListener('input', () => {\n    const pid = id[0];\n    if (pages[pid]) pages[pid].page = 1;\n    if (__s) render(__s);\n  });\n});\n['qsize', 'bsize', 'csize'].forEach(id => {\n  document.getElementById(id).addEventListener('change', () => { if (__s) render(__s); });\n});\nload();\nsetInterval(load, 10000);\n</script>\n</body>\n</html>\n";

http.createServer((req, res) => {
  const send = (code, body, type) => {
    res.writeHead(code, { 'Content-Type': type || 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
    res.end(body);
  };
  if (req.url === '/api/state') { send(200, JSON.stringify(state)); return; }
  let m = req.url.match(/^\/api\/job\/(\d+)$/);
  if (m) {
    try {
      const data = cached('job' + m[1], 30000, () =>
        containerCmd('python -m pipeline.queue show ' + Number(m[1])));
      send(200, data);
    } catch (e) { send(500, JSON.stringify({ error: String(e.message || e) })); }
    return;
  }
  m = req.url.match(/^\/api\/run\/([0-9a-f]+)$/);
  if (m) {
    try {
      const data = cached('run' + m[1], 60000, () => runDetail(m[1]));
      send(200, data);
    } catch (e) { send(500, JSON.stringify({ error: String(e.message || e) })); }
    return;
  }
  if (req.url === '/' || req.url === '/index.html') {
    send(200, HTML, 'text/html; charset=utf-8');
    return;
  }
  send(404, 'not found', 'text/plain');
}).listen(PORT, '127.0.0.1', () => {
  console.log('QLab webui listening on http://127.0.0.1:' + PORT);
});
