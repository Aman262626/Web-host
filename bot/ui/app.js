/* Web-Host Console - vanilla JS SPA (no build step). Part 1/3: core, auth, router, overview.
   Loaded together with app-sites.js and app-services.js as plain scripts sharing one scope. */
const $ = (s, r = document) => r.querySelector(s);
const app = $('#app');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const I = {
  grid:'<path d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z"/>',
  globe:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3.2 3 14.8 0 18M12 3c-3 3.2-3 14.8 0 18"/>',
  box:'<path d="M21 8l-9-5-9 5v8l9 5 9-5zM3 8l9 5 9-5M12 13v8"/>',
  send:'<path d="M22 2L11 13M22 2l-7 20-4-9-9-4z"/>',
  cog:'<circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 00-.1-1.2l2-1.5-2-3.4-2.3.9a7 7 0 00-2-1.2L14 3h-4l-.6 2.6a7 7 0 00-2 1.2l-2.3-.9-2 3.4 2 1.5a7 7 0 000 2.4l-2 1.5 2 3.4 2.3-.9a7 7 0 002 1.2L10 21h4l.6-2.6a7 7 0 002-1.2l2.3.9 2-3.4-2-1.5c.1-.4.1-.8.1-1.2z"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  up:'<path d="M12 16V4M6 10l6-6 6 6M4 20h16"/>',
  play:'<path d="M6 4l14 8-14 8z"/>',stop:'<rect x="5" y="5" width="14" height="14" rx="2"/>',
  redo:'<path d="M21 12a9 9 0 11-3-6.7M21 4v5h-5"/>',
  trash:'<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  folder:'<path d="M3 6a2 2 0 012-2h4l2 3h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2z"/>',
  file:'<path d="M14 3H6a2 2 0 00-2 2v14a2 2 0 002 2h12a2 2 0 002-2V8zM14 3v5h5"/>',
  ext:'<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5"/>',
  dl:'<path d="M12 4v12M6 12l6 6 6-6M4 20h16"/>',
  x:'<path d="M6 6l12 12M18 6L6 18"/>',
  copy:'<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 012-2h9"/>',
  out:'<path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9"/>',
  eye:'<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8S1 12 1 12z"/><circle cx="12" cy="12" r="3"/>',
};
const ic = (n, c='') => `<svg class="${c}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${I[n]}</svg>`;

/* ---------- api ---------- */
let TOKEN = localStorage.getItem('wh_token') || '';
async function api(path, opts = {}) {
  const o = {method: opts.method || 'GET', headers: {}};
  if (TOKEN) o.headers.Authorization = 'Bearer ' + TOKEN;
  if (opts.json !== undefined) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(opts.json); }
  if (opts.form) o.body = opts.form;
  const r = await fetch('/console/api' + path, o);
  let d = {}; try { d = await r.json(); } catch {}
  if (r.status === 401 && path !== '/login') { logout(true); throw new Error('Session expired'); }
  if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}
