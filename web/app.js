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
  const commitLine = st.last_commit_msg
    ? `<div class="commit">${icon('i-code', 'ico')}<span>${esc(relTime(st.last_commit_at))} · ${esc(st.last_commit_msg)}</span></div>`
    : (p.description ? `<div class="commit">${esc(p.description)}</div>` : '');

  // 「提交」按钮只在真有事可做时出现 —— 干净的仓库多点一个按钮只会碍事
  const needsCommit = st.dirty || (st.ahead || 0) > 0;
  const commitBtn = needsCommit
    ? `<button class="act commit-cta" data-act="commit" title="${
        st.dirty ? '提交改动，或推送到 GitHub' : '本地有提交还没推上去'
      }">${icon('i-commit')}${st.dirty ? '提交' : '推送'}</button>`
    : '';

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
      ${commitBtn}
      <button class="act" data-act="folder" title="在资源管理器里打开">${icon('i-folder')}打开</button>
      <button class="act ${p.github ? '' : 'dim'}" data-act="github"
        title="${p.github ? '打开 GitHub 仓库' : '还没有关联 GitHub 仓库，点这里补上'}">${icon('i-github')}</button>
      <button class="act ghostly" data-act="release"
        title="发布 Release：把打包产物传到 GitHub">${icon('i-tag')}</button>
      <button class="act" data-act="vscode" title="用 VS Code 打开">${icon('i-code')}</button>
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

/* ───────── 提交与推送面板 ───────── */

const CM = { id: null, detail: null, picked: new Set(), busy: null };

// 状态字母配色，跟 .cm-st.* 对应
const KIND_CLS = { M: 'm', A: 'a', D: 'd', R: 'r', C: 'c', T: 't', U: 'u', '?': 'q' };

async function openCommit(p) {
  CM.id = p.id;
  CM.detail = null;
  CM.picked = new Set();
  CM.busy = null;
  $('#cmTitle').textContent = p.name;
  $('#cmSub').textContent = p.path || '';
  $('#cmMsg').value = '';
  $('#cmRecent').innerHTML = '';
  $('#cmSync').innerHTML = '<span class="cm-branch">读取中…</span>';
  $('#cmFilesHead').innerHTML = '';
  $('#cmFiles').innerHTML = '<div class="cm-empty">正在读取改动…</div>';
  $('#cmInfo').textContent = '';
  $('#commitOverlay').hidden = false;

  try {
    const d = await api(`/api/projects/${p.id}/git`);
    CM.detail = d;
    // 默认全选：绝大多数时候就是「把改动都提交了」
    CM.picked = new Set((d.files || []).map((f) => f.path));
    renderCommit();
    setTimeout(() => $('#cmMsg').focus(), 80);
  } catch (e) {
    $('#cmFiles').innerHTML = `<div class="cm-empty">${esc(e.message)}</div>`;
  }
}

function renderCommit() {
  const d = CM.detail;
  if (!d) return;

  const hasChanges = (d.files || []).length > 0;
  // edit=有东西可提交 / sync=只剩推送拉取可做 / error=不是仓库
  const modal = document.querySelector('.commit-modal');
  if (modal) modal.dataset.mode = !d.is_git ? 'error' : (hasChanges ? 'edit' : 'sync');
  const closeBtn = document.querySelector('.modal-foot [data-close-commit]');
  if (closeBtn) closeBtn.textContent = hasChanges ? '取消' : '关闭';

  if (!d.is_git) {
    $('#cmSync').innerHTML = '';
    $('#cmFilesHead').innerHTML = '';
    $('#cmFiles').innerHTML = `<div class="cm-empty">${icon('i-alert')}
      <div>${esc(d.error || '这个目录还不是 Git 仓库')}</div></div>`;
    $('#cmInfo').textContent = '';
    setCommitButtons(false);
    return;
  }

  renderSync(d);
  renderFiles(d);
  renderRecent(d);

  const n = CM.picked.size;
  $('#cmHint').textContent = n ? `将提交 ${n} 个文件` : '';
  // 没配用户名的话提交必定失败，提前说清楚，别让人写完一大段话才看到报错
  const noId = !d.identity || !d.identity.name;
  $('#cmInfo').textContent = noId ? '没配置 Git 用户名，提交会失败' : '';
  setCommitButtons(n > 0 && !CM.busy);
}

