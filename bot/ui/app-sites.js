/* Web-Host Console - Part 2/3: sites, file manager, services list, new-service form. */

/* ---------- sites ---------- */
function newSiteModal() {
  let file = null;
  const m = modal('Upload a static site', `<div class="field"><label class="f">Site name</label><input type="text" id="sn" placeholder="my-portfolio" autocomplete="off"><div class="hint">Your site will live at <span class="mono">${esc(location.host)}/<b id="sp">name</b>/</span></div></div>
    <div class="drop" id="dz"><b>Drop a .zip here</b><span class="mute">or click to choose · index.html at the top level</span></div><input type="file" id="fi" accept=".zip" class="hidden"><div class="progress hidden" id="pg"><span></span></div>`,
    `<button class="btn" data-x>Cancel</button><button class="btn pri" id="go">${ic('up')}Deploy</button>`);
  const dz = $('#dz', m.el), fi = $('#fi', m.el), sn = $('#sn', m.el);
  const slug = s => s.toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '').slice(0, 40);
  sn.oninput = () => $('#sp', m.el).textContent = slug(sn.value) || 'name';
  const pick = f => { if (!f) return; file = f; dz.classList.add('has'); dz.innerHTML = `<b>${esc(f.name)}</b><span class="mute">${bytes(f.size)}</span>`; if (!sn.value) { sn.value = slug(f.name.replace(/\.zip$/i, '')); sn.oninput(); } };
  dz.onclick = () => fi.click(); fi.onchange = () => pick(fi.files[0]);
  dz.ondragover = e => { e.preventDefault(); dz.classList.add('over'); }; dz.ondragleave = () => dz.classList.remove('over');
  dz.ondrop = e => { e.preventDefault(); dz.classList.remove('over'); pick(e.dataTransfer.files[0]); };
  $('[data-x]', m.el).onclick = m.close;
  $('#go', m.el).onclick = async () => {
    if (!file) return toast('Choose a ZIP file first', 'err');
    const fd = new FormData(); fd.append('name', sn.value); fd.append('file', file);
    const pg = $('#pg', m.el); pg.classList.remove('hidden'); $('#go', m.el).disabled = true;
    try { const d = await upload('/sites', fd, p => pg.firstElementChild.style.width = (p*100)+'%'); toast('Deployed ' + d.name, 'ok'); m.close(); route(); }
    catch (e) { fail(e); $('#go', m.el).disabled = false; pg.classList.add('hidden'); }
  };
}
async function viewSites(v) {
  const d = await api('/sites');
  v.innerHTML = head('Static hosting', 'Sites', 'HTML/CSS/JS sites served instantly from a ZIP.', `<button class="btn pri" id="ns">${ic('up')}Upload site</button>`) +
  (d.sites.length ? `<div class="panel" style="padding:6px 6px 0"><table class="t"><thead><tr><th>Site</th><th class="hide-m">Files</th><th class="hide-m">Traffic · 14d</th><th>Visits</th><th></th></tr></thead><tbody>
  ${d.sites.map(s => `<tr><td><b>${esc(s.name)}</b>${s.expires?` <span class="tag acc">expires ${esc(s.expires.slice(0,10))}</span>`:''}<div class="meta mono mute" style="font-size:12px">${esc(s.url)}</div></td>
  <td class="hide-m mono mute">${s.files} · ${bytes(s.size)}</td><td class="hide-m">${spark(s.spark, 100, 26)}</td><td class="mono">${s.visits}<div class="dim" style="font-size:11px">${ago(s.last_visit)}</div></td>
  <td><div class="row" style="justify-content:flex-end;flex-wrap:nowrap"><a class="btn sm" href="${esc(s.url)}" target="_blank" rel="noopener">${ic('ext')}Open</a><button class="btn sm" data-manage="${esc(s.name)}">Manage</button></div></td></tr>`).join('')}</tbody></table></div>`
  : `<div class="panel empty"><b>No sites yet</b>Drop in a ZIP and get a live link in seconds.<br><br><button class="btn pri" id="ns2">${ic('up')}Upload your first site</button></div>`);
  ['ns','ns2'].forEach(id => { const b = $('#'+id); if (b) b.onclick = newSiteModal; });
  v.querySelectorAll('[data-manage]').forEach(b => b.onclick = () => siteModal(d.sites.find(s => s.name === b.dataset.manage)));
}
function siteModal(s) {
  const m = modal(s.name, `<div class="row" style="margin-bottom:14px"><a class="btn sm" href="${esc(s.url)}" target="_blank" rel="noopener">${ic('ext')}Open</a><button class="btn sm" id="cp">${ic('copy')}Copy URL</button><a class="btn sm" id="dl" href="#">${ic('dl')}Download ZIP</a><button class="btn sm" id="rn">Rename</button></div>
    <div class="grid g3" style="margin-bottom:16px"><div class="cap"><div><b>${s.files}</b><small>files</small></div></div><div class="cap"><div><b>${bytes(s.size)}</b><small>size</small></div></div><div class="cap"><div><b>${s.visits}</b><small>visits</small></div></div></div>
    <div class="item"><div><div class="nm">Single-page app mode</div><div class="meta">Unknown routes fall back to index.html</div></div><span class="spacer"></span><label class="switch"><input type="checkbox" id="spa" ${s.spa?'checked':''}><i></i></label></div>
    <div class="item"><div><div class="nm">Allow site to use cookies / localStorage</div><div class="meta">Off = sandboxed for safety. Turn on only for your own trusted sites.</div></div><span class="spacer"></span><label class="switch"><input type="checkbox" id="iso" ${s.isolation==='off'?'checked':''}><i></i></label></div>
    <div class="item"><div><div class="nm">Auto-delete</div><div class="meta">Remove this site after a time</div></div><span class="spacer"></span><select id="exp" style="width:auto"><option value="">Never</option><option value="1">1 hour</option><option value="24">24 hours</option><option value="168">7 days</option><option value="720">30 days</option></select></div>
    <h3 style="margin:22px 0 10px;font-size:12px;letter-spacing:.14em;color:var(--mute);text-transform:uppercase">Files</h3><div id="fmx"></div>`,
    `<button class="btn danger" id="del">${ic('trash')}Delete site</button><span class="spacer"></span><button class="btn" data-x>Close</button>`, true);
  $('[data-x]', m.el).onclick = m.close;
  mountFiles($('#fmx', m.el), 'sites', s.name);
  $('#cp', m.el).onclick = () => copy(s.url);
  $('#dl', m.el).onclick = e => { e.preventDefault(); downloadZip('sites', s.name); };
  const set = body => api(`/sites/${encodeURIComponent(s.name)}/settings`, {method: 'POST', json: body}).then(() => toast('Saved', 'ok')).catch(fail);
  $('#spa', m.el).onchange = e => set({spa: e.target.checked});
  $('#iso', m.el).onchange = e => set({isolation: e.target.checked ? 'off' : 'sandbox'});
  $('#exp', m.el).onchange = e => set({expires_hours: e.target.value || null});
  $('#rn', m.el).onclick = async () => { const n = prompt('New name', s.name); if (!n) return; try { await api(`/sites/${encodeURIComponent(s.name)}/rename`, {method: 'POST', json: {name: n}}); m.close(); route(); toast('Renamed', 'ok'); } catch (e) { fail(e); } };
  $('#del', m.el).onclick = async () => { if (await confirmBox('Delete ' + s.name + '?', 'The site and its files are removed permanently.')) { try { await api('/sites/' + encodeURIComponent(s.name), {method: 'DELETE'}); m.close(); route(); toast('Deleted', 'ok'); } catch (e) { fail(e); } } };
}
async function downloadZip(scope, name) {
  try {
    const r = await fetch(`/console/api/${scope}/${encodeURIComponent(name)}/download`, {headers: {Authorization: 'Bearer ' + TOKEN}});
    if (!r.ok) throw new Error('Download failed');
    const a = document.createElement('a'); a.href = URL.createObjectURL(await r.blob()); a.download = name + '.zip'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  } catch (e) { fail(e); }
}

