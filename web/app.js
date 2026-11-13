/* ═══════════════════════════════════════════════
   项目管家 · 前端逻辑
   数据全部来自本地服务 /api/*，无任何外网请求
   ═══════════════════════════════════════════════ */

const S = {
  projects: [],
  stats: { total: 0, dirty: 0, ahead: 0, missing: 0, mismatch: 0, linked: 0, unlinked: 0 },
  scanRoots: [],
  filter: 'all',
  tag: '',
  q: '',
  sort: 'recent',
  scanItems: [],
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const icon = (id, cls = 'ico') => `<svg class="${cls}"><use href="#${id}"></use></svg>`;

/* ───────── 与后端通信 ───────── */

async function api(path, body) {
  const opt = body === undefined
    ? { method: 'GET' }
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const res = await fetch(path, opt);
  let json;
  try {
    json = await res.json();
  } catch {
    throw new Error(`服务返回异常（HTTP ${res.status}）`);
  }
  if (!json.ok) throw new Error(json.error || '操作失败');
  if (json.msg) toast(json.msg);
  return json.data;
}

/* ───────── 小工具 ───────── */

let toastTimer;
function toast(msg, isErr = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 2400);
}

function relTime(iso) {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return '';
  const sec = (Date.now() - t) / 1000;
  if (sec < 60) return '刚刚';
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`;
  if (sec < 86400 * 30) return `${Math.floor(sec / 86400)} 天前`;
  if (sec < 86400 * 365) return `${Math.floor(sec / 86400 / 30)} 个月前`;
  return `${Math.floor(sec / 86400 / 365)} 年前`;
}

function avatarStyle(name) {
  let h = 0;
  for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `background: linear-gradient(135deg, hsl(${h} 64% 54%), hsl(${(h + 34) % 360} 66% 45%))`;
}

function firstChar(name) {
  const s = String(name || '?').trim();
  // 英文取首字母大写，中文取第一个字
  return /[a-z]/i.test(s[0]) ? s[0].toUpperCase() : s[0] || '?';
}

/* ───────── 数据加载 ───────── */

async function loadState({ probe = false } = {}) {
  const data = probe ? await api('/api/probe-all', {}) : await api('/api/state');
  S.projects = data.projects || [];
  S.stats = data.stats || S.stats;
  S.scanRoots = data.scan_roots || [];
  render();
}

let probing = false;
async function silentProbe() {
  if (probing) return;
  probing = true;
  const btn = $('#refreshBtn');
  btn.classList.add('spin');
  try {
    const data = await api('/api/probe-all', {});
    S.projects = data.projects || [];
    S.stats = data.stats || S.stats;
    render();
  } catch (e) {
    toast(e.message, true);
  } finally {
    probing = false;
    btn.classList.remove('spin');
  }
}

/* ───────── 筛选与排序 ───────── */

const NAVS = [
  { id: 'all', label: '全部项目', icon: 'i-folder' },
  { id: 'fav', label: '收藏', icon: 'i-star' },
  { id: 'dirty', label: '有未提交改动', dot: 'var(--amber)' },
  { id: 'ahead', label: '待推送', dot: 'var(--blue)' },
  { id: 'missing', label: '路径缺失', dot: 'var(--red)' },
  { id: 'mismatch', label: '远程不一致', dot: 'var(--red)' },
  { id: 'unlinked', label: '未关联仓库', dot: 'var(--text-3)' },
];

// 一个过滤器只写一遍，侧栏、统计条、实际筛选三处共用
const MATCH_FN = {
  fav: (p) => !!p.favorite,
  dirty: (p) => !!p.status?.dirty,
  ahead: (p) => (p.status?.ahead || 0) > 0,
  missing: (p) => !!p.status && !p.status.exists,
  mismatch: (p) => p.status?.match === false,
  unlinked: (p) => !p.github,
};

function countOf(id) {
  if (id === 'all') return S.projects.length;
  const fn = MATCH_FN[id];
  return fn ? S.projects.filter(fn).length : 0;
}

function setFilter(id) {
  S.filter = id || 'all';
  S.tag = '';           // 换类别时把标签条件清掉，免得出现「两个条件叠加后什么都没有」
  render();
}

function statusRank(p) {
  const st = p.status;
  if (!st) return 3;
  if (!st.exists) return 0;
  if (!st.is_git) return 2;
  if (st.dirty) return 1;
  return 4;
}

function visibleList() {
  let list = S.projects.slice();

  const q = S.q.trim().toLowerCase();
  if (q) {
    list = list.filter((p) => [
      p.name, p.path, p.github, p.description, p.language, (p.tags || []).join(' '),
    ].join(' ').toLowerCase().includes(q));
  }

  if (S.tag) list = list.filter((p) => (p.tags || []).includes(S.tag));

  if (S.filter === 'fav') list = list.filter(MATCH_FN.fav);
  else if (MATCH_FN[S.filter]) list = list.filter(MATCH_FN[S.filter]);

  const cmp = {
    recent: (a, b) => (b.status?.last_commit_at || '').localeCompare(a.status?.last_commit_at || ''),
    name: (a, b) => String(a.name).localeCompare(String(b.name), 'zh'),
    status: (a, b) => statusRank(a) - statusRank(b),
    added: (a, b) => (b.created_at || '').localeCompare(a.created_at || ''),
  }[S.sort] || (() => 0);

  // 收藏永远排前面，其余按选定规则
  list.sort((a, b) => (b.favorite ? 1 : 0) - (a.favorite ? 1 : 0) || cmp(a, b));
  return list;
}

/* ───────── 渲染 ───────── */

function render() {
  renderNav();
  renderTags();
  renderStats();
  renderCards();
}

function renderNav() {
  $('#nav').innerHTML = NAVS.map((n) => {
    const cnt = countOf(n.id);
    const lead = n.icon
      ? icon(n.icon)
      : `<span class="dot" style="background:${n.dot}"></span>`;
    return `<div class="nav-item ${S.filter === n.id ? 'on' : ''}" data-filter="${n.id}">
      ${lead}<span>${n.label}</span><span class="cnt">${cnt}</span>
    </div>`;
  }).join('');
}

function renderTags() {
  const map = new Map();
  S.projects.forEach((p) => (p.tags || []).forEach((t) => map.set(t, (map.get(t) || 0) + 1)));
  const tags = [...map.entries()].sort((a, b) => b[1] - a[1]);
  $('#tagCloud').innerHTML = tags.length
    ? tags.map(([t, c]) =>
        `<span class="tag-chip ${S.tag === t ? 'on' : ''}" data-tag="${esc(t)}">${esc(t)} ${c}</span>`).join('')
    : `<span style="font-size:12px;color:var(--text-3);padding:0 10px">还没有标签</span>`;
}

// 顶部统计条同时也是快捷筛选：点一下只看这一类，再点一下回到全部
const STAT_CELLS = [
  { id: 'all', icon: 'i-folder', key: 'total', label: '项目总数', tip: '显示全部项目' },
  { id: 'dirty', icon: 'i-edit', key: 'dirty', label: '有未提交改动', cls: 'a', tip: '只看工作区有改动、还没提交的项目' },
  { id: 'ahead', icon: 'i-refresh', key: 'ahead', label: '待推送', tip: '只看本地已有提交、还没推到 GitHub 的项目' },
  { id: 'missing', icon: 'i-close', key: 'missing', label: '路径缺失', cls: 'r', tip: '只看文件夹已被移动、改名或删掉的项目' },
  { id: 'mismatch', icon: 'i-alert', key: 'mismatch', label: '远程不一致', cls: 'r', tip: '只看仓库真实的 remote 与登记的 GitHub 地址对不上的项目' },
];

function renderStats() {
  const s = S.stats;
  $('#stats').innerHTML = STAT_CELLS.map((c) => {
    const n = s[c.key] || 0;
    return `<button class="stat ${S.filter === c.id ? 'on' : ''} ${n ? '' : 'zero'}"
      data-filter="${c.id}" title="${esc(c.tip)}">
      <span class="stat-i ${c.cls || ''}">${icon(c.icon)}</span>
      <span class="stat-body">
        <span class="stat-n">${n}</span>
        <span class="stat-l">${esc(c.label)}</span>
      </span>
    </button>`;
  }).join('');
}

function badgesFor(p) {
  const st = p.status;
  const out = [];
  if (!st) {
    out.push(['', '检查中…']);
  } else if (!st.exists) {
    out.push(['danger', '路径缺失']);
  } else if (!st.is_git) {
    out.push(['', '非 Git 仓库']);
  } else {
    out.push(st.dirty ? ['warn', `● 有改动 ${st.changed}`] : ['ok', '● 干净']);
    if (st.ahead > 0) out.push(['info', `↑ 待推送 ${st.ahead}`]);
    if (st.behind > 0) out.push(['info', `↓ 待拉取 ${st.behind}`]);
    if (st.match === false) {
      out.push(['danger', '远程不一致',
        `登记的是 ${p.github || '（空）'}\n实际 remote 是 ${st.remote || '（未设置）'}`]);
    }
  }
  if (st && st.is_git && st.branch && st.branch !== 'HEAD') out.push(['', st.branch]);
  if (p.language) out.push(['tag', p.language]);
  (p.tags || []).slice(0, 3).forEach((t) => out.push(['tag', t]));
  return out.map(([cls, txt, tip]) =>
    `<span class="badge ${cls}"${tip ? ` title="${esc(tip)}"` : ''}>${esc(txt)}</span>`).join('');
}

function cardHtml(p) {
  const st = p.status || {};
  const ghLabel = p.github ? 'GitHub' : '关联';
  const commitLine = st.last_commit_msg
    ? `<div class="commit">${icon('i-code', 'ico')}<span>${esc(relTime(st.last_commit_at))} · ${esc(st.last_commit_msg)}</span></div>`
    : (p.description ? `<div class="commit">${esc(p.description)}</div>` : '');

  return `<article class="card" data-id="${p.id}">
    <div class="card-head">
      <div class="avatar" style="${avatarStyle(p.name)}">${esc(firstChar(p.name))}</div>
      <div class="title-wrap">
        <h3 class="card-title" title="${esc(p.name)}">${esc(p.name)}</h3>
        <div class="card-path" data-act="copy" title="${esc(p.path)}">${esc(p.path)}</div>
      </div>
      <button class="fav ${p.favorite ? 'on' : ''}" data-act="fav" title="${p.favorite ? '取消收藏' : '收藏'}">
        ${icon(p.favorite ? 'i-star-fill' : 'i-star')}
      </button>
    </div>
    <div class="badges">${badgesFor(p)}</div>
    ${commitLine}
    <div class="card-actions">
      <button class="act" data-act="folder" title="在资源管理器里打开">${icon('i-folder')}打开</button>
      <button class="act ${p.github ? '' : 'ghostly'}" data-act="github" title="${p.github || '还没有关联 GitHub 仓库'}">
        ${icon('i-github')}${ghLabel}</button>
      <button class="act" data-act="vscode" title="用 VS Code 打开">${icon('i-code')}Code</button>
      <button class="act ghostly push" data-act="edit" title="编辑">${icon('i-edit')}</button>
      <button class="act ghostly danger" data-act="del" title="从列表移除">${icon('i-trash')}</button>
    </div>
  </article>`;
}

function renderCards() {
  const list = visibleList();
  const box = $('#content');

  if (!S.projects.length) {
    box.innerHTML = `<div class="empty">
      <div class="empty-ico">${icon('i-folder')}</div>
      <h3>还没有登记任何项目</h3>
      <p>最快的方式是让它自己找：选一个你放代码的目录，<br>它会把里面所有 Git 仓库连远程地址一起列出来。</p>
      <button class="btn primary" data-act="scan-top" style="margin:0 auto">
        ${icon('i-scan')}扫描本地目录</button>
    </div>`;
    return;
  }

  if (!list.length) {
    const filtering = S.filter !== 'all' || S.tag || S.q.trim();
    const label = (NAVS.find((n) => n.id === S.filter) || {}).label;
    box.innerHTML = `<div class="empty">
      <div class="empty-ico">${icon('i-search')}</div>
      <h3>这里没有符合条件的项目</h3>
      <p>${filtering && label ? `当前筛选：<b>${esc(label)}</b>（0 个）<br>` : ''}换个筛选条件，或者清空搜索框试试。</p>
      ${filtering ? `<button class="btn" data-act="clear-filter" style="margin:0 auto">
        ${icon('i-close')}清除所有筛选</button>` : ''}
    </div>`;
    return;
  }

  box.innerHTML = `<div class="grid">${list.map(cardHtml).join('')}</div>`;
}

/* ───────── 操作 ───────── */

async function doOpen(id, target) {
  try {
    await api('/api/open', { id, target });
  } catch (e) {
    toast(e.message, true);
  }
}

async function toggleFav(p) {
  try {
    await api('/api/projects', { ...p, favorite: !p.favorite, path: p.path });
    // 本地先更新，避免等一轮请求
    p.favorite = !p.favorite;
    render();
  } catch (e) {
    toast(e.message, true);
  }
}

async function removeProject(p) {
  if (!confirm(`把「${p.name}」从列表里移除？\n\n只是删掉这条记录，本地文件夹和 GitHub 仓库都不会动。`)) return;
  try {
    await fetch(`/api/projects/${p.id}`, { method: 'DELETE' })
      .then((r) => r.json())
      .then((j) => { if (!j.ok) throw new Error(j.error); });
    S.projects = S.projects.filter((x) => x.id !== p.id);
    toast('已移除');
    await loadState();
  } catch (e) {
    toast(e.message, true);
  }
}

async function copyPath(path) {
  try {
    await navigator.clipboard.writeText(path);
    toast('路径已复制');
  } catch {
    toast('复制失败，请手动选中', true);
  }
}

/* ───────── 新增 / 编辑弹窗 ───────── */

function openForm(p) {
  $('#formTitle').textContent = p ? '编辑项目' : '新增项目';
  $('#fId').value = p?.id || '';
  $('#fName').value = p?.name || '';
  $('#fPath').value = p?.path || '';
  $('#fGithub').value = p?.github || '';
  $('#fLang').value = p?.language || '';
  $('#fTags').value = (p?.tags || []).join(', ');
  $('#fDesc').value = p?.description || '';
  $('#fFav').checked = !!p?.favorite;
  $('#formOverlay').hidden = false;
  setTimeout(() => $('#fPath').focus(), 60);
}

async function submitForm(ev) {
  ev.preventDefault();
  const id = $('#fId').value;
  const payload = {
    name: $('#fName').value.trim(),
    path: $('#fPath').value.trim(),
    github: $('#fGithub').value.trim(),
    language: $('#fLang').value.trim(),
    tags: $('#fTags').value,
    description: $('#fDesc').value.trim(),
    favorite: $('#fFav').checked,
  };
  if (!payload.path) return toast('本地路径必填', true);

  try {
    if (id) await api(`/api/projects/${id}`, { ...payload, __patch: 1 });
    else await api('/api/projects', payload);
    $('#formOverlay').hidden = true;
    await loadState();
    silentProbe();
  } catch (e) {
    toast(e.message, true);
  }
}

/* ───────── 扫描弹窗 ───────── */

function openScan() {
  $('#scanOverlay').hidden = false;
  $('#rootHints').innerHTML = S.scanRoots.length
    ? S.scanRoots.slice(0, 5).map((r) => `<button data-root="${esc(r)}">${esc(r)}</button>`).join('')
    : '<span>还没有扫描记录</span>';
  if (!$('#sRoot').value && S.scanRoots.length) $('#sRoot').value = S.scanRoots[0];
  setTimeout(() => $('#sRoot').focus(), 60);
}

async function runScan() {
  const root = $('#sRoot').value.trim();
  if (!root) return toast('先填一个要扫描的目录', true);
  const btn = $('#scanRun');
  btn.disabled = true;
  btn.textContent = '扫描中…';
  $('#scanResult').innerHTML = '<div class="empty-sm">正在翻找 Git 仓库，目录大的话要等几秒…</div>';
  try {
    const data = await api('/api/scan', { root, depth: Number($('#sDepth').value) });
    S.scanItems = data.items || [];
    S.scanRoots = [data.root, ...S.scanRoots.filter((r) => r !== data.root)];
    renderScanResult();
    $('#rootHints').innerHTML = S.scanRoots.slice(0, 5)
      .map((r) => `<button data-root="${esc(r)}">${esc(r)}</button>`).join('');
  } catch (e) {
    $('#scanResult').innerHTML = `<div class="empty-sm">${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = '开始扫描';
  }
}