function renderSync(d) {
  const busy = CM.busy;
  const out = [`<span class="cm-branch" title="当前分支 / 上游：${esc(d.upstream || '未设置')}">
    ${icon('i-commit')}${esc(d.branch || '未知')}</span>`];

  if (d.detached) out.push('<span class="badge warn">游离 HEAD</span>');
  if (d.ahead > 0) out.push(`<span class="badge info">↑ 待推送 ${d.ahead}</span>`);
  if (d.behind > 0) out.push(`<span class="badge info">↓ 待拉取 ${d.behind}</span>`);
  if (!d.upstream && !d.detached) out.push('<span class="badge">还没有上游</span>');

  // 没上游时也允许推 —— 那正是「首次推送，顺便建立跟踪」
  const canPull = d.has_upstream && !d.detached && !busy;
  const canPush = !d.detached && !busy && (d.ahead > 0 || !d.has_upstream);

  out.push('<span class="grow"></span>');
  out.push(`<button class="btn sm" id="cmPull" ${canPull ? '' : 'disabled'}>${icon('i-down')}${
    busy === 'pull' ? '拉取中…' : '拉取'}</button>`);
  out.push(`<button class="btn sm" id="cmPush" ${canPush ? '' : 'disabled'}>${icon('i-up')}${
    busy === 'push' ? '推送中…' : (d.ahead > 0 ? `推送 ${d.ahead}` : '推送')}</button>`);

  $('#cmSync').innerHTML = out.join('');
}

function renderFiles(d) {
  const box = $('#cmFiles');
  const sc = box.scrollTop;   // 勾选后会重画，把滚动位置接回来
  const files = d.files || [];

  if (!files.length) {
    $('#cmFilesHead').innerHTML = '';
    box.innerHTML = `<div class="cm-empty">${icon('i-check')}
      <div>工作区是干净的<br>没有待提交的改动</div></div>`;
    return;
  }

  const allOn = CM.picked.size === files.length;
  $('#cmFilesHead').innerHTML = `
    <label class="all"><input type="checkbox" id="cmAll" ${allOn ? 'checked' : ''}>全选</label>
    <span class="sum">${CM.picked.size}/${files.length} 个文件 ·
      <b class="add">+${d.stat.add}</b> <b class="del">−${d.stat.del}</b></span>`;

  box.innerHTML = files.map((f) => {
    const on = CM.picked.has(f.path);
    const cls = KIND_CLS[f.kind] || 'm';
    const num = [
      f.add ? `<i class="add">+${f.add}</i>` : (f.untracked ? '<i class="add">新增</i>' : ''),
      f.del ? `<i class="del">−${f.del}</i>` : '',
    ].filter(Boolean).join('');
    return `<label class="cm-file ${on ? '' : 'off'}" title="${esc(f.label)}：${esc(f.path)}">
      <input type="checkbox" data-file="${esc(f.path)}" ${on ? 'checked' : ''}>
      <span class="cm-st ${cls}" title="${esc(f.label)}${f.staged ? '（已暂存）' : ''}">${esc(f.kind)}</span>
      <span class="cm-fp">${esc(f.path)}</span>
      <span class="cm-num">${num}</span>
    </label>`;
  }).join('');

  box.scrollTop = sc;
}

function renderRecent(d) {
  // 最近几条提交只用来「照抄格式」，点一下填进输入框
  const rs = (d.recent || []).slice(0, 4);
  $('#cmRecent').innerHTML = rs.map((r) =>
    `<button data-msg="${esc(r.msg)}" title="点一下填入：${esc(r.msg)}">${esc(r.msg)}</button>`).join('');
}

function setCommitButtons(on) {
  const busy = CM.busy;
  $('#cmCommit').disabled = !on || !!busy;
  $('#cmCommitPush').disabled = !on || !!busy;
  $('#cmCommit').textContent = busy === 'commit' ? '提交中…' : '仅提交';
  $('#cmCommitPush').textContent = busy === 'commitpush' ? '提交并推送中…' : '提交并推送';
}

