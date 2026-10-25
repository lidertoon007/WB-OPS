/* WB OPS: «Перенос карточек» — перенос товаров между кабинетами Wildberries, Ozon и Яндекс Маркета по артикулу. */
window.WBOPSTransfer = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const num = (v, d = 0) => v == null || v === '' || isNaN(v) ? '—' : Number(v).toLocaleString('ru-RU', {minimumFractionDigits: d, maximumFractionDigits: d});
  const MP = {wildberries: {t: 'Wildberries', s: 'WB', cls: 'wb'}, ozon: {t: 'Ozon', s: 'Ozon', cls: 'ozon'}, yandex: {t: 'Яндекс Маркет', s: 'ЯМ', cls: 'ym'}};
  const STATUS = {ready: ['Готов', 'ok'], missing: ['Не хватает данных', 'warn'], skipped: ['Пропущен', 'mute'], error: ['Ошибка', 'bad'],
    uploaded: ['Создан', 'ok'], failed: ['Не создан', 'bad'], pending: ['В обработке', 'warn']};
  const NON_ATTR = ['цена', 'фото', 'габариты/вес', 'нет размеров для переноса'];
  const st = {cabinets: [], settings: null, run: null, history: [], poll: null, src: '', dst: '', articles: '', showSettings: false, setTab: 'ozon', picker: null, selected: new Set()};
  let root = null;
  try { Object.assign(st, JSON.parse(localStorage.getItem('wbops.transfer') || '{}'), {run: null, poll: null, picker: null, selected: new Set()}); } catch (e) { /* без хранилища */ }
  const remember = () => { try { localStorage.setItem('wbops.transfer', JSON.stringify({src: st.src, dst: st.dst, articles: st.articles, setTab: st.setTab})); } catch (e) { /* ignore */ } };
  const api = async (url, opt = {}) => {
    const r = await fetch(url, {method: opt.method || 'GET', headers: opt.body && !(opt.body instanceof FormData) ? {'Content-Type': 'application/json'} : {},
      body: opt.body instanceof FormData ? opt.body : opt.body ? JSON.stringify(opt.body) : undefined});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || 'Ошибка запроса');
    return d;
  };

  // ---------------------------------------------------------------- каркас
  async function render(section) {
    root = section;
    root.className = 'panel rv-page tr-page';
    root.innerHTML = `<div class="db-head"><h2>Перенос карточек</h2><span class="muted db-fresh">Впишите артикул — карточка со всеми данными переедет в другой кабинет</span></div>
      <section class="tr-box" data-form></section>
      <section class="tr-box tr-settings" data-settings hidden></section>
      <section data-run></section>
      <section class="tr-history" data-history></section>`;
    try {
      [st.cabinets, st.settings, st.history] = await Promise.all([api('/api/transfer/cabinets'), api('/api/transfer/settings'), api('/api/transfer/runs')]);
    } catch (e) { root.querySelector('[data-form]').innerHTML = `<p class="rv-error">${esc(e.message)}</p>`; return; }
    drawForm(); drawSettings(); drawHistory();
    const live = st.history.find(h => h.status === 'running');
    if (live) openRun(live.id);
  }

  function cabOption(c, cur, other) {
    const label = `${MP[c.marketplace]?.t || c.marketplace} · ${c.name}${c.ready ? '' : ` — ${c.reason}`}`;
    return `<option value="${esc(c.id)}" ${c.id === cur ? 'selected' : ''} ${c.ready && c.id !== other ? '' : 'disabled'}>${esc(label)}</option>`;
  }
  function drawForm() {
    const box = root.querySelector('[data-form]');
    const ready = st.cabinets.filter(c => c.ready);
    if (!ready.some(c => c.id === st.src)) st.src = (ready.find(c => c.marketplace === 'wildberries') || ready[0] || {}).id || '';
    if (!ready.some(c => c.id === st.dst) || st.dst === st.src) st.dst = (ready.find(c => c.id !== st.src && c.marketplace === 'ozon') || ready.find(c => c.id !== st.src) || {}).id || '';
    const src = st.cabinets.find(c => c.id === st.src), dst = st.cabinets.find(c => c.id === st.dst);
    box.innerHTML = `<div class="tr-route">
        <label>Откуда<select data-k="src"><option value="">— кабинет-источник —</option>${st.cabinets.map(c => cabOption(c, st.src, '')).join('')}</select></label>
        <button type="button" class="tr-swap" data-swap title="Поменять местами" aria-label="Поменять местами">⇄</button>
        <label>Куда<select data-k="dst"><option value="">— кабинет-приёмник —</option>${st.cabinets.map(c => cabOption(c, st.dst, st.src)).join('')}</select></label>
      </div>
      ${src && dst ? `<p class="tr-dir"><span class="st-badge ${MP[src.marketplace].cls}">${MP[src.marketplace].t}</span> ${esc(src.name)} <b>→</b>
        <span class="st-badge ${MP[dst.marketplace].cls}">${MP[dst.marketplace].t}</span> ${esc(dst.name)}${src.marketplace === dst.marketplace ? ' · копия карточки в другое юрлицо' : ''}</p>` : ''}
      ${ready.length < 2 ? `<div class="db-note">Для переноса нужны минимум два кабинета с ключом API. Добавьте их в «Кабинеты маркетплейсов» (для Ozon — Client ID и ключ API, для Маркета — ключ API и ID кабинета).</div>` : ''}
      <label class="tr-arts">Артикулы <span class="muted">— по одному в строке или через запятую; артикул продавца или номер WB</span>
        <textarea data-k="articles" rows="3" placeholder="3140-D1&#10;3156-D1">${esc(st.articles)}</textarea></label>
      <div class="tr-actions"><button class="btn" type="button" data-go="auto">⇄ Перенести</button>
        <button class="btn secondary" type="button" data-go="check">Только проверить</button>
        <button class="db-ghost" type="button" data-toggle-settings aria-expanded="${st.showSettings}">⚙ Настройки переноса</button>
        <span class="muted" data-state role="status"></span></div>
      <p class="muted tr-hint">«Перенести» сразу выгружает товары, если всех обязательных данных хватает. Иначе покажет, что дописать. «Только проверить» — ничего не создаёт.</p>`;
    box.querySelectorAll('[data-k]').forEach(el => el.oninput = el.onchange = () => {
      st[el.dataset.k] = el.value; remember();
      if (el.tagName === 'SELECT') drawForm();
    });
    box.querySelector('[data-swap]').onclick = () => { [st.src, st.dst] = [st.dst, st.src]; remember(); drawForm(); };
    box.querySelectorAll('[data-go]').forEach(b => b.onclick = () => start(b.dataset.go === 'auto'));
    box.querySelector('[data-toggle-settings]').onclick = () => { st.showSettings = !st.showSettings; drawSettings(); drawForm(); };
  }

  async function start(auto) {
    const state = root.querySelector('[data-form] [data-state]');
    if (!st.src || !st.dst) { state.textContent = 'Выберите кабинеты'; return; }
    if (!st.articles.trim()) { state.textContent = 'Впишите артикул'; return; }
    state.textContent = 'Запускаю…';
    root.querySelectorAll('[data-go]').forEach(b => { b.disabled = true; });
    try {
      const run = await api('/api/transfer/runs', {method: 'POST', body: {source: st.src, target: st.dst, articles: st.articles, auto}});
      state.textContent = '';
      st.selected = new Set(); st.run = run; drawRun(); poll();
    } catch (e) { state.textContent = e.message; }
    root.querySelectorAll('[data-go]').forEach(b => { b.disabled = false; });
  }

  async function openRun(id) {
    try { st.run = await api('/api/transfer/runs/' + id); st.selected = new Set(); drawRun(); if (st.run.status === 'running') poll(); root.querySelector('[data-run]').scrollIntoView({behavior: 'smooth', block: 'start'}); }
    catch (e) { alert(e.message); }
  }
  function poll() {
    clearTimeout(st.poll);
    st.poll = setTimeout(async () => {
      if (!root?.isConnected || !st.run) return;
      try { st.run = await api('/api/transfer/runs/' + st.run.id); } catch (e) { return poll(); }
      drawRun();
      if (st.run.status === 'running') poll();
      else { st.history = await api('/api/transfer/runs').catch(() => st.history); drawHistory(); }
    }, 2500);
  }

  // ---------------------------------------------------------------- результат переноса
  function drawRun() {
    const box = root.querySelector('[data-run]'), r = st.run;
    if (!r) { box.innerHTML = ''; return; }
    const items = r.items || [], groups = [];
    items.forEach(it => { let g = groups.find(x => x.key === it.article); if (!g) { g = {key: it.article, rows: []}; groups.push(g); } g.rows.push(it); });
    const count = s => items.filter(i => i.status === s).length;
    const running = r.status === 'running', [done, total] = r.progress || [0, 1];
    const targetMp = r.target_mp;
    const uploadable = items.filter(i => i.has_payload && i.status === 'ready');
    const forceable = items.filter(i => i.has_payload && ['ready', 'missing', 'failed'].includes(i.status));
    box.innerHTML = `<div class="tr-box tr-run">
      <div class="tr-run-head"><div><h3>${esc(r.source_title)} → ${esc(r.target_title)}</h3>
        <span class="muted">${new Date(r.created_at).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'})} · ${esc((r.articles || []).join(', '))}</span></div>
        <div class="tr-chips">${Object.entries(STATUS).map(([k, [t, c]]) => count(k) ? `<span class="tr-chip ${c}">${t}: ${count(k)}</span>` : '').join('')}</div></div>
      ${running ? `<div class="tr-progress" role="progressbar" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${done}"><i style="width:${Math.max(4, done / total * 100)}%"></i></div>
        <p class="muted">${esc((r.log || []).slice(-1)[0] || 'Работаю…')}</p>` : ''}
      ${r.status === 'error' ? `<div class="db-note">${esc((r.log || []).filter(l => /Ошибка/.test(l)).slice(-1)[0] || 'Перенос остановился с ошибкой')}</div>` : ''}
      ${!running ? `<div class="tr-actions">
        ${uploadable.length ? `<button class="btn" type="button" data-upload>⇪ Выгрузить ${st.selected.size ? `отмеченные (${[...st.selected].filter(id => uploadable.some(i => i.offer_id === id)).length})` : `готовые (${uploadable.length})`}</button>` : ''}
        ${forceable.length > uploadable.length ? `<button class="btn secondary" type="button" data-force title="Отправить и товары, где чего-то не хватает: площадка сама покажет ошибки">Выгрузить принудительно (${forceable.length})</button>` : ''}
        <button class="btn secondary" type="button" data-rerun title="Собрать заново с учётом выбранных категорий и дописанных значений">↻ Пересобрать</button>
        ${targetMp === 'wildberries' && count('pending') ? '<button class="btn secondary" type="button" data-finalize>Догрузить фото и цены</button>' : ''}
        ${targetMp === 'ozon' && count('uploaded') ? '<button class="btn secondary" type="button" data-cert>Привязать декларацию</button>' : ''}
        <span class="muted" data-run-state role="status"></span></div>` : ''}
      ${groups.map(groupHtml).join('')}
      <details class="tr-log"><summary>Журнал (${(r.log || []).length})</summary><pre>${esc((r.log || []).join('\n'))}</pre></details></div>`;
    bindRun(box);
  }

  function groupHtml(g) {
    const first = g.rows.find(x => x.title && x.category) || g.rows.find(x => x.category) || g.rows[0], r = st.run;
    const cat = first.category, missingAttrs = [...new Set(g.rows.flatMap(x => x.missing || []).filter(m => !NON_ATTR.includes(m)))];
    const otherMissing = [...new Set(g.rows.flatMap(x => x.missing || []).filter(m => NON_ATTR.includes(m)))];
    const filled = Object.entries(first.filled || {});
    const picker = st.picker && st.picker.key === first.category_key;
    return `<article class="tr-group">
      <header>${first.photo ? `<img src="${esc(first.photo)}" alt="" loading="lazy">` : '<div class="tr-noimg">нет фото</div>'}
        <div class="tr-g-main"><b>${esc(g.key)}</b> ${first.title ? `<span class="tr-title">${esc(first.title)}</span>` : ''}
          ${cat ? `<div class="tr-cat">${esc(first.source_category || '—')} → <b>${esc(cat.name)}</b>${cat.path ? ` <span class="muted">· ${esc(cat.path)}</span>` : ''}
            ${cat.score != null && first.auto_category ? `<span class="tr-score ${cat.score < 0.8 ? 'low' : ''}">${cat.score < 0.8 ? 'проверьте категорию' : 'подобрано автоматически'}</span>` : ''}
            ${r.status !== 'running' ? `<button type="button" class="rv-link" data-pick="${esc(first.category_key)}">сменить</button>` : ''}</div>` : ''}
          ${g.rows.every(x => x.status === 'error') ? `<div class="rv-error">${esc(g.rows[0].message)}</div>` : ''}</div></header>
      ${picker ? pickerHtml(first) : ''}
      ${missingAttrs.length ? `<div class="tr-missing"><b>Не хватает обязательных характеристик.</b> Значение сохранится для всей категории «${esc(cat?.name || '')}» — нажмите «Пересобрать».
        ${missingAttrs.map(m => { const name = m.split(' (')[0]; return `<form class="tr-fix" data-fix="${esc(name)}" data-cat="${esc(cat?.name || '')}">
          <label>${esc(m)}<input name="v" required placeholder="значение"></label><button class="btn secondary" type="submit">Сохранить</button>
          <button class="rv-link" type="button" data-skip-attr>не заполнять</button></form>`; }).join('')}</div>` : ''}
      ${otherMissing.length ? `<p class="tr-warn">Нет данных в источнике: ${esc(otherMissing.join(', '))}.</p>` : ''}
      ${g.rows.some(x => x.offer_id || x.status !== 'error') ? `<div class="tr-table-wrap"><table class="tr-table"><thead><tr><th class="c"></th><th>Артикул в приёмнике</th><th>Размер</th><th class="r">Цена</th><th class="r">Старая цена</th><th>Статус</th><th>Комментарий</th></tr></thead><tbody>
        ${g.rows.filter(x => x.offer_id || x.status === 'skipped').map(x => `<tr>
          <td class="c">${x.has_payload && ['ready', 'missing', 'failed'].includes(x.status) ? `<input type="checkbox" data-sel="${esc(x.offer_id)}" ${st.selected.has(x.offer_id) ? 'checked' : ''} aria-label="Отметить ${esc(x.offer_id)}">` : ''}</td>
          <td><b>${esc(x.offer_id || '—')}</b></td><td>${esc((x.sizes || []).join(', ') || '—')}</td>
          <td class="r">${num(x.price)}</td><td class="r">${num(x.old_price)}</td>
          <td><span class="tr-chip ${(STATUS[x.status] || ['', 'mute'])[1]}">${(STATUS[x.status] || [x.status])[0]}</span></td>
          <td class="tr-msg">${esc([x.message, ...(x.notes || []), ...(x.status === 'missing' ? [`нет: ${(x.missing || []).join(', ')}`] : [])].filter(Boolean).join(' · '))}</td></tr>`).join('')}
        </tbody></table></div>` : ''}
      ${filled.length ? `<details class="tr-filled"><summary>Перенесённые характеристики (${filled.length})</summary><dl>${filled.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc([].concat(v).join(', ').slice(0, 300))}</dd>`).join('')}</dl></details>` : ''}
    </article>`;
  }

  function pickerHtml(row) {
    const p = st.picker;
    const opts = (p.results || row.alternatives || []);
    return `<div class="tr-picker"><div class="tr-picker-head"><input type="search" data-cat-q placeholder="Найти категорию в ${esc(MP[st.run.target_mp]?.t || '')}" value="${esc(p.q || '')}">
      <button type="button" class="rv-link" data-pick-close>закрыть</button></div>
      ${p.loading ? '<p class="muted">Ищу…</p>' : ''}
      <div class="tr-cat-list">${opts.map((c, i) => `<button type="button" data-cat-choose="${i}">${esc(c.name)}${c.path ? ` <span class="muted">· ${esc(c.path)}</span>` : ''}${c.score != null ? ` <small>${num(c.score, 2)}</small>` : ''}</button>`).join('')
        || '<p class="muted">Начните вводить название категории.</p>'}</div>
      ${row.auto_category ? '' : '<button type="button" class="rv-link" data-cat-reset>вернуть автоподбор</button>'}</div>`;
  }

  function bindRun(box) {
    const r = st.run, state = box.querySelector('[data-run-state]');
    const act = async (fn, label) => { if (state) state.textContent = label; try { await fn(); } catch (e) { if (state) state.textContent = e.message; else alert(e.message); } };
    box.querySelectorAll('[data-sel]').forEach(cb => cb.onchange = () => { cb.checked ? st.selected.add(cb.dataset.sel) : st.selected.delete(cb.dataset.sel); drawRun(); });
    const up = box.querySelector('[data-upload]');
    if (up) up.onclick = () => act(async () => { st.run = await api(`/api/transfer/runs/${r.id}/upload`, {method: 'POST', body: {offer_ids: st.selected.size ? [...st.selected] : null}}); drawRun(); poll(); }, 'Выгружаю…');
    const force = box.querySelector('[data-force]');
    if (force) force.onclick = () => { if (confirm('Отправить и товары с незаполненными данными? Площадка может их не принять.')) act(async () => { st.run = await api(`/api/transfer/runs/${r.id}/upload`, {method: 'POST', body: {offer_ids: st.selected.size ? [...st.selected] : null, force: true}}); drawRun(); poll(); }, 'Выгружаю…'); };
    const rerun = box.querySelector('[data-rerun]');
    if (rerun) rerun.onclick = () => act(async () => { st.run = await api('/api/transfer/runs', {method: 'POST', body: {source: r.source_id, target: r.target_id, articles: r.articles}}); st.selected = new Set(); drawRun(); poll(); }, 'Пересобираю…');
    const fin = box.querySelector('[data-finalize]');
    if (fin) fin.onclick = () => act(async () => { st.run = await api(`/api/transfer/runs/${r.id}/finalize`, {method: 'POST'}); drawRun(); }, 'Проверяю карточки в WB…');
    const cert = box.querySelector('[data-cert]');
    if (cert) cert.onclick = () => act(async () => { st.run = await api(`/api/transfer/runs/${r.id}/certificate`, {method: 'POST'}); drawRun(); }, 'Привязываю декларацию…');
    box.querySelectorAll('[data-pick]').forEach(b => b.onclick = () => { st.picker = st.picker?.key === b.dataset.pick ? null : {key: b.dataset.pick, q: ''}; drawRun(); box.querySelector('[data-cat-q]')?.focus(); });
    const q = box.querySelector('[data-cat-q]');
    if (q) {
      let timer;
      q.oninput = () => { clearTimeout(timer); st.picker.q = q.value; timer = setTimeout(async () => {
        if (q.value.trim().length < 2) return;
        st.picker.loading = true;
        try { st.picker.results = await api(`/api/transfer/categories?cabinet=${encodeURIComponent(r.target_id)}&q=${encodeURIComponent(q.value.trim())}`); }
        catch (e) { st.picker.results = []; alert(e.message); }
        st.picker.loading = false; drawRun(); const n = root.querySelector('[data-cat-q]'); n.focus(); n.setSelectionRange(n.value.length, n.value.length);
      }, 400); };
    }
    box.querySelector('[data-pick-close]')?.addEventListener('click', () => { st.picker = null; drawRun(); });
    box.querySelectorAll('[data-cat-choose]').forEach(b => b.onclick = () => act(async () => {
      const row = r.items.find(i => i.category_key === st.picker.key);
      const c = (st.picker.results || row.alternatives || [])[Number(b.dataset.catChoose)];
      await api('/api/transfer/category', {method: 'PUT', body: {target_mp: r.target_mp, source_key: st.picker.key, category: c}});
      st.picker = null;
      st.run = await api('/api/transfer/runs', {method: 'POST', body: {source: r.source_id, target: r.target_id, articles: r.articles}}); drawRun(); poll();
    }, 'Категория сохранена, пересобираю…'));
    box.querySelector('[data-cat-reset]')?.addEventListener('click', () => act(async () => {
      await api('/api/transfer/category', {method: 'PUT', body: {target_mp: r.target_mp, source_key: st.picker.key, category: null}});
      st.picker = null;
      st.run = await api('/api/transfer/runs', {method: 'POST', body: {source: r.source_id, target: r.target_id, articles: r.articles}}); drawRun(); poll();
    }, 'Пересобираю…'));
    box.querySelectorAll('[data-fix]').forEach(f => {
      const save = async (body, btn) => { btn.disabled = true; try { await api('/api/transfer/extra', {method: 'PUT', body: {target_mp: r.target_mp, category: f.dataset.cat, attr: f.dataset.fix, ...body}}); f.classList.add('saved'); f.querySelector('input').disabled = true; }
        catch (e) { alert(e.message); btn.disabled = false; } };
      f.onsubmit = e => { e.preventDefault(); save({value: f.elements.v.value}, f.querySelector('[type=submit]')); };
      f.querySelector('[data-skip-attr]').onclick = e => save({skip: true}, e.currentTarget);
    });
  }

  // ---------------------------------------------------------------- история
  function drawHistory() {
    const box = root.querySelector('[data-history]');
    if (!st.history.length) { box.innerHTML = ''; return; }
    box.innerHTML = `<h3>История переносов</h3><div class="tr-table-wrap"><table class="tr-table"><thead><tr><th>Когда</th><th>Откуда → куда</th><th>Артикулы</th><th>Итог</th><th></th></tr></thead><tbody>
      ${st.history.map(h => `<tr><td>${new Date(h.created_at).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'})}</td>
        <td>${esc(h.source_title)} → ${esc(h.target_title)}</td><td class="tr-msg">${esc(h.articles.join(', '))}</td>
        <td>${h.status === 'running' ? '<span class="tr-chip warn">идёт</span>' : Object.entries(h.counts).filter(([, n]) => n).map(([k, n]) => `<span class="tr-chip ${(STATUS[k] || ['', 'mute'])[1]}">${(STATUS[k] || [k])[0]}: ${n}</span>`).join(' ')}</td>
        <td class="r"><button type="button" class="rv-link" data-open="${esc(h.id)}">открыть</button></td></tr>`).join('')}</tbody></table></div>`;
    box.querySelectorAll('[data-open]').forEach(b => b.onclick = () => openRun(b.dataset.open));
  }

  // ---------------------------------------------------------------- настройки
  function drawSettings() {
    const box = root.querySelector('[data-settings]');
    box.hidden = !st.showSettings;
    if (!st.showSettings || !st.settings) return;
    const s = st.settings, t = s.targets[st.setTab] || {};
    const cert = s.certificate || {};
    const rename = Object.entries(s.offer_id_rename || {}).map(([a, b]) => `${a}=${b}`).join('\n');
    box.innerHTML = `<h3>Настройки переноса</h3>
      <div class="rnp-tabs tr-tabs" role="tablist">${Object.entries(MP).map(([k, m]) => `<button type="button" role="tab" data-set-tab="${k}" class="${st.setTab === k ? 'active' : ''}" aria-selected="${st.setTab === k}">В ${m.t}</button>`).join('')}</div>
      <form data-set-form class="tr-set-grid">
        <label>Наценка к цене источника, ×<input name="price_multiplier" type="number" step="0.01" min="0.1" value="${esc(t.price_multiplier ?? 1)}"></label>
        <label>Фиксированная старая цена, ₽<input name="old_price_fixed" type="number" step="1" min="0" value="${esc(t.old_price_fixed || '')}" placeholder="не задана — как у источника"></label>
        <label>Не переносить размеры со словами<input name="skip_size_words" value="${esc(t.skip_size_words || '')}" placeholder="через запятую, например: акц"></label>
        ${st.setTab === 'ozon' ? `<label>НДС на Ozon<select name="vat">${[['0', 'Без НДС'], ['0.05', '5%'], ['0.07', '7%'], ['0.1', '10%'], ['0.2', '20%'], ['0.22', '22%']].map(([v, l]) => `<option value="${v}" ${String(t.vat) === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>` : ''}
        ${st.setTab === 'wildberries' ? `<label class="tr-check"><input type="checkbox" name="new_barcodes" ${t.new_barcodes !== false ? 'checked' : ''}> Получать новые баркоды WB (старые баркоды заняты карточками источника)</label>` : ''}
        <label class="wide">Переименовать артикулы<textarea name="rename" rows="2" placeholder="старый=новый, по одному в строке">${esc(rename)}</textarea></label>
        <div class="wide tr-actions"><button class="btn" type="submit">Сохранить</button><span class="muted" data-set-state role="status"></span></div>
      </form>
      ${savedHtml(t)}
      ${st.setTab === 'ozon' ? `<h4>Декларация соответствия для Ozon</h4>
        <form data-cert-form class="tr-set-grid">
          <label>Название<input name="name" value="${esc(cert.name || 'Декларация соответствия')}"></label>
          <label>Номер<input name="number" value="${esc(cert.number || '')}"></label>
          <label>Дата выдачи<input name="issue_date" type="date" value="${esc(cert.issue_date || '')}"></label>
          <label>Действует до<input name="expire_date" type="date" value="${esc(cert.expire_date || '')}"></label>
          <div class="wide"><span class="muted">Скан: ${(s.certificate_files || []).map(f => `<span class="rnp-chip">${esc(f)} <button type="button" class="rnp-del" data-del-file="${esc(f)}" aria-label="Удалить ${esc(f)}">×</button></span>`).join(' ') || 'не загружен'}</span>
            <label class="db-ghost tr-file">Загрузить PDF/JPG<input type="file" accept=".pdf,.jpg,.jpeg,.png" data-cert-file hidden></label>
            ${cert.certificate_id ? `<span class="muted"> · создана в Ozon, ID ${esc(cert.certificate_id)}</span>` : ''}</div>
          <div class="wide tr-actions"><button class="btn secondary" type="submit">Сохранить декларацию</button><span class="muted" data-cert-state></span></div>
        </form>` : ''}`;
    box.querySelectorAll('[data-set-tab]').forEach(b => b.onclick = () => { st.setTab = b.dataset.setTab; remember(); drawSettings(); });
    const f = box.querySelector('[data-set-form]');
    f.onsubmit = async e => {
      e.preventDefault();
      const rename = {};
      f.elements.rename.value.split('\n').forEach(l => { const [a, b] = l.split('='); if (a?.trim() && b?.trim()) rename[a.trim()] = b.trim(); });
      const target = {price_multiplier: Number(f.elements.price_multiplier.value) || 1, old_price_fixed: Number(f.elements.old_price_fixed.value) || 0,
        skip_size_words: f.elements.skip_size_words.value.trim()};
      if (f.elements.vat) target.vat = f.elements.vat.value;
      if (f.elements.new_barcodes) target.new_barcodes = f.elements.new_barcodes.checked;
      try { st.settings = await api('/api/transfer/settings', {method: 'PUT', body: {targets: {[st.setTab]: target}, offer_id_rename: rename}}); box.querySelector('[data-set-state]').textContent = 'Сохранено'; }
      catch (err) { box.querySelector('[data-set-state]').textContent = err.message; }
    };
    box.querySelectorAll('[data-del-cat]').forEach(b => b.onclick = async () => {
      await api('/api/transfer/category', {method: 'PUT', body: {target_mp: st.setTab, source_key: b.dataset.delCat, category: null}});
      st.settings = await api('/api/transfer/settings'); drawSettings();
    });
    box.querySelectorAll('[data-del-extra]').forEach(b => b.onclick = async () => {
      const [cat, attr] = JSON.parse(b.dataset.delExtra);
      await api('/api/transfer/extra', {method: 'PUT', body: {target_mp: st.setTab, category: cat, attr, value: ''}});
      st.settings = await api('/api/transfer/settings'); drawSettings();
    });
    const cf = box.querySelector('[data-cert-form]');
    if (cf) {
      cf.onsubmit = async e => {
        e.preventDefault();
        const body = {certificate: {name: cf.elements.name.value.trim(), number: cf.elements.number.value.trim(), issue_date: cf.elements.issue_date.value, expire_date: cf.elements.expire_date.value}};
        try { st.settings = await api('/api/transfer/settings', {method: 'PUT', body}); cf.querySelector('[data-cert-state]').textContent = 'Сохранено'; }
        catch (err) { cf.querySelector('[data-cert-state]').textContent = err.message; }
      };
      cf.querySelector('[data-cert-file]').onchange = async e => {
        const fd = new FormData(); fd.append('file', e.target.files[0]);
        try { await api('/api/transfer/certificate-file', {method: 'POST', body: fd}); st.settings = await api('/api/transfer/settings'); drawSettings(); }
        catch (err) { alert(err.message); }
      };
      cf.querySelectorAll('[data-del-file]').forEach(b => b.onclick = async () => {
        await api('/api/transfer/certificate-file/' + encodeURIComponent(b.dataset.delFile), {method: 'DELETE'}); st.settings = await api('/api/transfer/settings'); drawSettings();
      });
    }
  }
  function savedHtml(t) {
    const cats = Object.entries(t.categories || {}), extras = Object.entries(t.extras || {}).flatMap(([c, m]) => Object.entries(m).map(([a, v]) => [c, a, v]));
    if (!cats.length && !extras.length) return '<p class="muted">Сохранённых категорий и значений пока нет — они появятся, когда вы смените категорию или допишете характеристику при переносе.</p>';
    return `<div class="tr-saved">${cats.length ? `<h4>Выбранные категории</h4><ul>${cats.map(([k, c]) => { const [mp, name] = k.split(/:(.*)/s);
        return `<li>${esc(MP[mp]?.s || mp)} «${esc(name)}» → <b>${esc(c.name)}</b> <span class="muted">${esc(c.path || '')}</span> <button type="button" class="rnp-del" data-del-cat="${esc(k)}" aria-label="Удалить">×</button></li>`; }).join('')}</ul>` : ''}
      ${extras.length ? `<h4>Значения характеристик по категориям</h4><ul>${extras.map(([c, a, v]) => `<li>«${esc(c)}» · ${esc(a)}: <b>${v === null ? 'не заполнять' : esc(v)}</b>
        <button type="button" class="rnp-del" data-del-extra="${esc(JSON.stringify([c, a]))}" aria-label="Удалить">×</button></li>`).join('')}</ul>` : ''}</div>`;
  }

  const style = document.createElement('style');
  style.textContent = `
.tr-box{background:#fff;border:1px solid #eceef2;border-radius:14px;padding:16px 18px;margin:14px 0}
.tr-route{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);gap:10px;align-items:end}
.tr-route label,.tr-arts,.tr-set-grid label{display:grid;gap:5px;font-size:13px;font-weight:600;color:#374151}
.tr-route select,.tr-set-grid input,.tr-set-grid select,.tr-arts textarea,.tr-set-grid textarea{border:1px solid #e3e5ea;border-radius:10px;padding:0 12px;height:42px;font:inherit;font-size:14px;font-weight:500;background:#fff;color:#1f2328;min-width:0;width:100%;box-sizing:border-box}
.tr-arts textarea,.tr-set-grid textarea{height:auto;padding:10px 12px;resize:vertical}
.tr-swap{height:42px;width:42px;border:0;border-radius:10px;background:#eef0f4;font-size:18px;cursor:pointer;color:#374151}.tr-swap:hover{background:#e4e7ec}
.tr-dir{margin:10px 0 0;font-size:13px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.tr-arts{margin-top:14px}.tr-arts .muted{font-weight:400}
.tr-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:12px}.tr-hint{font-size:12px;margin:8px 0 0}
.tr-settings h3,.tr-run h3,.tr-history h3{font-size:17px;margin:0 0 10px}.tr-settings h4{font-size:14px;margin:16px 0 8px}
.tr-tabs{margin-bottom:12px;width:max-content;max-width:100%}
.tr-set-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.tr-set-grid .wide{grid-column:1/-1}
.tr-check{display:flex!important;align-items:center;gap:8px;grid-column:1/-1;font-weight:500!important}.tr-check input{width:auto!important;height:auto!important}
.tr-set-grid label.tr-file{display:inline-flex;width:auto;align-items:center;margin-left:8px;cursor:pointer;font-weight:600}
.tr-saved ul{margin:0;padding-left:18px;font-size:13px;display:grid;gap:4px}
.tr-run-head{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start}.tr-run-head h3{margin:0 0 3px}
.tr-chips{display:flex;flex-wrap:wrap;gap:6px}
.tr-chip{display:inline-flex;align-items:center;border-radius:999px;padding:3px 9px;font-size:12px;font-weight:700;background:#f3f4f6;color:#4b5563;white-space:nowrap}
.tr-chip.ok{background:#e3efe3;color:#2f6b2f}.tr-chip.warn{background:#fef3c7;color:#92400e}.tr-chip.bad{background:#f6e0e0;color:#b42323}
.tr-progress{height:8px;background:#eef0f4;border-radius:999px;overflow:hidden;margin:14px 0 6px}.tr-progress i{display:block;height:100%;background:#7c3aed;border-radius:999px;transition:width .4s}
.tr-group{border:1px solid #eceef2;border-radius:12px;padding:12px 14px;margin-top:12px}
.tr-group header{display:flex;gap:12px;align-items:flex-start}.tr-group header img,.tr-noimg{width:54px;height:72px;object-fit:cover;border-radius:8px;background:#f3f4f6;flex:none}
.tr-noimg{display:grid;place-items:center;font-size:10px;color:#9ca3af;text-align:center}
.tr-g-main{min-width:0;flex:1}.tr-g-main>b{font-size:15px}.tr-title{color:#4b5563;font-size:13px}
.tr-cat{font-size:13px;margin-top:4px;display:flex;flex-wrap:wrap;gap:6px;align-items:baseline}
.tr-score{font-size:11px;font-weight:700;background:#ede9fe;color:#5b21b6;border-radius:6px;padding:1px 7px}.tr-score.low{background:#fef3c7;color:#92400e}
.tr-picker{margin:10px 0;background:#f8f9fb;border-radius:10px;padding:10px}.tr-picker-head{display:flex;gap:10px;align-items:center}
.tr-picker input{flex:1;height:38px;border:1px solid #e3e5ea;border-radius:9px;padding:0 10px;font:inherit}
.tr-cat-list{display:grid;gap:4px;margin-top:8px;max-height:260px;overflow:auto}
.tr-cat-list button{text-align:left;border:1px solid #e5e7eb;background:#fff;border-radius:8px;padding:7px 10px;font:inherit;font-size:13px;cursor:pointer}.tr-cat-list button:hover{border-color:#8b5cf6}
.tr-missing{background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:10px 12px;margin:10px 0;font-size:13px;color:#92400e;display:grid;gap:8px}
.tr-fix{display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap}.tr-fix label{display:grid;gap:4px;flex:1;min-width:220px;color:#374151;font-weight:600}
.tr-fix input{height:36px;border:1px solid #e3e5ea;border-radius:8px;padding:0 10px;font:inherit;background:#fff}.tr-fix.saved input{border-color:#7fbd80;background:#ecfdf0}
.tr-warn{font-size:13px;color:#b42323;margin:8px 0}
.tr-table-wrap{overflow-x:auto;margin-top:10px;border:1px solid #f0f1f4;border-radius:10px}
.tr-table{width:100%;border-collapse:collapse;font-size:13px}.tr-table th,.tr-table td{padding:7px 10px;border-bottom:1px solid #f0f1f4;text-align:left;text-transform:none;letter-spacing:normal}
.tr-table thead th{background:#f8f9fb;font-size:12px;font-weight:600;color:#374151}.tr-table .r{text-align:right;font-variant-numeric:tabular-nums}.tr-table .c{text-align:center;width:34px}
.tr-msg{color:#4b5563;max-width:520px}
.tr-filled{margin-top:8px;font-size:13px}.tr-filled summary{cursor:pointer;color:#5b21b6;font-weight:600}
.tr-filled dl{display:grid;grid-template-columns:minmax(160px,260px) 1fr;gap:4px 12px;margin:8px 0 0}.tr-filled dt{color:#6b7280}.tr-filled dd{margin:0}
.tr-log{margin-top:12px;font-size:12px}.tr-log summary{cursor:pointer;color:#6b7280}.tr-log pre{white-space:pre-wrap;background:#f8f9fb;border-radius:8px;padding:10px;max-height:260px;overflow:auto}
.tr-history{margin-top:18px}
.wb-member [data-nav=transfer]{display:none!important}
@media(max-width:800px){.tr-route{grid-template-columns:1fr}.tr-swap{justify-self:center;transform:rotate(90deg)}.tr-set-grid{grid-template-columns:1fr}.tr-filled dl{grid-template-columns:1fr}}`;
  document.head.appendChild(style);

  return {render};
})();