function renderScanResult() {
  const items = S.scanItems;
  if (!items.length) {
    $('#scanResult').innerHTML = '<div class="empty-sm">这个目录里没找到 Git 仓库，换个目录或加大层数试试。</div>';
    $('#scanInfo').textContent = '';
    $('#scanImport').disabled = true;
    return;
  }
  $('#scanResult').innerHTML = items.map((it, i) => `
    <label class="scan-row">
      <input type="checkbox" data-idx="${i}" ${it.imported ? '' : 'checked'} ${it.imported ? 'disabled' : ''}>
      <span class="sr-main">
        <span class="sr-name">${esc(it.name)} ${it.imported ? '<span class="badge">已收录</span>' : ''}</span>
        <span class="sr-path">${esc(it.path)}</span>
      </span>
      ${it.github ? '<span class="badge info">GitHub</span>' : '<span class="badge">无远程</span>'}
    </label>`).join('');

  const fresh = items.filter((x) => !x.imported).length;
  $('#scanInfo').textContent = `找到 ${items.length} 个仓库，其中 ${fresh} 个可导入`;
  $('#scanImport').disabled = fresh === 0;
  updateImportLabel();
}

function updateImportLabel() {
  const n = $$('#scanResult input[type=checkbox]:checked').length;
  const btn = $('#scanImport');
  btn.textContent = n ? `导入 ${n} 个` : '导入选中';
  btn.disabled = n === 0;
}