async function runCommit(push) {
  const d = CM.detail;
  if (!d || CM.busy) return;

  const msg = $('#cmMsg').value.trim();
  if (!msg) {
    $('#cmMsg').focus();
    return toast('先写一句提交信息', true);
  }
  if (!CM.picked.size) return toast('至少勾选一个文件', true);

  CM.busy = push ? 'commitpush' : 'commit';
  renderCommit();   // 先把按钮置灰，免得手快连点两次提示「没有可提交的改动」

  try {
    const res = await api(`/api/projects/${CM.id}/git`, {
      action: 'commit',
      message: msg,
      files: [...CM.picked],
      push,
    });
    if (res.error) {
      // 提交成功了、只是推送失败：本地提交是留着的，得说清楚，别让人以为白干
      toast(res.error, true);
      CM.detail = res.detail;
      CM.picked = new Set((res.detail.files || []).map((f) => f.path));
    } else {
      $('#commitOverlay').hidden = true;   // 成功提示由 api() 统一弹
    }
    await loadState();
    silentProbe();
  } catch (e) {
    toast(e.message, true);
  } finally {
    CM.busy = null;
    if (!$('#commitOverlay').hidden) renderCommit();
  }
}

async function runSync(action) {
  if (CM.busy || !CM.detail) return;
  const before = CM.detail.files || [];
  const wasAll = CM.picked.size === before.length;   // 用户是全选状态吗

  CM.busy = action;
  renderCommit();

  try {
    const res = await api(`/api/projects/${CM.id}/git`, { action });
    const next = res.detail.files || [];
    CM.detail = res.detail;
    // 拉取可能带来新文件：全选的继续保持全选，部分选择的按原样过滤
    CM.picked = wasAll
      ? new Set(next.map((f) => f.path))
      : new Set([...CM.picked].filter((p) => next.some((f) => f.path === p)));
    await loadState();
  } catch (e) {
    toast(e.message, true);
  } finally {
    CM.busy = null;
    renderCommit();
  }
}

/* ───────── 发布 Release ───────── */

const REL = {
  id: null,
  rec: null,
  state: null,
  files: [],                        // 用户额外挑的现成文件
  busy: false,
  picker: { path: '', data: null },
};

function relSize(n) {
  if (n === undefined || n === null || Number.isNaN(n)) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

async function openRelease(p) {
  REL.id = p.id;
  REL.rec = p;
  REL.state = null;
  REL.files = [];
  REL.busy = false;
  REL.picker = { path: p.path || '', data: null };

  $('#relSub').textContent = p.path || '';
  $('#relTag').value = '';
  $('#relTitle').value = '';
  $('#relNotes').value = '';
  $('#relDraft').checked = true;
  $('#relPre').checked = false;
  $('#relAutoZip').checked = true;
  $('#relPicker').hidden = true;
  $('#relFiles').innerHTML = '';
  $('#relHistory').innerHTML = '';
  $('#relZipHint').textContent = '';
  $('#relInfo').textContent = '';
  $('#relNotice').innerHTML = '<div class="rel-notice">正在读取仓库信息…</div>';
  $('#relPublish').disabled = true;
  $('#relPublish').textContent = '发布';
  $('#relOverlay').hidden = false;

  try {
    const s = await api(`/api/projects/${p.id}/release`, { action: 'state' });
    REL.state = s;
    if (!$('#relTag').value) $('#relTag').value = s.suggest_tag || 'v1.0.0';
    renderRelease();
  } catch (e) {
    $('#relNotice').innerHTML = `<div class="rel-notice err">${icon('i-alert')}
      <div>${esc(e.message)}</div></div>`;
  }
}

function releaseNotices(s) {
  const out = [];
  if (!s.is_git) {
    out.push(['err', '这个目录不是 Git 仓库，发不了 Release']);
  } else if (!s.has_github) {
    out.push(['err', '还没有关联 GitHub 仓库。先在卡片上点铅笔填好地址，再回来发布']);
  } else if (!s.gh_ready) {
    out.push(['err', s.gh_error || '没检测到 gh 命令行工具，装一个才能发布：https://cli.github.com']);
  } else if (s.gh_error) {
    out.push(['err', s.gh_error]);
  } else if (s.ahead > 0) {
    // 这是最容易踩的坑：release 的 tag 打在**远端**提交上
    out.push(['err', `本地有 ${s.ahead} 个提交还没推送。发布前先推送，否则 tag 会打在远程的旧代码上`]);
  } else if (s.dirty) {
    out.push(['warn', '工作区有未提交的改动：源码包会包含这些改动，但仓库里的 tag 指向最后一次提交']);
  }
  if (s.behind > 0 && s.is_git) {
    out.push(['warn', `远程有 ${s.behind} 个新提交，建议先拉取再发布`]);
  }
  return out.map(([cls, txt]) => `<div class="rel-notice ${cls}">${icon('i-alert')}
    <div>${esc(txt)}</div></div>`).join('');
}

function renderRelease() {
  const s = REL.state;
  if (!s) return;

  $('#relNotice').innerHTML = releaseNotices(s);
  renderRelFiles();
  renderRelHistory(s);

  const blocked = !s.is_git || !s.has_github || !s.gh_ready || !!s.gh_error || s.ahead > 0;
  $('#relPublish').disabled = blocked || REL.busy;
  $('#relZipHint').textContent = $('#relAutoZip').checked
    ? '排除 .git 和 .gitignore 里的文件'
    : '';
}

/* GitHub 会把资产名里的非 ASCII 字符直接删掉（"预览.png" 会变成 default.png），
   所以上传前要换成 ASCII 名。这里跟后端的 safe_asset_name 保持同一套规则，
   好让用户在点发布之前就能看到实际会用哪个名字。 */
function safeAssetName(name, i = 1) {
  const s = String(name || '');
  const dot = s.lastIndexOf('.');
  let stem = dot > 0 ? s.slice(0, dot) : s;
  let ext = dot > 0 ? s.slice(dot) : '';
  ext = ext.replace(/[^A-Za-z0-9._-]+/g, '');
  stem = stem.replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/^[-._]+/, '').replace(/[-._]+$/, '')
    .replace(/-{2,}/g, '-');
  if (!stem) stem = `asset-${i}`;
  return stem + ext;
}