/* ---------- file manager (sites & services) ---------- */
function mountFiles(el, scope, name) {
  let cwd = '', cur = null, dirty = false;
  const base = `/${scope}/${encodeURIComponent(name)}`;
  el.innerHTML = `<div class="fm"><div class="tree"><div class="crumbs" id="cr"></div><div id="ls"></div><div style="padding:10px;display:flex;gap:6px"><button class="btn sm" id="up">${ic('up')}Upload</button><button class="btn sm" id="nf">${ic('plus')}New file</button></div><input type="file" id="fu" class="hidden"></div>
  <div class="editor"><div class="bar2"><span id="fn" class="mute">Select a file to view or edit</span><span class="spacer"></span><button class="btn sm hidden" id="sv">Save</button><button class="btn sm danger hidden" id="rm">${ic('trash')}</button></div><textarea id="ed" class="hidden" spellcheck="false"></textarea><div class="empty" id="em" style="margin:auto"><b>No file open</b>Edits are saved to disk${scope==='services'?'. Redeploy or restart to apply':''}.</div></div></div>`;
  const q = s => $(s, el);
  async function load() {
    const d = await api(`${base}/files?path=${encodeURIComponent(cwd)}`);
    const parts = cwd ? cwd.split('/') : [];
    q('#cr').innerHTML = `<a data-p="">root</a>` + parts.map((p, i) => ` / <a data-p="${esc(parts.slice(0, i+1).join('/'))}">${esc(p)}</a>`).join('');
    q('#cr').querySelectorAll('a').forEach(a => a.onclick = () => { cwd = a.dataset.p; load().catch(fail); });
    q('#ls').innerHTML = d.entries.length ? d.entries.map(e => `<div class="ent ${cur === (cwd ? cwd+'/' : '')+e.name ? 'on' : ''}" data-n="${esc(e.name)}" data-d="${e.dir ? 1 : 0}">${ic(e.dir ? 'folder' : 'file')}<span class="nm">${esc(e.name)}</span><span class="sz">${e.dir ? '' : bytes(e.size)}</span></div>`).join('') : '<div class="empty">Empty folder</div>';
    q('#ls').querySelectorAll('.ent').forEach(n => n.onclick = () => { const p = (cwd ? cwd+'/' : '') + n.dataset.n; if (n.dataset.d === '1') { cwd = p; load().catch(fail); } else open(p).catch(fail); });
  }
  async function open(p) {
    if (dirty && !confirm('Discard unsaved changes?')) return;
    const d = await api(`${base}/file?path=${encodeURIComponent(p)}`);
    cur = p; dirty = false; q('#fn').textContent = p; q('#rm').classList.remove('hidden');
    if (d.binary) { q('#ed').classList.add('hidden'); q('#sv').classList.add('hidden'); q('#em').classList.remove('hidden'); q('#em').innerHTML = `<b>${esc(d.reason)}</b>${bytes(d.size)}`; }
    else { q('#em').classList.add('hidden'); q('#ed').classList.remove('hidden'); q('#ed').value = d.content; q('#sv').classList.remove('hidden'); }
    load();
  }
  q('#ed').oninput = () => dirty = true;
  q('#ed').onkeydown = e => { if (e.key === 'Tab') { e.preventDefault(); const t = e.target, s = t.selectionStart; t.value = t.value.slice(0, s) + '  ' + t.value.slice(t.selectionEnd); t.selectionStart = t.selectionEnd = s + 2; dirty = true; } if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); q('#sv').click(); } };
  q('#sv').onclick = async () => { try { await api(`${base}/file`, {method: 'PUT', json: {path: cur, content: q('#ed').value}}); dirty = false; toast('Saved', 'ok'); } catch (e) { fail(e); } };
  q('#rm').onclick = async () => { if (!await confirmBox('Delete file?', esc(cur))) return; try { await api(`${base}/file?path=${encodeURIComponent(cur)}`, {method: 'DELETE'}); cur = null; q('#fn').textContent = 'Select a file'; q('#ed').classList.add('hidden'); q('#sv').classList.add('hidden'); q('#rm').classList.add('hidden'); q('#em').classList.remove('hidden'); load(); } catch (e) { fail(e); } };
  q('#nf').onclick = async () => { const n = prompt('File name (e.g. config.json)'); if (!n) return; try { await api(`${base}/file`, {method: 'PUT', json: {path: (cwd ? cwd+'/' : '') + n, content: ''}}); await load(); open((cwd ? cwd+'/' : '') + n); } catch (e) { fail(e); } };
  q('#up').onclick = () => q('#fu').click();
  q('#fu').onchange = async e => { const f = e.target.files[0]; if (!f) return; const fd = new FormData(); fd.append('file', f); fd.append('path', cwd); try { await upload(`${base}/file`, fd); toast('Uploaded', 'ok'); load(); } catch (er) { fail(er); } };
  load().catch(fail);
}