async function importScan() {
  const pick = $$('#scanResult input[type=checkbox]:checked')
    .map((cb) => S.scanItems[Number(cb.dataset.idx)]);
  if (!pick.length) return;
  const btn = $('#scanImport');
  btn.disabled = true;
  btn.textContent = '导入中…';
  try {
    await api('/api/scan/import', { items: pick });
    $('#scanOverlay').hidden = true;
    await loadState();
    silentProbe();
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
  }
}

/* ───────── 目录选择器（内置） ───────── */

const PICK = { onPick: null, data: null };

function openDirPicker(startPath, onPick) {
  PICK.onPick = onPick;
  PICK.data = null;
  $('#pickerOverlay').hidden = false;
  return browseTo(startPath || '');
}

async function browseTo(path) {
  try {
    const d = await api('/api/browse', { path });
    PICK.data = d;
    renderPicker();
  } catch (e) {
    toast(e.message, true);
  }
}

function renderPicker() {
  const d = PICK.data;
  if (!d) return;

  $('#pickPath').value = d.path || '';
  $('#pickUp').disabled = d.parent === null;

  $('#pickShort').innerHTML = (d.shortcuts || [])
    .map((s) => `<button data-jump="${esc(s.path)}" title="${esc(s.path)}">${esc(s.label)}</button>`)
    .join('');

  $('#dirList').innerHTML = d.dirs.length
    ? d.dirs.map((x) => `
      <div class="dir-row" data-enter="${esc(x.path)}">
        ${icon(x.is_repo ? 'i-github' : 'i-folder', 'ico di' + (x.is_repo ? ' repo' : ''))}
        <span class="dn">${esc(x.name)}</span>
        ${x.is_repo ? '<span class="repo-tag">Git</span>' : ''}
      </div>`).join('')
    : `<div class="empty-sm">${d.path ? '这个文件夹下没有子文件夹' : '没检测到可用磁盘'}</div>`;

  if (d.path && d.self_repo) {
    const extra = [d.self_language, d.self_github].filter(Boolean).join(' · ');
    $('#pickInfo').textContent = '当前就是 Git 仓库' + (extra ? '：' + extra : '');
  } else {
    $('#pickInfo').textContent = d.path ? '将选中当前这个文件夹' : '先挑一个磁盘';
  }

  $('#pickConfirm').disabled = !d.path;
  $('#dirList').scrollTop = 0;
}