function renderRelFiles() {
  const box = $('#relFiles');
  if (!REL.files.length) {
    box.innerHTML = '';
    return;
  }
  let anyRenamed = false;
  const rows = REL.files.map((f, i) => {
    const safe = safeAssetName(f.name, i + 1);
    const changed = safe !== f.name;
    if (changed) anyRenamed = true;
    return `<div class="rel-file">
      ${icon('i-file')}
      <span class="nm" title="${esc(f.path)}">${esc(f.name)}${
        changed ? `<em>→ ${esc(safe)}</em>` : ''}</span>
      <span class="sz">${relSize(f.size)}</span>
      <button class="rm" data-rm="${i}" title="移除">${icon('i-close')}</button>
    </div>`;
  });
  box.innerHTML = rows.join('') + (anyRenamed
    ? `<div class="rel-note">GitHub 不接受中文等非 ASCII 资产名，上面标出的才是实际传上去的名字</div>`
    : '');
}

function renderRelHistory(s) {
  const box = $('#relHistory');
  if (!s.releases || !s.releases.length) {
    box.innerHTML = '';
    return;
  }
  box.innerHTML = '<div class="hd">已有的 Release</div>' + s.releases.map((r) => {
    const cls = r.draft ? 'draft' : (r.pre ? 'pre' : 'pub');
    const label = r.draft ? '草稿' : (r.pre ? '预发布' : '已发布');
    // 草稿才给操作按钮：草稿是个没公开的壳子，需要单独「发布」或干脆删掉
    const acts = r.draft
      ? `<button class="rel-mini" data-rel="publish" data-tag="${esc(r.tag)}">发布</button>
         <button class="rel-mini danger" data-rel="delete" data-tag="${esc(r.tag)}">删除</button>`
      : '';
    return `<div class="rel-item">
      <span class="tg">${esc(r.tag)}</span>
      <span class="nm">${esc(r.name || '')}</span>
      <span class="stab ${cls}">${label}</span>
      <a href="${esc(r.url)}" target="_blank" rel="noreferrer">查看</a>
      ${acts}
    </div>`;
  }).join('');
}

/* 草稿转正式 / 删草稿 —— 不用再开 GitHub 网页 */
async function relEdit(tag, action) {
  if (REL.busy) return;
  const isPub = action === 'publish_draft';
  if (!isPub && !confirm(`删除草稿 ${tag}？删除后无法恢复。`)) return;

  REL.busy = true;
  try {
    const d = await api(`/api/projects/${REL.id}/release`, { action, tag });
    REL.state = d.state;
    $('#relNotice').innerHTML = `<div class="rel-notice info">${icon('i-check')}
      <div>${isPub
        ? `${esc(tag)} 已正式发布，tag 现在出现在仓库里了`
        : `草稿 ${esc(tag)} 已删除`}</div></div>`;
    renderRelHistory(REL.state);
    await loadState();
    silentProbe();
  } catch (e) {
    toast(e.message, true);
  } finally {
    REL.busy = false;
  }
}

/* 内置文件浏览器：直接列目录并勾选文件，不走系统对话框 */