/* ---------- services list ---------- */
async function viewServices(v = $("#view")) {
  const draw = d => {
    v.innerHTML = head('Compute', 'Services', 'Web apps, APIs and always-on workers (Telegram bots) — run natively or in Docker, with live logs and auto-restart.', `<button class="btn pri" id="nv">${ic('plus')}New service</button>`) +
    (d.services.length ? `<div class="grid g3">${d.services.map((s, i) => `<a class="panel hover svc reveal" style="--i:${i};text-decoration:none;display:block" href="#/services/${encodeURIComponent(s.name)}">
      <div class="top"><div class="nm">${esc(s.name)}</div>${pill(s.status)}</div>${s.kind==='web' ? `<span class="url">${esc(location.origin)}${esc(s.path)}</span>` : '<span class="mute" style="font-size:13px">Background worker</span>'}
      <div class="facts"><span class="tag">${s.kind}</span><span class="tag ${s.runtime==='docker'?'acc':''}">${s.runtime}</span><span class="tag">${s.source}</span>${s.last_commit?`<span class="tag">${esc(s.last_commit)}</span>`:''}</div>
      <div class="meta mono mute" style="font-size:12px">${s.status==='running' ? 'up '+dur(s.uptime) : 'not running'} · ${s.env_count} env var${s.env_count===1?'':'s'}${s.restarts?' · '+s.restarts+' restarts':''}</div>
      ${s.metrics.length > 3 ? `<div style="margin-top:10px">${spark(s.metrics.map(m => m[1]), 220, 28, '#3ee6a0')}</div>` : ''}</a>`).join('')}</div>`
    : `<div class="panel empty"><b>Nothing deployed yet</b>Your Telegram bot, a Flask/FastAPI API, a Node app or a Docker project can live here.<br><br><button class="btn pri" id="nv2">${ic('plus')}Create your first service</button></div>`);
    ['nv','nv2'].forEach(id => { const b = $('#'+id); if (b) b.onclick = () => newServiceModal(d.caps); });
  };
  const d = await api('/services'); draw(d);
  every(async () => { try { draw(await api('/services')); } catch {} }, 5000);
}

