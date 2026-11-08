
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
}

function cardTags(card) {
  const v = card.dataset.tags || '';
  return v ? v.split(',') : [];
}

function resortCards() {
  const list = document.querySelector('.cards');
  if (!list) return;
  const cards = Array.from(list.querySelectorAll('.card'));
  cards.sort((a, b) => a.querySelector('.pagelink').textContent
    .localeCompare(b.querySelector('.pagelink').textContent));
  cards.forEach(c => list.appendChild(c));
}

function applyFilter() {
  const cards = document.querySelectorAll('.card');
  let hidden = 0;
  cards.forEach(card => {
    const tags = cardTags(card);
    const visible = SHOW_ALL || SELECTED.length === 0 || tags.some(t => SELECTED.includes(t));
    card.classList.toggle('hidden', !visible);
    if (!visible) hidden++;
  });
  const status = document.getElementById('status');
  status.textContent = SHOW_ALL
    ? `showing all ${TOTAL_PAGES}`
    : `${hidden} of ${TOTAL_PAGES} hidden`;
}

function toggleShowAll() {
  SHOW_ALL = !SHOW_ALL;
  document.getElementById('showAllToggle').classList.toggle('active', SHOW_ALL);
  applyFilter();
}

function renderFilterBar() {
  const bar = document.getElementById('filterbar');
  bar.innerHTML = SELECTED.map(t => {
    const n = TAG_COUNTS[t] || 0;
    const et = escapeHtml(t);
    return `<span class="chip" data-tag="${et}">${et}:${n}`
      + `<button type="button" onclick="deselectFilter('${et.replace(/'/g,"\\'")}')">&times;</button></span>`;
  }).join('\n');
  const sel = document.getElementById('addFilter');
  const unselected = Object.keys(TAG_COUNTS).sort().filter(t => !SELECTED.includes(t));
  sel.innerHTML = '<option value="">+ filter by tag&hellip;</option>' +
    unselected.map(t => `<option value="${escapeHtml(t)}">${escapeHtml(t)}:${TAG_COUNTS[t]}</option>`).join('');
}

async function selectFilter(tag) {
  const r = await fetch('/__landing/filter', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({tag})});
  if (r.ok) {
    const data = await r.json();
    SELECTED = data.selected_tags;
    renderFilterBar();
    applyFilter();
  }
}

async function deselectFilter(tag) {
  const r = await fetch('/__landing/filter', {method:'DELETE',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({tag})});
  if (r.ok) {
    const data = await r.json();
    SELECTED = data.selected_tags;
    renderFilterBar();
    applyFilter();
  }
}

function closeAllMenus(except) {
  document.querySelectorAll('.tagmenu.open').forEach(m => {
    if (m !== except) m.classList.remove('open');
  });
}

function toggleTagMenu(btn, page) {
  const menu = btn.nextElementSibling;
  const isOpen = menu.classList.contains('open');
  closeAllMenus();
  if (isOpen) { menu.classList.remove('open'); return; }
  const card = btn.closest('.card');
  const have = new Set(cardTags(card));
  const opts = Object.keys(TAG_COUNTS).sort().filter(t => !have.has(t));
  const ep = escapeHtml(page).replace(/'/g,"\\'");
  const currentLabel = DISPLAY_NAMES[page] || page;
  menu.innerHTML = opts.map(t =>
    `<button type="button" class="opt" onclick="addTag('${ep}','${escapeHtml(t).replace(/'/g,"\\'")}')">${escapeHtml(t)}</button>`
  ).join('') +
    `<input type="text" placeholder="new tag" onkeydown="if(event.key==='Enter'){addTag('${ep}',this.value);this.value='';}" />`
    + `<button type="button" class="addbtn" onclick="const i=this.previousElementSibling;addTag('${ep}',i.value);i.value='';">add</button>`
    + `<hr />`
    + `<input type="text" class="renameinput" value="${escapeHtml(currentLabel)}" placeholder="card name"`
    + ` onkeydown="if(event.key==='Enter'){renameCard('${ep}',this.value);}" />`
    + `<button type="button" class="addbtn" onclick="const i=this.previousElementSibling;renameCard('${ep}',i.value);">rename</button>`
    + `<button type="button" class="opt" onclick="renameCard('${ep}','')">reset to default</button>`;
  menu.classList.add('open');
}

document.addEventListener('click', ev => {
  if (!ev.target.closest('.tagmenu') && !ev.target.closest('.dots')) closeAllMenus();
});

function renderCardTags(page, tags) {
  const ep = escapeHtml(page).replace(/'/g,"\\'");
  return tags.map(t => {
    const et = escapeHtml(t);
    return `<span class="tagchip">${et}`
      + `<button type="button" onclick="removeTag('${ep}','${et.replace(/'/g,"\\'")}')">&times;</button></span>`;
  }).join('');
}

function applyTagUpdate(page, data) {
  Object.assign(TAG_COUNTS, data.tag_counts);
  Object.keys(TAG_COUNTS).forEach(k => { if (!(k in data.tag_counts)) delete TAG_COUNTS[k]; });
  const card = document.querySelector(`.card[data-page="${CSS.escape(page)}"]`);
  if (card) {
    card.dataset.tags = data.tags.join(',');
    card.querySelector('.cardtags').innerHTML = renderCardTags(page, data.tags);
    closeAllMenus();
  }
  renderFilterBar();
  applyFilter();
}

async function addTag(page, tag) {
  tag = (tag || '').trim();
  if (!tag) return;
  const r = await fetch('/__landing/tags', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({page, tag})});
  if (!r.ok) return;
  applyTagUpdate(page, await r.json());
}

async function removeTag(page, tag) {
  const r = await fetch('/__landing/tags', {method:'DELETE',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({page, tag})});
  if (!r.ok) return;
  applyTagUpdate(page, await r.json());
}

async function renameCard(page, name) {
  const r = await fetch('/__landing/name', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({page, name})});
  if (!r.ok) return;
  const data = await r.json();
  if (data.display_name) { DISPLAY_NAMES[page] = data.display_name; }
  else { delete DISPLAY_NAMES[page]; }
  const card = document.querySelector(`.card[data-page="${CSS.escape(page)}"]`);
  if (card) {
    card.querySelector('.pagelink').textContent = data.display_name || page;
    closeAllMenus();
    resortCards();
  }
}

applyFilter();