async function relBrowse(path) {
  try {
    const d = await api('/api/browse', { path: path || '', files: true });
    REL.picker.data = d;
    REL.picker.path = d.path;
    renderRelPicker();
  } catch (e) {
    toast(e.message, true);
  }
}

function renderRelPicker() {
  const d = REL.picker.data;
  if (!d) return;
  $('#relPath').value = d.path || '';
  const picked = new Set(REL.files.map((f) => f.path.toLowerCase()));
  const rows = [];

  if (d.path && d.parent !== null) {
    rows.push(`<div class="rel-frow dir" data-up="${esc(d.parent)}">
      ${icon('i-up')}<span class="nm">返回上一级</span></div>`);
  }
  (d.dirs || []).forEach((x) => {
    rows.push(`<div class="rel-frow dir" data-dir="${esc(x.path)}">
      ${icon('i-folder')}<span class="nm">${esc(x.name)}</span></div>`);
  });
  (d.files || []).forEach((x) => {
    const on = picked.has(x.path.toLowerCase()) ? ' on' : '';
    rows.push(`<div class="rel-frow${on}" data-file="${esc(x.path)}"
      data-name="${esc(x.name)}" data-size="${x.size}">
      ${icon('i-file')}<span class="nm">${esc(x.name)}</span>
      <span class="sz">${relSize(x.size)}</span>
      <svg class="ico tick"><use href="#i-check"></use></svg></div>`);
  });

  $('#relFl').innerHTML = rows.join('')
    || `<div class="rel-fempty">这个目录是空的</div>`;
}

function toggleRelFile(path, name, size) {
  const i = REL.files.findIndex((f) => f.path.toLowerCase() === path.toLowerCase());
  if (i >= 0) REL.files.splice(i, 1);
  else REL.files.push({ path, name, size });
  renderRelFiles();
  renderRelPicker();
}

