// notify_bridge.js - host-side bridge between the QLab queue (Docker) and DeepSeek Harness.
// 1) polls queue events (pipeline.queue notify), appends to inbox.jsonl,
//    posts failure notifications into the DSH session via POST /api/session.prompt,
//    shows a Windows toast for the human (best effort).
// 2) monitors dispatcher heartbeat; stale heartbeat + running jobs -> auto-heal + notify.
// usage: node notify_bridge.js [--interval 15] [--once] [--session <id>] [--no-dsh]
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const BASE = 'D:/quant_backup/notify';
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
  return execFileSync('docker', ['exec', '-i', 'hermes-1679f5b2'].concat(args),
    { encoding: 'utf8', timeout: 60000 }).trim();
}

function postToDsh(text) {
  const sid = getSessionId();
  if (!sid) { console.error('no session id (write one to ' + SESSION_FILE + ' or pass --session)'); return 'no-session'; }
  if (noDsh) { console.log('DSH post disabled (--no-dsh): ' + text.slice(0, 80)); return 'disabled'; }
  const body = JSON.stringify({
    type: 'client-request',
    rpcId: 'qlab-' + Date.now(),
    method: 'session.prompt',
    payload: { sessionId: sid, mode: 'queue', content: [{ type: 'text', text: text }] },
  });
  return fetch('http://127.0.0.1:3080/api/session.prompt', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body,
  }).then(function (r) { return r.text(); }).then(function (t) { return t.slice(0, 160); })
    .catch(function (e) { return 'post-failed: ' + String(e).slice(0, 120); });
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

function pollEvents() {
  let line = '';
  try { line = dockerExec(['sh', '-c', 'cd /root/quant && python -m pipeline.queue notify']); }
  catch (e) { console.error('notify poll failed:', String(e).slice(0, 150)); return; }
  if (!line) return;
  fs.mkdirSync(BASE, { recursive: true });
  fs.appendFileSync(INBOX, line + '\n');
  let events = [];
  try { events = JSON.parse(line); } catch (e) { events = []; }
  const text = buildFailureText(events);
  if (text) {
    console.log('notify events: ' + events.length + ' lines');
    postToDsh(text).then(function (resp) { console.log('DSH response:', resp); });
    const failed = events.filter(function (ev) { return ev.status === 'failed'; });
    toast('QLab 队列：' + failed.length + ' 个任务失败',
      failed.map(function (ev) { return ev.exp_id + '#' + ev.job_id; }).join(', '));
  }
}

function checkHeartbeat() {
  // heartbeat content is "<epoch> <pid>"; heal (inside the container) verifies PID
  // liveness before touching anything, so this bridge can never mis-kill long jobs.
  let raw = '';
  try { raw = dockerExec(['sh', '-c', 'cat /root/quant/results/queue/heartbeat 2>/dev/null']); } catch (e) { raw = ''; }
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
  try { healOut = dockerExec(['sh', '-c', 'cd /root/quant && python -m pipeline.queue heal']); }
  catch (e) { console.error('heal poll failed:', String(e).slice(0, 150)); return; }
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
    postToDsh(text).then(function (resp) { console.log('DSH response:', resp); });
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
      'docker exec hermes-1679f5b2 sh -c "ps -ef | grep pipeline.queue" 后再决定 python -m pipeline.queue heal。';
    postToDsh(text).then(function (resp) { console.log('DSH response:', resp); });
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
  checkHeartbeat();
  if (!once) setTimeout(poll, interval * 1000);
}

poll();
