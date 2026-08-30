// notify_bridge.js - host-side bridge between the QLab queue and DeepSeek Harness.
// 1) polls queue events (pipeline.queue notify) and completion lines (pipeline.queue
//    notify-done) with the two-phase protocol: peek -> post to DSH -> ack (advance marker)
//    ONLY after the post succeeded. Failed posts re-peek and re-post next round.
//    Appends everything to inbox.jsonl; posts failure / completion / batch-summary texts
//    into the DSH session via POST /api/session.prompt; shows a Windows toast (best effort).
// 2) monitors dispatcher heartbeat; stale heartbeat + running jobs -> auto-heal + notify.
// usage: node notify_bridge.js [--interval 15] [--once] [--session <id>] [--no-dsh]
// env: QLAB_NOTIFY_DIR (default <repo>/notify)
//      QLAB_CONTAINER  (default hermes-1679f5b2, 仅容器模式使用)
//      QLAB_DSH_URL    (default http://127.0.0.1:3080)
// 默认【本机直跑】（不加 docker）：直接在本机跑 python -m pipeline.queue
// （cwd=QLAB_ROOT，venv python），通知数据落 <repo>/notify/。
// 容器模式仅在显式 QLAB_BRIDGE_CONTAINER=1 时启用（Windows 主机等仍有容器的环境）。
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

// 仓库根（notify_bridge.js 在仓库根）与 venv python（没有则退 python3）
const QLAB_ROOT = process.env.QLAB_ROOT || path.resolve(__dirname);
const BASE = process.env.QLAB_NOTIFY_DIR || path.join(QLAB_ROOT, 'notify');
const CONTAINER = process.env.QLAB_CONTAINER || 'hermes-1679f5b2';
const DSH_URL = process.env.QLAB_DSH_URL || 'http://127.0.0.1:3080';
const PY_LOCAL = fs.existsSync(path.join(QLAB_ROOT, '.venv', 'bin', 'python'))
  ? path.join(QLAB_ROOT, '.venv', 'bin', 'python') : 'python3';
// 本机直跑为默认；容器模式仅 QLAB_BRIDGE_CONTAINER=1 显式开启（且 docker 可达）。
// QLAB_BRIDGE_LOCAL=1 仍可强制本机（兼容旧用法）。
let useLocal = process.env.QLAB_BRIDGE_LOCAL === '1' || process.env.QLAB_BRIDGE_CONTAINER !== '1';
if (!useLocal) {
  try { execFileSync('docker', ['exec', '-i', CONTAINER, 'true'], { stdio: 'ignore', timeout: 10000 }); }
  catch (e) { useLocal = true; }
}
// 插话模式（steer）：消息直接插进会话而不是进队列堆积（2026-08-23 用户指定）
const DSH_MODE = process.env.QLAB_DSH_MODE || 'steer';
const INBOX = path.join(BASE, 'inbox.jsonl');
const SESSION_FILE = path.join(BASE, 'session.txt');
const STATE_FILE = path.join(BASE, 'bridge_state.json');
const STALE_MS = 5 * 60 * 1000;
const RENOTIFY_MS = 15 * 60 * 1000;

const interval = (function () { const i = process.argv.indexOf('--interval'); return i >= 0 ? Number(process.argv[i + 1]) : 15; })();
const once = process.argv.includes('--once');
const noDsh = process.argv.includes('--no-dsh');
const sessionArg = (function () { const i = process.argv.indexOf('--session'); return i >= 0 ? process.argv[i + 1] : ''; })();

function getSessionId() {
  if (sessionArg) return sessionArg;
  try { return fs.readFileSync(SESSION_FILE, 'utf8').trim(); } catch (e) { return ''; }
}

function loadState() {
  try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); } catch (e) { return {}; }
}

function saveState(s) {
  fs.mkdirSync(BASE, { recursive: true });
  fs.writeFileSync(STATE_FILE, JSON.stringify(s));
}

function dockerExec(args) {
  return execFileSync('docker', ['exec', '-i', CONTAINER].concat(args),
    { encoding: 'utf8', timeout: 120000 }).trim();
}

