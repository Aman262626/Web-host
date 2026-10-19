/* Web-Host Console - Part 3/3: service detail, live logs, env, bot, settings, boot. */

/* ---------- service detail ---------- */
const TABS = [['logs','Live logs'],['deploys','Deploys'],['env','Environment'],['files','Files'],['shell','Shell'],['settings','Settings']];
async function viewService(name, tab) {
  const v = $('#view');
  const enc = encodeURIComponent(name);
  let s = await api('/services/' + enc);
  const hdr = s => head('<a href="#/services" style="text-decoration:none">← Services</a>', esc(s.name), '',
    `${pill(s.status)}${s.kind==='web' && s.status==='running' ? `<a class="btn sm" target="_blank" rel="noopener" href="${esc(s.path)}">${ic('ext')}Open</a>` : ''}
     <button class="btn sm" data-a="deploy">${ic('up')}Deploy</button>
     ${s.status==='running' ? `<button class="btn sm" data-a="restart">${ic('redo')}Restart</button><button class="btn sm" data-a="stop">${ic('stop')}Stop</button>` : `<button class="btn sm pri" data-a="start">${ic('play')}Start</button>`}`);
  v.innerHTML = `<div id="hd"></div><div class="tabs">${TABS.map(([k,l]) => `<a href="#/services/${enc}/${k}" class="${k===tab?'on':''}">${l}</a>`).join('')}</div><div id="tb"></div>`;
  const paintHead = () => {
    $('#hd').innerHTML = hdr(s);
    $('#hd').querySelectorAll('[data-a]').forEach(b => b.onclick = async () => {
      const a = b.dataset.a; b.disabled = true;
      try {
        if (a === 'deploy') { const fd = new FormData(); if (s.source === 'zip') { const f = await pickFile(); if (f === undefined) { b.disabled = false; return; } if (f) fd.append('file', f); } await upload(`/services/${enc}/deploy`, fd); toast('Deploy started', 'ok'); location.hash = `#/services/${enc}/deploys`; }
        else { await api(`/services/${enc}/${a}`, {method: 'POST'}); toast(a + ' ok', 'ok'); }
        s = await api('/services/' + enc); paintHead();
      } catch (e) { fail(e); b.disabled = false; }
    });
  };
  paintHead();
  every(async () => { try { const n = await api('/services/' + enc); if (n.status !== s.status || n.deploys.length !== s.deploys.length) { s = n; paintHead(); } else s = n; } catch {} }, 3000);
  const tb = $('#tb');
  ({logs: tabLogs, deploys: tabDeploys, env: tabEnv, files: tabFilesSvc, shell: tabShell, settings: tabSettings}[tab] || tabLogs)(tb, s, enc);
}
function pickFile() { return new Promise(res => { if (!confirm('Upload a new ZIP now?\n\nOK = choose a new ZIP\nCancel = redeploy existing files')) return res(null); const i = document.createElement('input'); i.type = 'file'; i.accept = '.zip'; i.onchange = () => res(i.files[0] || undefined); i.click(); }); }