function newServiceModal(caps) {
  const st = {kind: 'worker', source: 'zip', runtime: 'native', file: null};
  const m = modal('New service', `
  <div class="field"><label class="f">Name</label><input type="text" id="nm" placeholder="my-telegram-bot" autocomplete="off"></div>
  <div class="field"><label class="f">Type</label><div class="seg" id="kind"><button data-v="worker" class="on">Worker<small>Telegram bot, scripts, jobs</small></button><button data-v="web">Web service<small>Serves HTTP at /svc/name/</small></button></div></div>
  <div class="field"><label class="f">Source</label><div class="seg" id="source"><button data-v="zip" class="on">Upload ZIP<small>Your project files</small></button><button data-v="git">Git repository<small>GitHub / GitLab URL</small></button></div></div>
  <div id="zipbox" class="field"><div class="drop" id="dz"><b>Drop project .zip here</b><span class="mute">or click · max 100MB</span></div><input type="file" id="fi" accept=".zip" class="hidden"></div>
  <div id="gitbox" class="hidden"><div class="field"><label class="f">Your GitHub repositories</label><div class="row" style="flex-wrap:nowrap"><select id="ghsel"><option value="">Click Load to list your repos</option></select><button class="btn sm" type="button" id="ghload">Load</button></div><div class="hint" id="ghhint">Needs a <span class="mono">GITHUB_TOKEN</span> variable (private repos too). Or just paste a URL below.</div></div><div class="field"><label class="f">Repository URL</label><input type="text" id="repo" placeholder="https://github.com/you/bot.git"><div class="hint">Private repo? Set <span class="mono">GITHUB_TOKEN</span> (token is never stored in the repo or shown in logs).</div></div><div class="field"><label class="f">Branch (optional)</label><input type="text" id="branch" placeholder="main"></div></div>
  <div class="field"><label class="f">Runtime</label><div class="seg" id="runtime"><button data-v="native" class="on">Native<small>Runs directly (fast)</small></button><button data-v="docker" id="dk">Docker<small>Uses your Dockerfile</small></button></div><div class="hint" id="dkh"></div></div>
  <div class="field"><label class="f">Start command <span class="dim">(optional — auto-detected)</span></label><input type="text" id="start" placeholder="python bot.py"></div>
  <div class="field"><label class="f">Build command <span class="dim">(optional)</span></label><input type="text" id="build" placeholder="pip install -r requirements.txt"></div>
  <div class="field"><label class="f">Environment variables <span class="dim">(KEY=value per line)</span></label><textarea id="env" placeholder="BOT_TOKEN=123:ABC&#10;API_KEY=secret"></textarea></div>`,
  `<button class="btn" data-x>Cancel</button><button class="btn pri" id="go">${ic('play')}Create &amp; deploy</button>`, true);
  const q = s => $(s, m.el);
  $('[data-x]', m.el).onclick = m.close;
  (async () => { const c = caps || (await api('/services')).caps; if (!c.docker) { q('#dk').disabled = true; q('#dkh').innerHTML = `Docker daemon not available on this host (${esc(c.docker_reason)}). Native mode is used. A Dockerfile is still read to detect your start command &amp; install steps.`; } else q('#dkh').textContent = 'Docker is available on this host.'; })().catch(() => {});
  ['kind','source','runtime'].forEach(k => q('#'+k).querySelectorAll('button').forEach(b => b.onclick = () => { if (b.disabled) return; st[k] = b.dataset.v; q('#'+k).querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b)); q('#zipbox').classList.toggle('hidden', st.source !== 'zip'); q('#gitbox').classList.toggle('hidden', st.source !== 'git'); }));
  q('#ghload').onclick = async () => { try { const d = await api('/github/repos'); q('#ghsel').innerHTML = '<option value="">Select a repository (' + d.repos.length + ')</option>' + d.repos.map((r, i) => `<option value="${i}">${esc(r.full_name)}${r.private ? ' (private)' : ''}</option>`).join(''); q('#ghsel').onchange = () => { const r = d.repos[+q('#ghsel').value]; if (!r) return; q('#repo').value = r.clone_url; q('#branch').value = r.branch; if (!q('#nm').value) q('#nm').value = r.full_name.split('/')[1].toLowerCase().replace(/[^a-z0-9-]+/g, '-').slice(0, 40); }; q('#ghhint').textContent = 'Pick one - URL and branch fill in automatically.'; q('#ghhint').style.color = ''; } catch (e) { q('#ghhint').textContent = e.message; q('#ghhint').style.color = 'var(--err)'; } };
  const dz = q('#dz'), fi = q('#fi');
  const slug = s => s.toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '').slice(0, 40);
  const pick = f => { if (!f) return; st.file = f; dz.classList.add('has'); dz.innerHTML = `<b>${esc(f.name)}</b><span class="mute">${bytes(f.size)}</span>`; if (!q('#nm').value) q('#nm').value = slug(f.name.replace(/\.zip$/i, '')); };
  dz.onclick = () => fi.click(); fi.onchange = () => pick(fi.files[0]);
  dz.ondragover = e => { e.preventDefault(); dz.classList.add('over'); }; dz.ondragleave = () => dz.classList.remove('over'); dz.ondrop = e => { e.preventDefault(); dz.classList.remove('over'); pick(e.dataTransfer.files[0]); };
  q('#go').onclick = async () => {
    const env = {}; q('#env').value.split('\n').forEach(l => { const i = l.indexOf('='); if (i > 0 && !l.trim().startsWith('#')) env[l.slice(0, i).trim()] = l.slice(i+1).trim().replace(/^["']|["']$/g, ''); });
    const spec = {name: q('#nm').value, kind: st.kind, source: st.source, runtime: st.runtime, repo: q('#repo').value, branch: q('#branch').value, start_cmd: q('#start').value, build_cmd: q('#build').value, env};
    if (st.source === 'zip' && !st.file) return toast('Choose a ZIP file', 'err');
    const fd = new FormData(); fd.append('spec', JSON.stringify(spec)); if (st.file) fd.append('file', st.file);
    q('#go').disabled = true;
    try { const d = await upload('/services', fd); m.close(); toast('Deploy started', 'ok'); location.hash = `#/services/${encodeURIComponent(d.name)}/deploys`; }
    catch (e) { fail(e); q('#go').disabled = false; }
  };
}