function queueLocal(args) {
  return execFileSync(PY_LOCAL, ['-m', 'pipeline.queue'].concat(args),
    { encoding: 'utf8', timeout: 120000, cwd: QLAB_ROOT,
      env: Object.assign({}, process.env, { QLAB_ROOT: QLAB_ROOT }) }).trim();
}

function queueCommand(args) {
  if (useLocal) return queueLocal(args);
  return dockerExec(['sh', '-c', 'cd /root/quant && python -m pipeline.queue ' + args.join(' ')]);
}

function postToDsh(text) {
  const sid = getSessionId();
  if (!sid) { console.error('no session id (write one to ' + SESSION_FILE + ' or pass --session)'); return Promise.resolve({ ok: false, resp: 'no-session' }); }
  if (noDsh) { console.log('DSH post disabled (--no-dsh): ' + text.slice(0, 80)); return Promise.resolve({ ok: true, resp: 'disabled' }); }
  const body = JSON.stringify({
    type: 'client-request',
    rpcId: 'qlab-' + Date.now(),
    method: 'session.prompt',
    payload: { sessionId: sid, mode: DSH_MODE, content: [{ type: 'text', text: text }] },
  });
  return fetch(DSH_URL + '/api/session.prompt', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body,
  }).then(function (r) {
    return r.text().then(function (t) { return { ok: r.ok, resp: t.slice(0, 160) }; });
  }).catch(function (e) {
    return { ok: false, resp: 'post-failed: ' + String(e).slice(0, 120) };
  });
}

function toast(title, body) {
  const script = [
    "$ErrorActionPreference='SilentlyContinue'",
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null",
    "$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)",
    "$texts = $xml.GetElementsByTagName('text')",
    "$texts.Item(0).AppendChild($xml.CreateTextNode('" + title + "')) > $null",
    "$texts.Item(1).AppendChild($xml.CreateTextNode('" + body + "')) > $null",
    "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)",
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('PowerShell').Show($toast)",
  ].join('; ');
  try { execFileSync('powershell', ['-NoProfile', '-Command', script], { timeout: 15000 }); }
  catch (e) { console.error('toast failed (non-fatal):', String(e).slice(0, 120)); }
}

function buildFailureText(events) {
  const failed = events.filter(function (ev) { return ev.status === 'failed'; });
  if (failed.length === 0) return '';
  const lines = failed.map(function (ev) {
    const err = (ev.error || '无错误信息').slice(0, 120);
    return '- ' + ev.exp_id + '#job' + ev.job_id + ': ' + err;
  });
  return '【QLab 队列通知】' + failed.length + ' 个任务失败：\n' + lines.join('\n') +
    '\n请按 AGENTS.md 排查：python -m pipeline.queue show <job_id>，修复后重新 submit。';
}

function buildBackupText(events) {
  const pend = events.filter(function (ev) { return ev.status === 'backup_pending'; });
  if (pend.length === 0) return '';
  const lines = pend.map(function (ev) {
    return '- ' + (ev.error || '备份未推送（无 token）').slice(0, 140);
  });
  return '【QLab 备份】' + pend.length + ' 条备份未推送事件：\n' + lines.join('\n') +
    '\n请手动执行 python -m pipeline.backup push --message "..." 或配置 QLAB_GITHUB_TOKEN。';
}

function buildBlockedText(events, state) {
  // blocked is not terminal (spark unreachable / GPU busy): notify once per
  // job; the marker clears when the job leaves blocked so a later blocked
  // state can re-notify. Never spam one message per poll round.
  const seen = state.blockedNotified || {};
  const fresh = [];
  events.forEach(function (ev) {
    if (ev.status === 'blocked' && ev.job_id && !seen[ev.job_id]) {
      fresh.push(ev);
      seen[ev.job_id] = true;
    } else if (ev.job_id && seen[ev.job_id] && ev.status !== 'blocked') {
      delete seen[ev.job_id];  // requeued/unblocked/finished: arm re-notify
    }
  });
  state.blockedNotified = seen;
  if (fresh.length === 0) return '';
  const lines = fresh.map(function (ev) {
    return '- ' + ev.exp_id + '#job' + ev.job_id + '：' +
      (ev.error || 'spark 不可达').slice(0, 100);
  });
  return '【QLab 队列】' + fresh.length + ' 个任务 blocked（需人工处理）：\n' + lines.join('\n') +
    '\n处理：python -m pipeline.queue unblock <job_id>（强制本地）或 retry --blocked。';
}