/* ───────── 事件绑定 ───────── */

function bind() {
  // 侧栏筛选
  $('#nav').addEventListener('click', (e) => {
    const item = e.target.closest('[data-filter]');
    if (!item) return;
    setFilter(item.dataset.filter);
  });

  // 顶部统计条 = 快捷筛选，再点一次取消
  $('#stats').addEventListener('click', (e) => {
    const cell = e.target.closest('[data-filter]');
    if (!cell) return;
    const id = cell.dataset.filter;
    setFilter(S.filter === id && id !== 'all' ? 'all' : id);
  });

  // 标签
  $('#tagCloud').addEventListener('click', (e) => {
    const chip = e.target.closest('[data-tag]');
    if (!chip) return;
    S.tag = S.tag === chip.dataset.tag ? '' : chip.dataset.tag;
    render();
  });

  // 搜索
  $('#q').addEventListener('input', (e) => { S.q = e.target.value; renderCards(); });
  $('#sort').addEventListener('change', (e) => { S.sort = e.target.value; renderCards(); });

  // 卡片内所有操作（事件委托）
  $('#content').addEventListener('click', (e) => {
    const scanTop = e.target.closest('[data-act="scan-top"]');
    if (scanTop) return openScan();

    if (e.target.closest('[data-act="clear-filter"]')) {
      S.q = '';
      $('#q').value = '';
      S.filter = 'all';
      S.tag = '';
      return render();
    }

    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const act = btn.dataset.act;
    const card = btn.closest('.card');
    const p = card && S.projects.find((x) => x.id === card.dataset.id);
    if (!p) return;

    if (act === 'folder' || act === 'vscode') return doOpen(p.id, act);
    if (act === 'github') return p.github ? doOpen(p.id, 'github') : openForm(p);
    if (act === 'edit') return openForm(p);
    if (act === 'del') return removeProject(p);
    if (act === 'fav') return toggleFav(p);
    if (act === 'copy') return copyPath(p.path);
  });

  // 顶栏
  $('#addBtn').addEventListener('click', () => openForm(null));
  $('#scanBtn').addEventListener('click', openScan);
  $('#refreshBtn').addEventListener('click', silentProbe);

  // 表单
  $('#projForm').addEventListener('submit', submitForm);

  // 表单：选目录
  $('#pickBtn').addEventListener('click', () => {
    openDirPicker($('#fPath').value.trim(), (path, info) => {
      $('#fPath').value = path;
      if (!$('#fName').value) {
        $('#fName').value = path.split(/[\\/]/).filter(Boolean).pop() || '';
      }
      if (!$('#fGithub').value && info.self_github) $('#fGithub').value = info.self_github;
      if (!$('#fLang').value && info.self_language) $('#fLang').value = info.self_language;
    });
  });

  // 扫描弹窗：选根目录
  $('#sPick').addEventListener('click', () => {
    openDirPicker($('#sRoot').value.trim(), (path) => { $('#sRoot').value = path; });
  });

  // 目录选择器内部
  $('#dirList').addEventListener('click', (e) => {
    const row = e.target.closest('[data-enter]');
    if (row) browseTo(row.dataset.enter);
  });
  $('#pickShort').addEventListener('click', (e) => {
    const b = e.target.closest('[data-jump]');
    if (b) browseTo(b.dataset.jump);
  });
  $('#pickUp').addEventListener('click', () => {
    if (PICK.data && PICK.data.parent !== null) browseTo(PICK.data.parent);
  });
  $('#pickPath').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      browseTo(e.target.value.trim());
    }
  });
  $('#pickConfirm').addEventListener('click', () => {
    if (!PICK.data || !PICK.data.path) return;
    const { path } = PICK.data;
    const info = PICK.data;
    $('#pickerOverlay').hidden = true;
    if (PICK.onPick) PICK.onPick(path, info);
  });
  $$('[data-close-picker]').forEach((b) =>
    b.addEventListener('click', () => { $('#pickerOverlay').hidden = true; }));
  $('#rootHints').addEventListener('click', (e) => {
    const b = e.target.closest('[data-root]');
    if (b) $('#sRoot').value = b.dataset.root;
  });
  $('#scanRun').addEventListener('click', runScan);
  $('#scanImport').addEventListener('click', importScan);
  $('#scanResult').addEventListener('change', updateImportLabel);
  $('#sRoot').addEventListener('keydown', (e) => { if (e.key === 'Enter') runScan(); });

  // 弹窗关闭
  $$('[data-close]').forEach((b) => b.addEventListener('click', () => { $('#formOverlay').hidden = true; }));
  $$('[data-close-scan]').forEach((b) => b.addEventListener('click', () => { $('#scanOverlay').hidden = true; }));
  $$('.overlay').forEach((ov) => ov.addEventListener('click', (e) => {
    if (e.target === ov) ov.hidden = true;
  }));

  // 快捷键
  document.addEventListener('keydown', (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (e.key === 'Escape') {
      $('#formOverlay').hidden = true;
      $('#scanOverlay').hidden = true;
      $('#pickerOverlay').hidden = true;
    }
    if (typing) return;
    if (e.key === '/') { e.preventDefault(); $('#q').focus(); }
    if (e.key === 'n' || e.key === 'N') { e.preventDefault(); openForm(null); }
    if (e.key === 'r' || e.key === 'R') { e.preventDefault(); silentProbe(); }
  });

  // 主题
  $('#themeBtn').addEventListener('click', () => {
    const cur = document.documentElement.dataset.theme;
    const next = cur === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    localStorage.setItem('ph-theme', next);
  });
}