/* live log terminal */
function terminal(el, {url, initial, label}) {
  let offset = null, follow = true, wrap = false, filter = '', buf = [], live = true, busy = false;
  el.innerHTML = `<div class="term-wrap"><div class="term-bar">${label || ''}<input type="text" id="flt" placeholder="Filter…" style="width:160px"><label class="row mute" style="gap:6px;font-size:13px"><span class="switch" style="transform:scale(.8)"><input type="checkbox" id="fol" checked><i></i></span>Follow</label><label class="row mute" style="gap:6px;font-size:13px"><span class="switch" style="transform:scale(.8)"><input type="checkbox" id="wr"><i></i></span>Wrap</label><span class="spacer"></span><span class="live" id="lv"><i></i>LIVE</span><button class="btn sm" id="cpl">${ic('copy')}Copy</button><button class="btn sm" id="dll">${ic('dl')}Save</button><button class="btn sm" id="clr">Clear view</button></div><div class="term" id="tm"><div class="empty">Waiting for output…</div></div></div>`;
  const tm = $('#tm', el);
  const cls = t => /(error|exception|traceback|failed|fatal|critical)/i.test(t) ? 'err' : /(warn)/i.test(t) ? 'warn' : /^\S*\s*==> /.test(t) ? 'step' : /^\S*\s*\$ /.test(t) ? 'cmd' : /(live in|success|started|ready|listening)/i.test(t) ? 'ok' : '';
  function paint() {
    const f = filter.toLowerCase(); const lines = f ? buf.filter(l => l.toLowerCase().includes(f)) : buf;
    if (!lines.length) { tm.innerHTML = `<div class="empty">${buf.length ? 'No lines match the filter' : 'No logs yet. Output appears here the moment it happens.'}</div>`; return; }
    tm.innerHTML = lines.slice(-2500).map(l => { const m = l.match(/^(\d\d:\d\d:\d\d) (.*)$/s); const ts = m ? m[1] : '', tx = m ? m[2] : l; let h = esc(tx); if (f) h = h.replace(new RegExp(f.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/&/g,'&amp;'), 'gi'), x => `<mark>${x}</mark>`); return `<div class="ln ${cls(tx)}"><span class="ts">${ts}</span><span>${h}</span></div>`; }).join('');
    if (follow) tm.scrollTop = tm.scrollHeight;
  }
  async function poll() {
    if (busy) return; busy = true;
    try {
      const d = await api(url + (offset === null ? '' : (url.includes('?') ? '&' : '?') + 'after=' + offset));
      if (offset !== null && d.offset < offset) { buf = []; }
      if (d.text) { const parts = d.text.split('\n'); if (parts[parts.length-1] === '') parts.pop(); buf = buf.concat(parts).slice(-5000); paint(); }
      else if (offset === null) paint();
      offset = d.offset; live = true;
      $('#lv', el).classList.toggle('off', !(d.status === 'running' || d.status === 'building'));
      $('#lv', el).lastChild.textContent = d.status === 'building' ? 'BUILDING' : d.status === 'running' ? 'LIVE' : d.status.toUpperCase();
    } catch { $('#lv', el).classList.add('off'); }
    busy = false;
  }
  tm.onscroll = () => { const atEnd = tm.scrollHeight - tm.scrollTop - tm.clientHeight < 30; if (!atEnd && follow) { follow = false; $('#fol', el).checked = false; } };
  $('#flt', el).oninput = e => { filter = e.target.value; paint(); };
  $('#fol', el).onchange = e => { follow = e.target.checked; if (follow) tm.scrollTop = tm.scrollHeight; };
  $('#wr', el).onchange = e => { tm.classList.toggle('wrap', e.target.checked); };
  $('#cpl', el).onclick = () => copy(buf.join('\n'));
  $('#clr', el).onclick = () => { buf = []; paint(); };
  $('#dll', el).onclick = () => { const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([buf.join('\n')], {type: 'text/plain'})); a.download = 'logs.txt'; a.click(); };
  poll(); every(poll, 1200);
}
function tabLogs(el, s, enc) {
  el.innerHTML = '<div id="t"></div>';
  terminal($('#t', el), {url: `/services/${enc}/logs?type=runtime`, label: '<b style="font-size:13px">Runtime</b>'});
}
function tabDeploys(el, s, enc) {
  const sel = s.deploys[0];
  el.innerHTML = `<div class="grid g-side" style="align-items:start"><div id="t"></div><div class="panel"><h3>History</h3><div class="list" id="dl">${s.deploys.length ? s.deploys.map(d => `<div class="item" style="cursor:pointer" data-id="${d.id}"><div><div class="nm">#${d.id} <span class="tag">${esc(d.trigger)}</span>${d.commit?` <span class="tag">${esc(d.commit)}</span>`:''}</div><div class="meta">${esc((d.started||'').replace('T',' '))}</div>${d.error?`<div class="meta" style="color:var(--err)">${esc(d.error)}</div>`:''}</div><span class="spacer"></span>${pill(d.status)}</div>`).join('') : '<div class="empty"><b>No deploys yet</b>Press Deploy to build &amp; start.</div>'}</div></div></div>`;
  const show = id => { stopTimers(); every(() => {}, 99999); const t = $('#t', el); t.innerHTML = ''; terminal(t, {url: `/services/${enc}/logs?type=deploy&id=${id}`, label: `<b style="font-size:13px">Deploy #${id}</b>`}); el.querySelectorAll('.item').forEach(i => i.style.background = +i.dataset.id === id ? 'var(--panel2)' : ''); };
  if (sel) show(sel.id); else $('#t', el).innerHTML = '<div class="panel empty"><b>Build output shows here</b></div>';
  el.querySelectorAll('.item[data-id]').forEach(i => i.onclick = () => { const id = +i.dataset.id; show(id); every(async () => { try { const n = await api('/services/' + enc); if (n.deploys.length !== s.deploys.length) route(); } catch {} }, 3000); });
}
async function tabEnv(el, s, enc) {
  const [own, glob] = await Promise.all([api(`/services/${enc}/env`), api('/env')]);
  let rows = Object.entries(own.env);
  const paint = () => {
    el.innerHTML = `<div class="grid g-side" style="align-items:start"><div class="panel"><h3>Environment variables</h3><div class="hint" style="margin:-6px 0 14px">Injected into the process. Changes apply on restart. Host secrets such as BOT_TOKEN are <b>not</b> inherited — set what your app needs here.</div><div id="rows">${rows.map(([k, val], i) => `<div class="envrow"><input type="text" data-k="${i}" value="${esc(k)}" placeholder="KEY"><input type="password" data-v="${i}" value="${esc(val)}" placeholder="value"><button class="btn sm icon-btn" data-eye="${i}" title="Show">${ic('eye')}</button><button class="btn sm icon-btn danger" data-rm="${i}">${ic('x')}</button></div>`).join('')}</div>
      <div class="row" style="margin-top:12px"><button class="btn sm" id="add">${ic('plus')}Add variable</button><button class="btn sm" id="bulk">Paste .env</button><span class="spacer"></span><label class="row mute" style="font-size:13px;gap:8px">Restart after saving<span class="switch"><input type="checkbox" id="rs" checked><i></i></span></label><button class="btn pri" id="sv">Save</button></div></div>
      <div class="panel"><h3>Shared variables</h3><div class="hint" style="margin:-6px 0 12px">Apply to every service. Per-service values win. Add <span class="mono">GITHUB_TOKEN</span> here to deploy private repos.</div><textarea id="g" style="min-height:140px" placeholder="TZ=Asia/Kolkata">${esc(Object.entries(glob.env).map(([k,v]) => k+'='+v).join('\n'))}</textarea><button class="btn sm" style="margin-top:10px" id="sg">Save shared</button></div></div>`;
    const sync = () => { const ks = [...el.querySelectorAll('[data-k]')], vs = [...el.querySelectorAll('[data-v]')]; rows = ks.map((k, i) => [k.value, vs[i].value]); };
    el.querySelectorAll('[data-rm]').forEach(b => b.onclick = () => { sync(); rows.splice(+b.dataset.rm, 1); paint(); });
    el.querySelectorAll('[data-eye]').forEach(b => b.onclick = () => { const i = el.querySelector(`[data-v="${b.dataset.eye}"]`); i.type = i.type === 'password' ? 'text' : 'password'; });
    $('#add', el).onclick = () => { sync(); rows.push(['', '']); paint(); };
    $('#bulk', el).onclick = () => { const m = modal('Paste .env', '<textarea id="bt" style="min-height:220px" placeholder="KEY=value"></textarea>', '<button class="btn" data-x>Cancel</button><button class="btn pri" id="ok">Import</button>'); $('[data-x]', m.el).onclick = m.close; $('#ok', m.el).onclick = () => { sync(); $('#bt', m.el).value.split('\n').forEach(l => { const i = l.indexOf('='); if (i > 0 && !l.trim().startsWith('#')) { const k = l.slice(0, i).replace(/^export\s+/, '').trim(), val = l.slice(i+1).trim().replace(/^["']|["']$/g, ''); const ex = rows.find(r => r[0] === k); ex ? ex[1] = val : rows.push([k, val]); } }); m.close(); paint(); }; };
    $('#sv', el).onclick = async () => { sync(); const env = {}; rows.forEach(([k, val]) => { if (k.trim()) env[k.trim()] = val; }); try { const d = await api(`/services/${enc}/env`, {method: 'PUT', json: {env, restart: $('#rs', el).checked}}); toast(d.restarted ? 'Saved & restarted' : 'Saved', 'ok'); } catch (e) { fail(e); } };
    $('#sg', el).onclick = async () => { const env = {}; $('#g', el).value.split('\n').forEach(l => { const i = l.indexOf('='); if (i > 0) env[l.slice(0, i).trim()] = l.slice(i+1).trim(); }); try { await api('/env', {method: 'PUT', json: {env}}); toast('Shared variables saved', 'ok'); } catch (e) { fail(e); } };
  };
  paint();
}
function tabFilesSvc(el, s, enc) { el.innerHTML = '<div class="panel" id="f"></div>'; mountFiles($('#f', el), 'services', s.name); }
function tabShell(el, s, enc) {
  el.innerHTML = `<div class="panel"><h3>Run a one-off command</h3><div class="hint" style="margin:-6px 0 12px">Runs inside the service folder with its environment (e.g. <span class="mono">pip list</span>, <span class="mono">ls</span>, <span class="mono">python manage.py migrate</span>). 60s limit.</div><div class="row" style="flex-wrap:nowrap"><input type="text" id="cmd" class="mono" placeholder="ls -la" autocomplete="off"><button class="btn pri" id="run">${ic('play')}Run</button></div><div class="term-wrap" style="margin-top:12px"><div class="term" id="out" style="height:380px"><div class="empty">Output appears here</div></div></div></div>`;
  const run = async () => { const c = $('#cmd', el).value.trim(); if (!c) return; $('#run', el).disabled = true; $('#out', el).innerHTML = '<div class="ln">running…</div>'; try { const d = await api(`/services/${enc}/exec`, {method: 'POST', json: {cmd: c}}); $('#out', el).innerHTML = `<div class="ln cmd">$ ${esc(c)}</div>` + d.output.split('\n').map(l => `<div class="ln ${/error|traceback/i.test(l)?'err':''}">${esc(l)}</div>`).join('') + `<div class="ln ${d.code ? 'err' : 'ok'}">exit ${d.code}</div>`; } catch (e) { fail(e); } $('#run', el).disabled = false; };
  $('#run', el).onclick = run; $('#cmd', el).onkeydown = e => e.key === 'Enter' && run();
}
async function tabSettings(el, s, enc) {
  const hook = `${location.origin}/hooks/${encodeURIComponent(s.name)}/${s.hook_token}`;
  const caps = (await api('/services')).caps;
  el.innerHTML = `<div class="grid g2" style="align-items:start"><div class="panel"><h3>Build &amp; run</h3>
    <div class="field"><label class="f">Runtime</label><div class="seg" id="rt"><button data-v="native" class="${s.runtime==='native'?'on':''}">Native</button><button data-v="docker" class="${s.runtime==='docker'?'on':''}" ${caps.docker?'':'disabled'}>Docker</button></div>${caps.docker?'':`<div class="hint">Docker not available here: ${esc(caps.docker_reason)}</div>`}</div>
    <div class="field"><label class="f">Type</label><div class="seg" id="kd"><button data-v="worker" class="${s.kind==='worker'?'on':''}">Worker</button><button data-v="web" class="${s.kind==='web'?'on':''}">Web</button></div></div>
    <div class="field"><label class="f">Build command</label><input type="text" id="b" class="mono" value="${esc(s.build_cmd)}" placeholder="auto-detected"></div>
    <div class="field"><label class="f">Start command</label><input type="text" id="st" class="mono" value="${esc(s.start_cmd)}" placeholder="auto-detected"></div>
    ${s.source==='git'?`<div class="field"><label class="f">Branch</label><input type="text" id="br" value="${esc(s.branch)}" placeholder="default"></div>`:''}
    <div class="field"><label class="f">Port ${s.kind==='web'?'':'<span class="dim">(web only)</span>'}</label><input type="number" id="pt" value="${s.port||''}" placeholder="auto"><div class="hint">Your app must listen on the <span class="mono">$PORT</span> environment variable.</div></div>
    <div class="item"><div><div class="nm">Auto-restart on crash</div><div class="meta">Exponential back-off, gives up after 5 crashes in 5 min</div></div><span class="spacer"></span><label class="switch"><input type="checkbox" id="ar" ${s.auto_restart?'checked':''}><i></i></label></div>
    <button class="btn pri" style="margin-top:14px" id="sv">Save settings</button></div>
    <div class="grid" style="align-content:start"><div class="panel"><h3>Auto-deploy webhook</h3>${s.source==='git'?`<div class="hint" style="margin:-6px 0 12px">Add this URL as a webhook in GitHub (push events, content-type JSON) to redeploy on every push.</div><div class="row" style="flex-wrap:nowrap"><input type="text" readonly value="${esc(hook)}" class="mono" onclick="this.select()"><button class="btn sm" id="ch">${ic('copy')}</button></div><button class="btn sm" style="margin-top:10px" id="rh">Regenerate URL</button>` : '<div class="hint">Available for Git-sourced services.</div>'}</div>
    <div class="panel"><h3>Resources</h3>${s.metrics.length>2?`<div class="mute mono" style="font-size:12px;margin-bottom:4px">CPU %</div>${spark(s.metrics.map(m=>m[1]),360,50,'#ffc933')}<div class="mute mono" style="font-size:12px;margin:10px 0 4px">Memory · ${bytes(s.metrics[s.metrics.length-1][2])}</div>${spark(s.metrics.map(m=>m[2]),360,50,'#7cc4ff')}`:'<div class="hint">Metrics appear while the service is running.</div>'}</div>
    <div class="panel"><h3>Danger zone</h3><div class="row">${s.source==='git'?'<button class="btn" id="cl">Clean redeploy (re-clone)</button>':''}<button class="btn danger" id="dx">${ic('trash')}Delete service</button></div></div></div></div>`;
  const seg = id => { const b = $(`#${id} .on`, el); return b && b.dataset.v; };
  ['rt','kd'].forEach(id => $('#'+id, el).querySelectorAll('button').forEach(b => b.onclick = () => { if (b.disabled) return; $('#'+id, el).querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b)); }));
  $('#sv', el).onclick = async () => { const body = {runtime: seg('rt'), kind: seg('kd'), build_cmd: $('#b', el).value, start_cmd: $('#st', el).value, port: $('#pt', el).value, auto_restart: $('#ar', el).checked}; if ($('#br', el)) body.branch = $('#br', el).value; try { await api('/services/' + enc, {method: 'PATCH', json: body}); toast('Saved. Redeploy to apply build changes.', 'ok'); } catch (e) { fail(e); } };
  if ($('#ch', el)) $('#ch', el).onclick = () => copy(hook);
  if ($('#rh', el)) $('#rh', el).onclick = async () => { await api(`/services/${enc}/regen-hook`, {method: 'POST'}); route(); };
  if ($('#cl', el)) $('#cl', el).onclick = async () => { try { await upload(`/services/${enc}/deploy?clean=1`, new FormData()); location.hash = `#/services/${enc}/deploys`; } catch (e) { fail(e); } };
  $('#dx', el).onclick = async () => { if (await confirmBox('Delete ' + s.name + '?', 'The process is stopped and all its files, logs and variables are removed.')) { try { await api('/services/' + enc, {method: 'DELETE'}); location.hash = '#/services'; toast('Deleted', 'ok'); } catch (e) { fail(e); } } };
}

/* ---------- telegram ---------- */
async function viewBot(v) {
  const d = await api('/bot');
  v.innerHTML = head('Telegram', 'Bot control', 'Manage the built-in hosting bot and the people using it.') +
  `<div class="grid g3"><div class="panel tile"><div class="k">Status</div><div class="v" style="font-size:26px">${d.bot.running ? '<span style="color:var(--ok)">Online</span>' : '<span style="color:var(--err)">Offline</span>'}</div><div class="sub">${d.bot.running ? '@'+esc(d.bot.username) : esc(d.bot.error || 'Set BOT_TOKEN to enable')}</div></div>
   <div class="panel tile"><div class="k">Users</div><div class="v">${d.users.length}</div><div class="sub">${d.users.filter(u => u.banned).length} banned</div></div>
   <div class="panel tile"><div class="k">Admin ID</div><div class="v mono" style="font-size:22px">${d.admin_id || '—'}</div><div class="sub">${d.admin_id ? 'gets deploy/crash alerts' : 'set ADMIN_ID for alerts & admin commands'}</div></div></div>
  <div class="grid g-side" style="margin-top:14px;align-items:start"><div class="panel" style="padding:6px 6px 0"><table class="t"><thead><tr><th>User</th><th class="hide-m">Sites</th><th>Last active</th><th></th></tr></thead><tbody>${d.users.length ? d.users.map(u => `<tr><td><b>${esc(u.name || '?')}</b> <span class="mute">${u.username ? '@'+esc(u.username) : ''}</span><div class="mono dim" style="font-size:11px">${esc(u.id)}</div></td><td class="hide-m mono">${u.sites}</td><td class="mute">${ago(u.last_active)}</td><td style="text-align:right"><button class="btn sm ${u.banned?'':'danger'}" data-b="${esc(u.id)}" data-ban="${u.banned?0:1}">${u.banned?'Unban':'Ban'}</button></td></tr>`).join('') : '<tr><td colspan="4"><div class="empty"><b>No users yet</b>Send /start to your bot.</div></td></tr>'}</tbody></table></div>
  <div class="grid" style="align-content:start"><div class="panel"><h3>Broadcast</h3><textarea id="bm" placeholder="Message to every user…" style="min-height:110px"></textarea><button class="btn pri" style="margin-top:10px;width:100%" id="bs" ${d.token_set?'':'disabled'}>${ic('send')}Send to ${d.users.length} users</button></div>
  <div class="panel"><h3>Bot commands</h3><div class="mono" style="font-size:12.5px;line-height:1.9;color:var(--mute)"><b style="color:var(--text)">Everyone</b><br>/start /mysites /info /qr<br>/rename /delete /expire /stats<br><b style="color:var(--text)">Admin</b><br>/status /services /logs /deploy<br>/restart /svcstart /svcstop<br>/setenv /env /unsetenv<br>/broadcast /ban /unban /panel</div></div></div></div>`;
  v.querySelectorAll('[data-b]').forEach(b => b.onclick = async () => { try { await api('/bot/' + (b.dataset.ban === '1' ? 'ban' : 'unban'), {method: 'POST', json: {id: b.dataset.b}}); route(); } catch (e) { fail(e); } });
  $('#bs').onclick = async () => { const m = $('#bm').value.trim(); if (!m) return; if (!await confirmBox('Send broadcast?', `This messages ${d.users.length} users.`, 'Send')) return; try { const r = await api('/bot/broadcast', {method: 'POST', json: {message: m}}); toast(`Sending to ${r.queued} users`, 'ok'); $('#bm').value = ''; } catch (e) { fail(e); } };
}

/* ---------- settings ---------- */
async function viewSettings(v) {
  const [s, ev] = await Promise.all([api('/settings'), api('/events')]);
  v.innerHTML = head('Configuration', 'Settings', 'Read-only view of the running configuration. Change values in your host’s environment settings.') +
  (s.password_generated ? `<div class="callout err"><b>⚠</b><div>No <span class="mono">ADMIN_PASSWORD</span> is set, so your API key doubles as the login password. Set <span class="mono">ADMIN_PASSWORD</span> in the environment and redeploy.</div></div>` : '') +
  `<div class="grid g2" style="align-items:start"><div class="panel"><h3>Instance</h3>${[['Public URL', s.domain],['Data directory', s.data_dir],['Persistent disk', s.persistent ? 'yes' : 'NO — data lost on redeploy'],['Telegram bot token', s.bot_token_set ? 'set' : 'not set'],['Admin Telegram ID', s.admin_id || 'not set'],['Max ZIP upload', s.max_upload_mb+' MB'],['Max unzipped', s.max_unzipped_mb+' MB'],['Sites per user', s.max_sites_per_user]].map(([k,val]) => `<div class="item"><span class="mute">${k}</span><span class="spacer"></span><span class="mono">${esc(val)}</span></div>`).join('')}</div>
  <div class="grid" style="align-content:start"><div class="panel"><h3>API access</h3><div class="hint" style="margin:-6px 0 12px">Deploy from CI or scripts with the <span class="mono">X-API-Key</span> header.</div><div class="row" style="flex-wrap:nowrap"><input type="password" id="ak" class="mono" readonly value="${esc(s.api_key)}"><button class="btn sm" id="ek">${ic('eye')}</button><button class="btn sm" id="ck">${ic('copy')}</button></div><pre class="mono" style="font-size:12px;color:var(--mute);margin-top:14px;white-space:pre-wrap;word-break:break-all">curl -X POST ${esc(location.origin)}/api/upload \\
  -H "X-API-Key: YOUR_KEY" \\
  -F site_name=my-site -F file=@site.zip</pre></div>
  <div class="panel"><h3>Event log</h3><div style="max-height:360px;overflow:auto">${evHtml(ev.events)}</div></div></div></div>`;
  $('#ek').onclick = () => { const i = $('#ak'); i.type = i.type === 'password' ? 'text' : 'password'; };
  $('#ck').onclick = () => copy(s.api_key);
}

/* ---------- boot ---------- */
function boot() {
  if (!TOKEN) return renderLogin();
  api('/me').then(() => { shell(); route(); }).catch(() => renderLogin());
}
window.addEventListener('hashchange', () => { if (TOKEN && $('#view')) route(); });
boot();