function peekEvents() {
  try { const line = queueCommand(['notify', '--peek']); return line ? JSON.parse(line) : []; }
  catch (e) { console.error('notify peek failed:', String(e).slice(0, 150)); return null; }
}

function ackEvents(lastId) {
  try { queueCommand(['notify', '--ack', String(lastId)]); }
  catch (e) { console.error('notify ack failed:', String(e).slice(0, 150)); }
}

function pollEvents() {
  const events = peekEvents();
  if (events === null || events.length === 0) return;
  fs.mkdirSync(BASE, { recursive: true });
  fs.appendFileSync(INBOX, JSON.stringify(events) + '\n');
  const state = loadState();
  const maxId = Math.max.apply(null, events.map(function (ev) { return ev.id; }));
  // post succeeded earlier but ack failed: retry the ack only, never re-post
  if (state.lastAckedId && maxId <= state.lastAckedId) { ackEvents(maxId); return; }
  const failed = events.filter(function (ev) { return ev.status === 'failed'; });
  const backupPending = events.filter(function (ev) { return ev.status === 'backup_pending'; });
  const texts = [];
  const failText = buildFailureText(failed);
  if (failText) texts.push(failText);
  const backupText = buildBackupText(backupPending);
  if (backupText) texts.push(backupText);
  const blockedText = buildBlockedText(events, state);
  if (blockedText) texts.push(blockedText);
  // a batch that only failed/blocked produces no done line: summarize it from
  // the events themselves so an all-failed batch still gets its closure note
  const evSummary = batchSummary(events, state);
  if (evSummary) texts.push(evSummary);
  if (failed.length) {
    toast('QLab 队列：' + failed.length + ' 个任务失败',
      failed.map(function (ev) { return ev.exp_id + '#' + ev.job_id; }).join(', '));
  }
  let ok = true;
  (function next(i) {
    if (i >= texts.length) {
      if (ok) {
        state.lastAckedId = maxId;
        saveState(state);
        ackEvents(maxId);
      } else {
        console.log('post failed: NOT acking, will re-post next round');
      }
      return;
    }
    postToDsh(texts[i]).then(function (resp) {
      console.log('DSH response:', resp.resp);
      if (!resp.ok) ok = false;
      next(i + 1);
    });
  })(0);
}

function batchSummary(rows, state) {
  // for each referenced batch with no queued/running jobs left, append a terminal
  // summary line (once per batch; dedupe via state.batchSummarySent)
  const batches = [];
  rows.forEach(function (r) {
    if (r.batch_id && batches.indexOf(r.batch_id) < 0) batches.push(r.batch_id);
  });
  if (batches.length === 0) return '';
  let jobs = [];
  try { jobs = JSON.parse(queueCommand(['status', '--json'])); } catch (e) { jobs = []; }
  const sent = state.batchSummarySent || {};
  const lines = [];
  batches.forEach(function (bid) {
    if (sent[bid]) return;
    const mine = jobs.filter(function (j) { return j.batch_id === bid; });
    if (mine.length === 0) return;
    const active = mine.filter(function (j) { return j.status === 'queued' || j.status === 'running'; });
    if (active.length > 0) return;
    const doneN = mine.filter(function (j) { return j.status === 'done'; }).length;
    const failN = mine.filter(function (j) { return j.status === 'failed'; }).length;
    const blockedN = mine.filter(function (j) { return j.status === 'blocked'; }).length;
    const cancelledN = mine.filter(function (j) { return j.status === 'cancelled'; }).length;
    let line = '批次 ' + bid + ' 全部终态：done ' + doneN + ' / failed ' + failN +
      ' / blocked ' + blockedN + ' / cancelled ' + cancelledN;
    if (blockedN > 0) line += '（blocked 任务需人工 unblock）';
    lines.push(line);
    sent[bid] = true;
  });
  state.batchSummarySent = sent;
  return lines.length ? lines.join('\n') : '';
}