function upload(path, form, onprog) {
  return new Promise((res, rej) => {
    const x = new XMLHttpRequest();
    x.open('POST', '/console/api' + path);
    x.setRequestHeader('Authorization', 'Bearer ' + TOKEN);
    x.upload.onprogress = e => e.lengthComputable && onprog && onprog(e.loaded / e.total);
    x.onload = () => { let d = {}; try { d = JSON.parse(x.responseText); } catch {} x.status < 300 ? res(d) : rej(new Error(d.error || 'HTTP ' + x.status)); };
    x.onerror = () => rej(new Error('Network error'));
    x.send(form);
  });
}
function toast(msg, kind = '') {
  const t = document.createElement('div'); t.className = 'toast ' + kind; t.textContent = msg;
  $('#toasts').appendChild(t); setTimeout(() => t.remove(), 4200);
}
const fail = e => toast(e.message || String(e), 'err');
const bytes = n => { n = +n || 0; const u = ['B','KB','MB','GB']; let i = 0; while (n >= 1024 && i < 3) { n /= 1024; i++; } return (i ? n.toFixed(1) : n) + ' ' + u[i]; };
const dur = s => { s = +s || 0; const d = Math.floor(s/86400), h = Math.floor(s%86400/3600), m = Math.floor(s%3600/60); return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : m ? `${m}m ${s%60}s` : `${s}s`; };
const ago = iso => { if (!iso) return 'never'; const s = (Date.now() - new Date(iso)) / 1000; return s < 60 ? 'just now' : s < 3600 ? Math.floor(s/60)+'m ago' : s < 86400 ? Math.floor(s/3600)+'h ago' : Math.floor(s/86400)+'d ago'; };
const pill = s => `<span class="pill ${esc(s)}"><i></i>${esc(s)}</span>`;
const copy = t => navigator.clipboard.writeText(t).then(() => toast('Copied', 'ok'), () => toast('Copy failed', 'err'));
async function confirmBox(title, text, label = 'Delete') {
  return new Promise(res => {
    const m = modal(title, `<p class="mute">${text}</p>`, `<button class="btn" data-x>Cancel</button><button class="btn danger" data-ok>${label}</button>`);
    m.el.querySelector('[data-x]').onclick = () => { m.close(); res(false); };
    m.el.querySelector('[data-ok]').onclick = () => { m.close(); res(true); };
  });
}
function modal(title, body, footer = '', wide = false) {
  const s = document.createElement('div'); s.className = 'scrim';
  s.innerHTML = `<div class="modal ${wide ? 'wide' : ''}" role="dialog"><header><h2>${esc(title)}</h2><button class="btn ghost icon-btn" data-close>${ic('x')}</button></header><div class="body">${body}</div>${footer ? `<footer>${footer}</footer>` : ''}</div>`;
  document.body.appendChild(s);
  const close = () => s.remove();
  s.addEventListener('mousedown', e => { if (e.target === s) close(); });
  s.querySelector('[data-close]').onclick = close;
  return {el: s, close};
}

/* ---------- auth ---------- */
function logout(silent) { TOKEN = ''; localStorage.removeItem('wh_token'); stopTimers(); renderLogin(); if (!silent) toast('Signed out'); }
function logo(sz = 34) { return `<svg class="mark" width="${sz}" height="${sz}" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#ffc933"/><path d="M7 10h18v13H7z" fill="none" stroke="#1a1400" stroke-width="2.4"/><path d="M7 15h18" stroke="#1a1400" stroke-width="2.4"/><circle cx="10.5" cy="12.5" r="1" fill="#1a1400"/><circle cx="14" cy="12.5" r="1" fill="#1a1400"/></svg>`; }
function renderLogin() {
  app.innerHTML = `<div class="login"><form class="card reveal" id="lf">${logo(46)}<h1>Web-Host Console</h1><p>Sign in to deploy, monitor and manage your sites, apps and bots.</p>
  <div class="field"><label class="f">Admin password</label><input type="password" id="pw" autofocus autocomplete="current-password" placeholder="••••••••"></div>
  <button class="btn pri" style="width:100%" id="lb">Sign in</button><p class="hint" style="margin:14px 0 0">Password = <span class="mono">ADMIN_PASSWORD</span> (or <span class="mono">API_KEY</span>) from your environment settings. If you set neither, it is printed once in the deploy logs.</p></form></div>`;
  $('#lf').onsubmit = async e => {
    e.preventDefault(); const b = $('#lb'); b.disabled = true;
    try { const d = await api('/login', {method: 'POST', json: {password: $('#pw').value}}); TOKEN = d.token; localStorage.setItem('wh_token', TOKEN); boot(); }
    catch (err) { fail(err); b.disabled = false; }
  };
}