function applyTheme(mode) {
  document.documentElement.dataset.theme = mode;
  $('#themeTxt').textContent = mode === 'dark' ? '切换到浅色' : '切换到深色';
  $('#themeBtn').querySelector('use').setAttribute('href', mode === 'dark' ? '#i-sun' : '#i-moon');
}

/* ───────── 启动 ───────── */

function initTheme() {
  const saved = localStorage.getItem('ph-theme');
  // 没存过就跟随系统（注意：浏览器若被固定成浅色，prefers-color-scheme 会一直报 light）
  const sys = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  // 支持 ?theme=dark 直接指定，方便对比和截图
  const forced = new URLSearchParams(location.search).get('theme');
  applyTheme(forced === 'dark' || forced === 'light' ? forced : (saved || sys));
}

async function boot() {
  initTheme();
  bind();
  // 支持 ?filter=dirty 直达某个筛选（跟 ?theme= 一样，方便收藏和截图）
  const want = new URLSearchParams(location.search).get('filter');
  if (want && NAVS.some((n) => n.id === want)) S.filter = want;
  try {
    await loadState();
    // 首屏先出卡片，状态在后台补 —— 这样即使有几十个仓库也不会白屏等
    if (S.projects.some((p) => !p.status)) silentProbe();
  } catch (e) {
    $('#content').innerHTML = `<div class="empty">
      <div class="empty-ico">${icon('i-close')}</div>
      <h3>连不上本地服务</h3>
      <p>${esc(e.message)}</p></div>`;
  }
}

boot();