function peekDone() {
  try { const line = queueCommand(['notify-done', '--peek']); return line ? JSON.parse(line) : []; }
  catch (e) { console.error('notify-done peek failed:', String(e).slice(0, 150)); return null; }
}

function ackDone(lastId) {
  try { queueCommand(['notify-done', '--ack', String(lastId)]); }
  catch (e) { console.error('notify-done ack failed:', String(e).slice(0, 150)); }
}

function pollDoneEvents() {
  const rows = peekDone();
  if (rows === null || rows.length === 0) return;
  fs.mkdirSync(BASE, { recursive: true });
  fs.appendFileSync(INBOX, 'DONE ' + JSON.stringify(rows) + '\n');
  const state = loadState();
  const maxId = Math.max.apply(null, rows.map(function (r) { return r.id; }));
  const normal = rows.filter(function (r) { return r.kind !== 'round_end'; });
  const rounds = rows.filter(function (r) { return r.kind === 'round_end'; });
  const texts = [];
  if (normal.length) {
    const lines = normal.map(function (r) {
      const sig = (typeof r.rankic === 'number') ? String(r.rankic.toFixed(4)) : String(r.rankic);
      const p = (typeof r.p === 'number') ? String(r.p.toFixed(3)) : String(r.p);
      return '- ' + r.exp_id + '#job' + r.job_id + ': rankic=' + sig + ' p=' + p +
        ' expectation=' + (r.expectation_check || 'n/a');
    });
    texts.push('【QLab 队列】完成 ' + normal.length + ' 个任务：\n' + lines.join('\n'));
  }
  rounds.forEach(function (r) {
    const parts = (r.claimed || []).map(function (c) {
      return '- ' + c.exp_id + '#job' + c.job_id + ': ' + c.status;
    });
    const doneN = (r.claimed || []).filter(function (c) { return c.status === 'done'; }).length;
    const failN = (r.claimed || []).filter(function (c) { return c.status === 'failed'; }).length;
    const blockedN = (r.claimed || []).filter(function (c) { return c.status === 'blocked'; }).length;
    const rest = (typeof r.remaining_queued === 'number') ? r.remaining_queued : '?';
    texts.push('【QLab 队列】本轮 run（--once）结束：done ' + doneN + ' / failed ' + failN +
      ' / blocked ' + blockedN + '，队列剩余 ' + rest + ' 个排队任务\n' + parts.join('\n') +
      '\n继续：python -m pipeline.queue run --once（排空用 --watch）。');
  });
  const summary = batchSummary(rows, state);
  if (summary) texts.push(summary);
  const text = texts.join('\n');
  if (!text) {
    // nothing postable (e.g. only already-handled lines): ack and move on
    state.doneAckId = maxId;
    saveState(state);
    ackDone(maxId);
    return;
  }
  postToDsh(text).then(function (resp) {
    console.log('DSH response:', resp.resp);
    if (!resp.ok) { console.log('done post failed: NOT acking, will re-post next round'); return; }
    state.doneAckId = maxId;
    saveState(state);
    ackDone(maxId);
  });
}