async function doPublish() {
  if (REL.busy || !REL.state) return;

  const tag = $('#relTag').value.trim();
  if (!tag) {
    $('#relTag').focus();
    return toast('先填个版本号', true);
  }
  const autoZip = $('#relAutoZip').checked;
  if (!autoZip && !REL.files.length) {
    return toast('没有可上传的资产：勾上自动打包，或添加几个文件', true);
  }

  REL.busy = true;
  const btn = $('#relPublish');
  btn.disabled = true;
  btn.textContent = '发布中…';
  $('#relInfo').textContent = autoZip ? '正在打包并上传，大文件要等一会儿…' : '正在上传…';

  try {
    const d = await api(`/api/projects/${REL.id}/release`, {
      action: 'publish',
      tag,
      title: $('#relTitle').value.trim(),
      notes: $('#relNotes').value,
      draft: $('#relDraft').checked,
      prerelease: $('#relPre').checked,
      auto_zip: autoZip,
      files: REL.files.map((f) => f.path),
    });

    REL.state = d.state;
    // 草稿发布后 GitHub 还没建 tag，得说清楚，不然去仓库里找不到会以为失败了
    const tail = d.state && d.state.releases.find((r) => r.tag === tag);
    const tip = (tail && tail.draft)
      ? '草稿已创建（别人暂时看不到）。在下面列表里点「发布」才会公开，tag 也才真正建到仓库里。'
      : '已发布，可以去 GitHub 看了。';
    $('#relNotice').innerHTML = `<div class="rel-notice info">${icon('i-check')}
      <div>${esc(tip)}${d.url ? ` <a href="${esc(d.url)}" target="_blank"
        rel="noreferrer" style="color:inherit;text-decoration:underline">打开页面</a>` : ''}</div></div>`;
    renderRelHistory(REL.state);
    REL.files = [];
    renderRelFiles();
    $('#relPicker').hidden = true;
    await loadState();
    silentProbe();
  } catch (e) {
    $('#relInfo').textContent = '';
    toast(e.message, true);
    // 失败后重拉一次状态：可能 gh 那边已经变了（比如 tag 已存在）
    try {
      REL.state = await api(`/api/projects/${REL.id}/release`, { action: 'state' });
      renderRelease();
    } catch { /* 拉不到就保持原样 */ }
  } finally {
    REL.busy = false;
    btn.textContent = '发布';
    if (REL.state) {
      const s = REL.state;
      const blocked = !s.is_git || !s.has_github || !s.gh_ready || !!s.gh_error || s.ahead > 0;
      btn.disabled = blocked;
    } else {
      btn.disabled = false;
    }
  }
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
    if (act === 'commit') return openCommit(p);
    if (act === 'release') return openRelease(p);
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
  $$('[data-close-commit]').forEach((b) => b.addEventListener('click', () => { $('#commitOverlay').hidden = true; }));
  $$('.overlay').forEach((ov) => ov.addEventListener('click', (e) => {
    if (e.target === ov) ov.hidden = true;
  }));

  // 提交面板：勾选文件
  $('#cmFiles').addEventListener('change', (e) => {
    const cb = e.target.closest('input[data-file]');
    if (!cb) return;
    if (cb.checked) CM.picked.add(cb.dataset.file);
    else CM.picked.delete(cb.dataset.file);
    renderCommit();
  });
  $('#cmFilesHead').addEventListener('change', (e) => {
    if (e.target.id !== 'cmAll') return;
    const files = CM.detail?.files || [];
    CM.picked = e.target.checked ? new Set(files.map((f) => f.path)) : new Set();
    renderCommit();
  });
  // 拉取 / 推送按钮是动态渲染的，用委托
  $('#cmSync').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b || b.disabled) return;
    if (b.id === 'cmPull') return runSync('pull');
    if (b.id === 'cmPush') return runSync('push');
  });
  // 点最近提交 = 把那条信息填进输入框
  $('#cmRecent').addEventListener('click', (e) => {
    const b = e.target.closest('[data-msg]');
    if (!b) return;
    $('#cmMsg').value = b.dataset.msg;
    $('#cmMsg').focus();
  });
  $('#cmCommit').addEventListener('click', () => runCommit(false));
  $('#cmCommitPush').addEventListener('click', () => runCommit(true));
  $('#cmMsg').addEventListener('keydown', (e) => {
    // Ctrl/Cmd + Enter 一路提交并推送，写代码的人习惯这个手感
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      runCommit(true);
    }
  });

  // ── 发布 Release ──
  $$('[data-close-release]').forEach((b) =>
    b.addEventListener('click', () => { $('#relOverlay').hidden = true; }));

  $('#relAddFile').addEventListener('click', () => {
    const box = $('#relPicker');
    if (!box.hidden) {          // 再点一次收起来
      box.hidden = true;
      return;
    }
    box.hidden = false;
    relBrowse(REL.picker.path || REL.rec?.path || '');
  });
  $('#relPickDone').addEventListener('click', () => { $('#relPicker').hidden = true; });
  $('#relUp').addEventListener('click', () => {
    const d = REL.picker.data;
    if (!d) return;
    relBrowse(d.parent === null ? '' : d.parent);
  });
  $('#relPath').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    relBrowse(e.target.value.trim());
  });
  $('#relFl').addEventListener('click', (e) => {
    const up = e.target.closest('[data-up]');
    if (up) return relBrowse(up.dataset.up);
    const dir = e.target.closest('[data-dir]');
    if (dir) return relBrowse(dir.dataset.dir);
    const f = e.target.closest('[data-file]');
    if (f) return toggleRelFile(f.dataset.file, f.dataset.name, Number(f.dataset.size) || 0);
  });
  $('#relFiles').addEventListener('click', (e) => {
    const rm = e.target.closest('[data-rm]');
    if (!rm) return;
    REL.files.splice(Number(rm.dataset.rm), 1);
    renderRelFiles();
    if (!$('#relPicker').hidden) renderRelPicker();   // 勾选状态跟着变
  });
  // 已有 release 里的草稿操作（发布 / 删除），列表是动态渲染的所以用委托
  $('#relHistory').addEventListener('click', (e) => {
    const b = e.target.closest('[data-rel]');
    if (!b || b.disabled) return;
    const tag = b.dataset.tag;
    if (b.dataset.rel === 'publish') return relEdit(tag, 'publish_draft');
    if (b.dataset.rel === 'delete') return relEdit(tag, 'delete_draft');
  });
  $('#relAutoZip').addEventListener('change', () => {
    $('#relZipHint').textContent = $('#relAutoZip').checked
      ? '排除 .git 和 .gitignore 里的文件' : '';
  });
  $('#relPublish').addEventListener('click', doPublish);

  // 快捷键
  document.addEventListener('keydown', (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (e.key === 'Escape') {
      $('#formOverlay').hidden = true;
      $('#scanOverlay').hidden = true;
      $('#pickerOverlay').hidden = true;
      $('#commitOverlay').hidden = true;
      $('#relOverlay').hidden = true;
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