/* ---------- shell & router ---------- */
let timers = [];
function every(fn, ms) { timers.push(setInterval(fn, ms)); }
function stopTimers() { timers.forEach(clearInterval); timers = []; }
let OV = null;
const NAV = [['overview','grid','Overview'],['sites','globe','Sites'],['services','box','Services'],['bot','send','Telegram'],['settings','cog','Settings']];
function shell() {
  app.innerHTML = `<div class="shell"><aside class="rail"><div class="brand">${logo()}<div><b>Web-Host</b><small>Console</small></div></div>
  <nav class="nav">${NAV.map(([k,i,l]) => `<a href="#/${k}" data-n="${k}">${ic(i)}<span>${l}</span><span class="count" id="c-${k}"></span></a>`).join('')}</nav>
  <div class="rail-foot"><div class="mute">Live at</div><div class="host">${esc(location.host)}</div><button class="btn sm" style="width:100%" id="lo">${ic('out')}Sign out</button></div></aside>
  <main id="view"></main></div>`;
  $('#lo').onclick = () => logout();
}
function route() {
  const h = location.hash.replace(/^#\/?/, '') || 'overview';
  const [page, a, b] = h.split('/');
  stopTimers();
  document.querySelectorAll('.nav a').forEach(x => x.classList.toggle('on', x.dataset.n === page));
  const v = $('#view'); v.innerHTML = '';
  const map = {overview: viewOverview, sites: viewSites, services: () => a ? viewService(decodeURIComponent(a), b || 'logs') : viewServices(), bot: viewBot, settings: viewSettings};
  (map[page] || viewOverview)(v).catch(fail);
  refreshCounts();
}
async function refreshCounts() {
  try {
    const o = await api('/overview'); OV = o;
    const s = $('#c-sites'), v = $('#c-services'); if (s) s.textContent = o.sites.count; if (v) v.textContent = o.services.length;
  } catch {}
}
const head = (eyebrow, title, sub, actions = '') => `<div class="page-head"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1>${sub ? `<p>${sub}</p>` : ''}</div><div class="row">${actions}</div></div>`;

/* ---------- charts ---------- */
function spark(data, w = 120, h = 32, color = '#ffc933') {
  const max = Math.max(1, ...data), step = w / Math.max(1, data.length - 1);
  const pts = data.map((v, i) => `${(i*step).toFixed(1)},${(h - 3 - (v/max)*(h-6)).toFixed(1)}`);
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><polyline points="${pts.join(' ')}" fill="none" stroke="${color}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/><polyline points="0,${h} ${pts.join(' ')} ${w},${h}" fill="${color}" fill-opacity=".12" stroke="none"/></svg>`;
}
function bars(data, labels) {
  const max = Math.max(1, ...data), w = 560, h = 150, bw = w / data.length;
  return `<svg class="chart" viewBox="0 0 ${w} ${h+22}">${data.map((v,i) => { const bh = Math.max(2, v/max*h); return `<rect x="${i*bw+4}" y="${h-bh}" width="${bw-8}" height="${bh}" rx="3" fill="${i===data.length-1?'#ffc933':'#3a3f27'}"><title>${labels[i]}: ${v}</title></rect>`; }).join('')}
  <text x="0" y="${h+16}" fill="#5d5d4d" font-size="11" font-family="Geist Mono,monospace">${labels[0]}</text><text x="${w}" y="${h+16}" text-anchor="end" fill="#5d5d4d" font-size="11" font-family="Geist Mono,monospace">today</text></svg>`;
}
const meter = (label, val, extra) => `<div class="meter"><div class="top"><span>${label}</span><b>${extra}</b></div><div class="bar"><span class="${val>90?'crit':val>75?'hot':''}" style="width:${Math.min(100,val)}%"></span></div></div>`;
const days14 = () => Array.from({length: 14}, (_, i) => { const d = new Date(Date.now() - (13-i)*864e5); return (d.getMonth()+1)+'/'+d.getDate(); });
const evHtml = evs => evs.length ? evs.map(e => `<div class="ev"><i class="d ${esc(e.level)}"></i><div><div>${esc(e.msg)}</div><time>${esc((e.t||'').replace('T',' '))}</time></div></div>`).join('') : '<div class="empty"><b>Quiet so far</b>Deploys, crashes and restarts will show up here.</div>';

/* ---------- overview ---------- */
async function viewOverview(v) {
  const draw = o => {
    OV = o; const sy = o.system, c = o.caps;
    const running = o.services.filter(s => s.status === 'running').length, bad = o.services.filter(s => ['failed','crashed'].includes(s.status)).length;
    v.innerHTML = head('Dashboard', 'Everything, at a glance', `Up ${dur(o.uptime)} · ${esc(o.domain)}`,
      `<button class="btn" id="ns">${ic('up')}Upload site</button><button class="btn pri" id="nv">${ic('plus')}New service</button>`) +
    (o.persistent ? '' : `<div class="callout"><b>⚠</b><div><b>Data is not on a persistent disk.</b> On Render's free tier everything (sites, services, env vars) is wiped on every redeploy. Attach a Disk and set <span class="mono">DATA_DIR=/data</span>, or use the Docker setup in the README.</div></div>`) +
    `<div class="grid g4">
      <div class="panel tile reveal" style="--i:0"><div class="k">Static sites</div><div class="v">${o.sites.count}</div><div class="sub">${bytes(o.sites.size)} · ${o.sites.visits} total visits</div></div>
      <div class="panel tile reveal" style="--i:1"><div class="k">Services</div><div class="v">${running}<small>/ ${o.services.length} running</small></div><div class="sub" style="color:${bad?'var(--err)':'var(--mute)'}">${bad ? bad+' need attention' : 'all healthy'}</div></div>
      <div class="panel tile reveal" style="--i:2"><div class="k">Bot users</div><div class="v">${o.users}</div><div class="sub">${o.bot.running ? '@'+esc(o.bot.username)+' online' : esc(o.bot.error || 'bot offline')}</div></div>
      <div class="panel tile reveal" style="--i:3"><div class="k">Visits · 14d</div><div class="v" style="align-items:center">${o.sites.spark.reduce((a,b)=>a+b,0)}<span style="margin-left:auto">${spark(o.sites.spark)}</span></div></div>
    </div>
    <div class="grid g-side" style="margin-top:14px">
      <div class="grid" style="align-content:start">
        <div class="panel reveal" style="--i:4"><h3>Traffic · last 14 days</h3>${bars(o.sites.spark, days14())}</div>
        <div class="panel reveal" style="--i:5"><h3>Services</h3>${o.services.length ? `<div class="list">${o.services.map(s => `<a class="item" href="#/services/${encodeURIComponent(s.name)}" style="text-decoration:none"><div><div class="nm">${esc(s.name)}</div><div class="meta">${s.kind} · ${s.runtime}${s.status==='running'?' · up '+dur(s.uptime):''}</div></div><span class="spacer"></span>${pill(s.status)}</a>`).join('')}</div>` : `<div class="empty"><b>No services yet</b>Deploy a Python/Node app, a Telegram bot or any Docker project.<br><br><button class="btn pri" id="nv2">${ic('plus')}New service</button></div>`}</div>
      </div>
      <div class="grid" style="align-content:start">
        <div class="panel reveal" style="--i:6"><h3>Server</h3>
          ${meter('CPU', sy.cpu, sy.cpu.toFixed(0)+'% · '+sy.cores+' cores')}${meter('Memory', sy.mem_percent, bytes(sy.mem_used)+' / '+bytes(sy.mem_total))}${meter('Disk', sy.disk.percent, bytes(sy.disk.used)+' / '+bytes(sy.disk.total))}</div>
        <div class="panel reveal" style="--i:7"><h3>Runtimes</h3><div class="grid" style="gap:8px">
          ${[['Docker', c.docker, c.docker ? 'daemon reachable' : c.docker_reason],['Git', c.git, c.git ? 'ready for repo deploys' : 'not installed'],['Node.js', !!c.node, c.node || 'not installed'],['Python', true, c.python]].map(([n,ok,t]) => `<div class="cap"><i class="dot" style="background:${ok?'var(--ok)':'var(--dim)'}"></i><div><b>${n}</b><small>${esc(t)}</small></div></div>`).join('')}</div></div>
        <div class="panel reveal" style="--i:8"><h3>Activity</h3>${evHtml(o.events.slice(0, 8))}</div>
      </div></div>`;
    ['nv','nv2'].forEach(id => { const b = $('#'+id); if (b) b.onclick = () => newServiceModal(); });
    $('#ns').onclick = newSiteModal;
  };
  draw(await api('/overview'));
  every(async () => { try { const o = await api('/overview'); if (location.hash.replace(/^#\/?/, '') .startsWith('overview') || !location.hash) draw(o); } catch {} }, 6000);
}
