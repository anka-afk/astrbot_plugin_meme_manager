const INDEX_URL = 'https://raw.githubusercontent.com/anka-afk/astrbot-meme-pack-index/main/community-index.json';
const $ = (selector) => document.querySelector(selector);
const state = { packs: [], group: 'all', type: 'all', query: '', sort: 'default', busy: false };
const tagNames = { animated: '动态', gif: 'GIF', static: '静态', semantic: '语义描述', anime: '动漫', chinese: '中文', 'zh-cn': '中文', 'ai-generated': 'AI 创作', starter: '入门', fanart: '二创', chibi: 'Q版', moe: '萌系', cat: '猫猫' };
const official = (pack) => pack.id.startsWith('official-') || pack.tags.includes('official');
const safeURL = (value) => { try { const url = new URL(value); return url.protocol === 'https:' ? url.href : ''; } catch { return ''; } };
const element = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
const externalLink = (text, url, className = '') => { const a = element('a', className, text); a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a; };
function normalizeIndex(data) {
  if (!Array.isArray(data?.packs) || !data.packs.length) throw new Error('Invalid index');
  const ids = new Set();
  const packs = data.packs.filter(p => p && typeof p.id === 'string' && typeof p.name === 'string' && /^[\w.-]+\/[\w.-]+$/.test(p.source?.repo || '') && typeof p.source?.ref === 'string' && p.source.ref && !ids.has(p.id) && ids.add(p.id)).map(p => ({ ...p, description: String(p.description || ''), maintainer: String(p.maintainer || '社区维护者'), license: String(p.license || '请查看来源仓库中的许可说明'), tags: Array.isArray(p.tags) ? p.tags.filter(t => typeof t === 'string') : [], previews: Array.isArray(p.previews) ? p.previews.map(safeURL).filter(Boolean) : [], verified: p.verified === true, source: { ...p.source, subpath: String(p.source.subpath || '.') } }));
  if (!packs.length) throw new Error('Empty index');
  return { ...data, packs };
}
function makeImage(pack, index = 0, lazy = true) {
  const image = element('img'); image.alt = `${pack.name}预览`; image.loading = lazy ? 'lazy' : 'eager'; image.decoding = 'async'; image.referrerPolicy = 'no-referrer';
  const fallback = () => { const box = element('div', 'fallback-cover'); box.append(element('b', '', '◡̈'), element('span', '', '预览暂不可用')); image.replaceWith(box); };
  image.addEventListener('error', fallback, { once: true });
  if (pack.previews[index]) image.src = pack.previews[index]; else queueMicrotask(fallback);
  return image;
}
function displayTags(pack) { return [...new Set(pack.tags.filter(t => tagNames[t]).map(t => tagNames[t]))]; }
function tags(pack, limit = 3) { const wrap = element('div', 'card-tags'); const labels = displayTags(pack); for (const tag of (labels.length ? labels : ['表情包']).slice(0, limit)) wrap.append(element('span', 'tag', tag)); return wrap; }
function card(pack) {
  const node = element('article', 'pack-card');
  const cover = element('button', 'cover-button'); cover.type = 'button'; cover.setAttribute('aria-label', `查看${pack.name}详情`); cover.append(makeImage(pack), element('span', `cover-badge${official(pack) ? '' : ' community'}`, official(pack) ? '✦ 官方' : '社区'));
  cover.addEventListener('click', () => showDetails(pack));
  const body = element('div', 'card-body'); const title = element('button', 'card-title', pack.name); title.type = 'button'; title.title = pack.name; title.addEventListener('click', () => showDetails(pack));
  body.append(title, element('p', 'card-description', pack.description), tags(pack));
  const bottom = element('div', 'card-bottom'); const author = element('span', 'maintainer', pack.maintainer); author.title = pack.maintainer;
  const details = element('button', 'detail-button', '查看详情 ↗'); details.type = 'button'; details.addEventListener('click', () => showDetails(pack)); bottom.append(author, details); body.append(bottom); node.append(cover, body); return node;
}
function render() {
  let packs = state.packs.filter(p => (state.group === 'all' || (state.group === 'official') === official(p)) && (state.type === 'all' || p.tags.includes(state.type) || (state.type === 'animated' && p.tags.includes('gif'))) && `${p.name} ${p.id} ${p.description} ${p.maintainer} ${p.source.repo} ${p.tags.join(' ')} ${displayTags(p).join(' ')}`.toLocaleLowerCase().includes(state.query.toLocaleLowerCase().trim()));
  if (state.sort === 'newest') packs = [...packs].reverse();
  if (state.sort === 'name') packs = [...packs].sort((a,b) => a.name.localeCompare(b.name, 'zh-CN'));
  $('#pack-grid').replaceChildren(...packs.map(card)); $('#pack-grid').setAttribute('aria-busy', 'false');
  $('#empty-state').hidden = packs.length > 0 || !state.packs.length;
  $('#result-count').textContent = `找到 ${packs.length} 个表情包${state.query ? ` · “${state.query}”` : ''}`;
  document.querySelectorAll('[data-group]').forEach(button => { const selected = button.dataset.group === state.group; button.classList.toggle('active', selected); button.setAttribute('aria-pressed', String(selected)); });
}
function applyIndex(data, cached) {
  state.packs = data.packs;
  for (const selector of ['#total-count', '#section-count', '#all-count']) $(selector).textContent = state.packs.length;
  $('#creator-count').textContent = new Set(state.packs.map(p => p.maintainer)).size;
  $('#official-count').textContent = state.packs.filter(official).length;
  $('#community-count').textContent = state.packs.filter(p => !official(p)).length;
  const date = new Date(data.generated_at); const dateText = Number.isNaN(date.valueOf()) ? '' : ` · 更新于 ${date.toLocaleDateString('zh-CN')}`;
  $('#sync-status').textContent = `${cached ? '当前显示资源快照，可刷新获取最新列表' : '已同步资源索引'}${dateText}`;
  $('#error-state').hidden = true; render();
}
async function fetchIndex(url, timeout) { const response = await fetch(url, { signal: AbortSignal.timeout(timeout), cache: 'no-cache' }); if (!response.ok) throw new Error('Unavailable index'); return normalizeIndex(await response.json()); }
async function loadIndex() {
  if (state.busy) return; state.busy = true; $('#refresh').disabled = true;
  if (!state.packs.length) {
    $('#pack-grid').replaceChildren(...Array.from({length:8}, () => { const n=element('div','skeleton'); n.setAttribute('aria-hidden','true'); return n; }));
    try { applyIndex(await fetchIndex('./data/community-index.json', 4000), true); } catch { /* The live index can still load independently. */ }
  }
  try { applyIndex(await fetchIndex(INDEX_URL, 10000), false); }
  catch {
    if (state.packs.length) $('#sync-status').textContent = '刷新失败，当前列表仍可使用。请稍后重试。';
    else { $('#pack-grid').replaceChildren(); $('#pack-grid').setAttribute('aria-busy','false'); $('#error-state').hidden = false; $('#result-count').textContent = '暂时无法加载资源'; }
  } finally { state.busy = false; $('#refresh').disabled = false; }
}
let toastTimer;
function toast(text) { clearTimeout(toastTimer); $('#toast').textContent = text; $('#toast').hidden = false; toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 3500); }
async function copySource(pack, button) {
  const text = `仓库: ${pack.source.repo}\nref: ${pack.source.ref}\n子路径: ${pack.source.subpath}`;
  try { await navigator.clipboard.writeText(text); toast('安装来源已复制，前往插件资源广场粘贴使用'); }
  catch { const field = element('textarea', 'copy-fallback'); field.value = text; field.readOnly = true; field.setAttribute('aria-label','安装来源，选中后手动复制'); button.after(field); field.focus(); field.select(); toast('请复制选中的安装来源'); }
}
function showDetails(pack) {
  const dialog = $('#pack-dialog'); const layout = element('div', 'detail-layout');
  const visual = element('div', 'detail-visual'); const imageBox = element('div', 'detail-image'); imageBox.append(makeImage(pack, 0, false)); visual.append(imageBox);
  if (pack.previews.length > 1) {
    let index = 0; const controls = element('div', 'preview-switch'); const prev = element('button','','‹'); prev.setAttribute('aria-label','上一张预览'); const next = element('button','','›'); next.setAttribute('aria-label','下一张预览'); const count = element('span','',`1 / ${pack.previews.length}`);
    const change = delta => { index = (index + delta + pack.previews.length) % pack.previews.length; imageBox.replaceChildren(makeImage(pack,index,false)); count.textContent = `${index + 1} / ${pack.previews.length}`; };
    prev.onclick = () => change(-1); next.onclick = () => change(1); controls.append(prev,count,next); visual.append(controls);
  }
  const info = element('div','detail-info'); const status = element('span', `review-state${pack.verified ? '' : ' pending'}`, pack.verified ? '✓ 索引标记：已审核' : '○ 社区投稿 · 尚未审核');
  const title = element('h2','',pack.name); title.id = 'detail-title'; info.append(status,title,element('p','description',pack.description),tags(pack,10));
  const source = element('dl','detail-source');
  for (const [label,value] of [['维护者',pack.maintainer],['仓库',pack.source.repo],['版本引用',pack.source.ref],['子目录',pack.source.subpath]]) source.append(element('dt','',label),element('dd','',value));
  info.append(source,element('p','license',`许可说明：${pack.license}`));
  const actions = element('div','detail-actions'); const copy = element('button','button primary','复制安装来源'); copy.onclick = () => copySource(pack,copy);
  actions.append(copy,externalLink('来源仓库 ↗',`https://github.com/${pack.source.repo}`,'button secondary'),externalLink('下载 ZIP ↓',`https://github.com/${pack.source.repo}/archive/${encodeURIComponent(pack.source.ref)}.zip`,'button secondary'));
  info.append(actions,element('p','install-help','在 AstrBot 插件的资源广场中按名称安装，或使用「从 GitHub 安装」填写以上来源。ZIP 为来源仓库归档；若包位于子目录，请按子目录定位。'));
  layout.append(visual,info); $('#detail-content').replaceChildren(layout); dialog.showModal();
}
$('#close-dialog').onclick = () => $('#pack-dialog').close();
$('#pack-dialog').addEventListener('click', event => { if (event.target === event.currentTarget) { const bounds = event.currentTarget.getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) event.currentTarget.close(); } });
$('#search').addEventListener('input', event => { state.query = event.target.value; render(); });
$('#sort').addEventListener('change', event => { state.sort = event.target.value; render(); });
$('#type').addEventListener('change', event => { state.type = event.target.value; render(); });
document.querySelectorAll('[data-group]').forEach(button => button.addEventListener('click', () => { state.group = button.dataset.group; render(); }));
$('#reset-filters').onclick = () => { Object.assign(state,{query:'',group:'all',type:'all',sort:'default'}); $('#search').value=''; $('#type').value='all'; $('#sort').value='default'; render(); $('#search').focus(); };
$('#refresh').onclick = loadIndex; $('#retry').onclick = loadIndex;
document.addEventListener('keydown', event => { if (event.key === '/' && !$('#pack-dialog').open && !['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName) && !event.ctrlKey && !event.metaKey) { event.preventDefault(); $('#search').focus(); } });
loadIndex();