function checkHeartbeat() {
  // heartbeat content is "<epoch> <pid>"; heal (inside the container) verifies PID
  // liveness before touching anything, so this bridge can never mis-kill long jobs.
  let raw = '';
  if (useLocal) {
    try { raw = fs.readFileSync(path.join(QLAB_ROOT, 'results/queue/heartbeat'), 'utf8'); } catch (e) { raw = ''; }
  } else {
    try { raw = dockerExec(['sh', '-c', 'cat /root/quant/results/queue/heartbeat 2>/dev/null']); } catch (e) { raw = ''; }
  }
  const toks = raw.trim().split(/\s+/);
  const epoch = toks.length ? Number(toks[0]) : NaN;
  const staleSec = STALE_MS / 1000;
  const age = Date.now() / 1000 - epoch;

  // fast path: fresh heartbeat by host clock -> nothing to do (reset dedupe state)
  if (!isNaN(epoch) && age < staleSec && age > -300) {
    const state = loadState();
    if (state.lastStaleNotify || state.lastMissingNotify) {
      state.lastStaleNotify = 0; state.lastMissingNotify = 0; saveState(state);
    }
    return;
  }

  // suspicious (stale, clock-ahead, or file missing): let container-side heal decide.
  // heal only mutates when heartbeat is stale AND the dispatcher PID is verifiably dead.
  let healOut = '';
  if (useLocal) {
    try { healOut = queueLocal(['heal']); }
    catch (e) { console.error('heal poll failed:', String(e).slice(0, 150)); return; }
  } else {
    try { healOut = dockerExec(['sh', '-c', 'cd /root/quant && python -m pipeline.queue heal']); }
    catch (e) { console.error('heal poll failed:', String(e).slice(0, 150)); return; }
  }
  let res = null;
  try { res = JSON.parse(healOut); } catch (e) {
    console.error('heal output not json:', healOut.slice(0, 200)); return;
  }

  if (res.state === 'healed') {
    const state = loadState();
    if (state.lastStaleNotify && Date.now() - state.lastStaleNotify < RENOTIFY_MS) return;
    state.lastStaleNotify = Date.now(); saveState(state);
    const jobs = (res.auto_healed || []).join(', ');
    const pid = res.heartbeat && res.heartbeat.pid ? res.heartbeat.pid : '?';
    const text = '【QLab 队列心跳】dispatcher 已确认死亡（PID ' + pid + ' 不存在），自动 heal：' +
      res.running + ' 个 running 任务置 failed' + (jobs ? '（job ' + jobs + '）' : '') +
      '。请检查日志并恢复现场：python -m pipeline.queue show <job_id>。';
    postToDsh(text).then(function (resp) { console.log('DSH response:', resp.resp); });
    toast('QLab 队列心跳失联', 'dispatcher 已确认死亡，已 heal ' + res.running + ' 个任务');
    return;
  }
  if (res.state === 'unknown') {
    // heartbeat file missing/unreadable: refuse to auto-kill, ask the agent instead
    const state = loadState();
    if (state.lastMissingNotify && Date.now() - state.lastMissingNotify < 30 * 60 * 1000) return;
    state.lastMissingNotify = Date.now(); saveState(state);
    const text = '【QLab 队列心跳】心跳文件缺失/不可读（' + (res.reason || '未知原因') + '），且有 ' +
      res.running + ' 个 running 任务。为防误杀未自动 heal，请人工核查 dispatcher 是否存活：' +
      (useLocal
        ? '本机 ps -ef | grep "pipeline.queue run"'
        : 'docker exec hermes-1679f5b2 sh -c "ps -ef | grep pipeline.queue"') +
      ' 后再决定 python -m pipeline.queue heal。';
    postToDsh(text).then(function (resp) { console.log('DSH response:', resp.resp); });
    toast('QLab 队列心跳文件缺失', '请人工核查 dispatcher');
    return;
  }
  // state ok (no running jobs) or alive_but_stale (PID alive): reset dedupe state, stay quiet
  const state = loadState();
  if (state.lastStaleNotify || state.lastMissingNotify) {
    state.lastStaleNotify = 0; state.lastMissingNotify = 0; saveState(state);
  }
  if (res.state === 'alive_but_stale') {
    console.log('heartbeat stale by host clock but dispatcher PID alive (clock drift?): ' +
      (res.reason || ''));
  }
}

function poll() {
  pollEvents();
  pollDoneEvents();
  checkHeartbeat();
  if (!once) setTimeout(poll, interval * 1000);
}

poll();
