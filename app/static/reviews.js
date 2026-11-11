/* WB OPS: раздел «Все отзывы» — фильтры, карточки отзывов и ответов, редактирование и отправка, групповые действия. */
window.WBOPSReviews = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const MP = {wildberries: ['wb', 'wb'], ozon: ['ozon', 'ozon'], yandex: ['ym', 'Я.М']};
  const METHOD_ICON = {ai: '🤖', individual: '🧩', templates: '📄', knowledge: '📚', manual: '✍️'};
  const STATUS_CLS = {published: 'ok', approved: 'info', draft: '', nodraft: 'muted', rework: 'warn', error: 'bad'};
  const RATING_OPTIONS = [['', 'Рейтинг'], ['5', '★★★★★ 5'], ['4', '★★★★ 4'], ['3', '★★★ 3'], ['2', '★★ 2'], ['1', '★ 1'], ['1,2,3', 'Негатив 1–3★'], ['4,5', 'Позитив 4–5★']];
  const FILTER_KEYS = ['search', 'from', 'to', 'marketplace', 'cabinet_id', 'order_status', 'answer', 'rating', 'tag', 'sort'];
  const MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];

  const st = {f: {sort: 'desc'}, page: 0, size: 50, meta: null, data: null, selected: new Set(), open: new Set(), editing: null, busy: new Set(), auto: null};
  let root = null, searchTimer = null, loadSeq = 0;
  const photos = {};            // WB ID → ссылка на фото (или null — фото нет)

  async function api(url, options = {}) {
    if (options.body && typeof options.body !== 'string') options = {...options, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(options.body)};
    const r = await fetch(url, options);
    let d = null;
    try { d = await r.json(); } catch (e) { /* пусто */ }
    if (!r.ok) {
      const detail = d && d.detail;
      const msg = typeof detail === 'string' ? detail : detail && detail.message ? `${detail.message}: ${(detail.warnings || []).join('; ')}` : `Ошибка ${r.status}`;
      throw new Error(msg);
    }
    return d;
  }
  function toast(text) {
    const t = document.getElementById('toast');
    if (!t) return;
    t.textContent = text; t.style.display = 'block';
    clearTimeout(toast.timer); toast.timer = setTimeout(() => t.style.display = 'none', 4000);
  }
  const dateParts = s => {
    const d = new Date(s);
    return isNaN(d) ? ['', '', ''] : [`${d.getDate()} ${MONTHS[d.getMonth()]},`, String(d.getFullYear()), d.toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit'})];
  };
  const shortDate = s => s ? s.slice(0, 10).split('-').reverse().join('.') : '';
  const stars = n => n ? `<span class="rv-stars" aria-label="Оценка ${n} из 5">${'★'.repeat(n)}<i>${'★'.repeat(5 - n)}</i></span>` : '<span class="muted">без оценки</span>';

  // ---------------------------------------------------------------- состояние в адресе
  function readUrl() {
    const p = new URLSearchParams(location.search);
    FILTER_KEYS.forEach(k => { if (p.get(k)) st.f[k] = p.get(k); });
    st.page = Math.max(0, (+p.get('page') || 1) - 1);
    if (+p.get('size')) st.size = +p.get('size');
  }
  function writeUrl() {
    if (location.pathname !== '/reviews') return;
    const p = new URLSearchParams();
    FILTER_KEYS.forEach(k => { if (st.f[k] && !(k === 'sort' && st.f[k] === 'desc')) p.set(k, st.f[k]); });
    if (st.page) p.set('page', st.page + 1);
    if (st.size !== 50) p.set('size', st.size);
    history.replaceState(history.state, '', '/reviews' + (p.toString() ? '?' + p : ''));
  }
  const query = (extra = {}) => {
    const p = new URLSearchParams();
    FILTER_KEYS.forEach(k => { if (st.f[k]) p.set(k === 'from' ? 'from' : k === 'to' ? 'to' : k, st.f[k]); });
    Object.entries(extra).forEach(([k, v]) => p.set(k, v));
    return p.toString();
  };
  const activeFilters = () => FILTER_KEYS.filter(k => k !== 'sort' && st.f[k]).length;

  // ---------------------------------------------------------------- каркас
  async function render(section) {
    root = section;
    root.className = 'panel rv-page';
    readUrl();
    root.innerHTML = `<div class="rv-head"><div class="rv-counters" data-counters></div>
        <div class="rv-head-actions"><button type="button" class="rv-dark" data-auto>Автоответы…</button>
        <button type="button" class="rv-dark" data-sync>Обновить список</button>
        <div class="rv-menu-wrap"><button type="button" class="rv-dark rv-sq" data-export aria-haspopup="menu" aria-label="Скачать" title="Скачать список">⇩</button>
          <div class="rv-menu" role="menu" hidden><a role="menuitem" data-export-fmt="xlsx">Excel (.xlsx)</a><a role="menuitem" data-export-fmt="csv">CSV</a></div></div></div></div>
      <div data-banner></div>
      <div class="rv-filters" data-filters></div>
      <div class="rv-list" data-list aria-live="polite"><p class="muted">Загрузка…</p></div>
      <div class="rv-bulk" data-bulk hidden></div>`;
    bindHead();
    await load(true);
    loadAuto();
  }

  function bindHead() {
    root.querySelector('[data-sync]').onclick = syncAll;
    root.querySelector('[data-auto]').onclick = toggleAuto;
    const menu = root.querySelector('.rv-menu'), btn = root.querySelector('[data-export]');
    btn.onclick = e => { e.stopPropagation(); menu.hidden = !menu.hidden; };
    menu.querySelectorAll('[data-export-fmt]').forEach(a => a.onclick = () => {
      menu.hidden = true;
      location.href = `/api/reviews/export?${query({format: a.dataset.exportFmt})}`;
    });
    document.addEventListener('click', e => {
      if (!root || !root.isConnected) return;
      if (!e.target.closest('.rv-menu-wrap')) menu.hidden = true;
      if (!e.target.closest('.rv-pop-wrap')) root.querySelectorAll('.rv-pop').forEach(p => p.hidden = true);
    });
    document.addEventListener('keydown', e => {
      if (e.key !== 'Escape' || !root || !root.isConnected) return;
      root.querySelectorAll('.rv-pop, .rv-menu').forEach(p => p.hidden = true);
    });
  }

  async function load(withMeta = false) {
    const seq = ++loadSeq;
    writeUrl();
    const list = root.querySelector('[data-list]');
    list.classList.add('rv-loading');
    try {
      const d = await api(`/api/reviews?${query({limit: st.size, offset: st.page * st.size, ...(withMeta || !st.meta ? {meta: 1} : {})})}`);
      if (seq !== loadSeq) return;
      if (d.meta) st.meta = d.meta;
      st.data = d;
      if (st.page && !d.items.length && d.total) { st.page = 0; return load(); }
      drawFilters(); drawCounters(); drawBanner(); drawList(); drawBulk();
      loadPhotos();
    } catch (e) {
      list.innerHTML = `<p class="rv-error">Не удалось загрузить отзывы: ${esc(e.message)}</p>`;
    } finally { list.classList.remove('rv-loading'); }
  }

  async function loadPhotos() {
    const need = [...new Set(st.data.items.map(i => i.product_id).filter(n => n && !(n in photos)))];
    if (!need.length) return;
    try {
      const d = await api(`/api/wb/images?nm=${need.join(',')}`);
      Object.assign(photos, d.urls);
    } catch (e) { need.forEach(n => photos[n] = null); return; }
    root.querySelectorAll('[data-photo]').forEach(box => {
      const url = photos[box.dataset.photo];
      if (url && !box.querySelector('img')) box.insertAdjacentHTML('afterbegin', `<img loading="lazy" src="${esc(url)}" alt="" onerror="this.remove()">`);
    });
  }

  // ---------------------------------------------------------------- шапка
  function drawCounters() {
    const c = st.data.counts, total = Object.entries(c).filter(([k]) => k !== 'unanswered').reduce((a, [, v]) => a + v, 0);
    const chip = (key, label, n) => `<button type="button" class="rv-count ${st.f.answer === key || (!key && !st.f.answer) ? 'on' : ''}" data-count="${key}">${label} <b>${n}</b></button>`;
    root.querySelector('[data-counters]').innerHTML = chip('', 'Все', total) + chip('unanswered', 'Без ответа', c.unanswered) +
      (c.rework ? chip('rework', 'На доработке', c.rework) : '') + (c.error ? chip('error', 'Ошибка отправки', c.error) : '') + chip('published', 'Отвечено', c.published) +
      (st.meta && st.meta.complaints ? `<a class="rv-count rv-complaints" href="/complaints" data-nav="complaints">Жалобы к подаче <b>${st.meta.complaints}</b> →</a>` : '');
    root.querySelectorAll('[data-count]').forEach(b => b.onclick = () => setFilter('answer', b.dataset.count));
  }
  function drawBanner() {
    const off = st.meta && st.meta.publishing && !st.meta.publishing.enabled;
    root.querySelector('[data-banner]').innerHTML = off ? '<div class="rv-banner">Отправка в маркетплейс выключена (режим черновиков): ответы можно править и одобрять, но кнопка «Отправить» недоступна. Включается в файле настроек сервиса.</div>' : '';
  }
  async function loadAuto() {
    const b = root.querySelector('[data-auto]');
    try { st.auto = await api('/api/settings/auto-replies'); } catch (e) { b.hidden = true; return; }
    b.textContent = st.auto.enabled ? 'Автоответы включены' : 'Включить автоответы';
    b.classList.toggle('on', !!st.auto.enabled);
    b.title = st.auto.enabled ? 'Новые отзывы получают черновики автоматически при загрузке. Нажмите, чтобы выключить.' : 'Черновики ответов будут создаваться автоматически при загрузке отзывов (правила — в «Настройках ответов»).';
  }
  async function toggleAuto() {
    if (!st.auto) return;
    const b = root.querySelector('[data-auto]');
    b.disabled = true;
    try {
      st.auto = await api('/api/settings/auto-replies', {method: 'PUT', body: {...st.auto, enabled: !st.auto.enabled}});
      toast(st.auto.enabled ? `Автоответы включены${st.auto.auto_publish ? ' (с автоотправкой)' : ' — ответы сохраняются черновиками'}` : 'Автоответы выключены');
    } catch (e) { toast(e.message); }
    b.disabled = false; loadAuto();
  }
  async function syncAll() {
    const b = root.querySelector('[data-sync]');
    const cabs = (st.meta?.cabinets || []).filter(c => c.supported && c.token_configured && (c.reviews || c.questions));
    if (!cabs.length) { toast('Нет подключённых кабинетов Wildberries с ключом — добавьте в «Кабинетах маркетплейсов»'); return; }
    b.disabled = true;
    let received = 0; const errors = [];
    for (const [i, c] of cabs.entries()) {
      b.textContent = `Загрузка ${i + 1}/${cabs.length}…`;
      try { received += (await api(`/api/sync/${encodeURIComponent(c.id)}`, {method: 'POST'})).received; }
      catch (e) { errors.push(`${c.name}: ${e.message}`); }
    }
    b.disabled = false; b.textContent = 'Обновить список';
    toast(errors.length ? `Получено ${received}. Ошибки: ${errors.join('; ')}` : `Готово: получено новых ${received}`);
    load(true);
  }

  // ---------------------------------------------------------------- фильтры
  function setFilter(key, value) {
    if (value) st.f[key] = value; else delete st.f[key];
    st.page = 0; st.selected.clear();
    load();
  }
  function drawFilters() {
    const m = st.meta, f = st.f;
    const sel = (key, label, options) => `<label class="rv-select ${f[key] ? 'set' : ''}"><span class="sr">${label}</span><select data-f="${key}">
        <option value="">${label}</option>${options.map(([v, t]) => `<option value="${esc(v)}" ${String(f[key] || '') === String(v) ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>
        ${f[key] ? `<button type="button" class="rv-x" data-clear="${key}" aria-label="Сбросить: ${label}">×</button>` : ''}</label>`;
    const cabinets = (m.cabinets || []).filter(c => !f.marketplace || c.marketplace === f.marketplace);
    const range = f.from || f.to ? `${f.from ? shortDate(f.from) : '…'} — ${f.to ? shortDate(f.to) : '…'}` : 'Выберите диапазон дат';
    const tags = (f.tag || '').split(',').filter(Boolean);
    const tagMeta = Object.fromEntries((m.tags || []).map(t => [t.tag, t]));
    const polarityTitle = {positive: 'хвалят', negative: 'жалуются', any: 'любое'};
    root.querySelector('[data-filters]').innerHTML = `
      <label class="rv-search"><span class="sr">Поиск</span><input type="search" data-search placeholder="Поиск по тексту, имени, товару, артикулу или WB ID…" value="${esc(f.search || '')}"><span aria-hidden="true">⌕</span></label>
      <div class="rv-pop-wrap"><button type="button" class="rv-range ${f.from || f.to ? 'set' : ''}" data-range aria-haspopup="dialog">📅 ${esc(range)}</button>
        <div class="rv-pop rv-range-pop" hidden role="dialog" aria-label="Диапазон дат">
          <div class="rv-presets">${[['today', 'Сегодня'], ['7', '7 дней'], ['30', '30 дней'], ['month', 'Этот месяц'], ['90', '90 дней'], ['all', 'Всё время']].map(([k, t]) => `<button type="button" data-preset="${k}">${t}</button>`).join('')}</div>
          <div class="rv-range-inputs"><label>с<input type="date" data-from value="${esc(f.from || '')}"></label><label>по<input type="date" data-to value="${esc(f.to || '')}"></label></div>
          <div class="rv-pop-actions"><button type="button" class="btn" data-range-apply>Применить</button><button type="button" class="btn secondary" data-range-clear>Сбросить</button></div></div></div>
      ${sel('marketplace', 'Маркетплейс', Object.entries(m.marketplaces || {}))}
      ${sel('cabinet_id', 'Кабинеты', cabinets.map(c => [c.id, c.name]))}
      ${sel('order_status', 'Статус', Object.entries(m.order_statuses || {}))}
      ${sel('answer', 'Статус ответа', Object.entries(m.answer_statuses || {}))}
      ${sel('rating', 'Рейтинг', RATING_OPTIONS.slice(1))}
      <div class="rv-pop-wrap"><div class="rv-tagbox ${tags.length ? 'set' : ''}">
          <button type="button" class="rv-tagbtn" data-tags aria-haspopup="dialog">${tags.length ? '' : '<span class="rv-ph">Тег</span>'}</button>
          ${tags.map(t => { const [k, p] = t.split(':'); return `<span class="rv-chip">${tagMeta[k]?.icon || ''} ${esc(tagMeta[k]?.title || k)}${p && p !== 'any' ? ` · ${polarityTitle[p]}` : ''}<button type="button" data-untag="${esc(t)}" aria-label="Убрать тег">×</button></span>`; }).join('')}
          ${tags.length ? '<button type="button" class="rv-x" data-clear="tag" aria-label="Сбросить теги">×</button>' : ''}<span class="rv-chev" aria-hidden="true">⌄</span></div>
        <div class="rv-pop rv-tag-pop" hidden role="dialog" aria-label="Теги">
          <p class="muted">Теги находятся по тексту отзыва — те же, что в «Аналитике».</p>
          ${(m.tags || []).map(t => `<div class="rv-tagrow"><span>${t.icon} ${esc(t.title)}</span><span class="rv-tagopts">
            ${[['any', 'любое']].concat(t.positive ? [['positive', '👍 хвалят']] : []).concat(t.negative ? [['negative', '👎 жалуются']] : []).map(([p, label]) =>
              `<button type="button" class="${tags.includes(`${t.tag}:${p}`) ? 'on' : ''}" data-tag="${t.tag}:${p}">${label}</button>`).join('')}</span></div>`).join('')}
        </div></div>
      ${activeFilters() ? `<div class="rv-found">Найдено: <b>${st.data.total}</b> <button type="button" class="rv-link" data-reset>Сбросить все фильтры</button></div>` : ''}`;
    bindFilters();
  }

  function bindFilters() {
    const box = root.querySelector('[data-filters]');
    const search = box.querySelector('[data-search]');
    search.oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => setFilter('search', search.value.trim()), 350); };
    search.onkeydown = e => { if (e.key === 'Enter') { clearTimeout(searchTimer); setFilter('search', search.value.trim()); } };
    box.querySelectorAll('select[data-f]').forEach(s => s.onchange = () => {
      if (s.dataset.f === 'marketplace' && st.f.cabinet_id) delete st.f.cabinet_id;
      setFilter(s.dataset.f, s.value);
    });
    box.querySelectorAll('[data-clear]').forEach(b => b.onclick = e => { e.stopPropagation(); setFilter(b.dataset.clear, ''); });
    const reset = box.querySelector('[data-reset]');
    if (reset) reset.onclick = () => { st.f = {sort: st.f.sort || 'desc'}; st.page = 0; load(); };
    // даты
    const rangeBtn = box.querySelector('[data-range]'), rangePop = box.querySelector('.rv-range-pop');
    rangeBtn.onclick = e => { e.stopPropagation(); const open = rangePop.hidden; box.querySelectorAll('.rv-pop').forEach(p => p.hidden = true); rangePop.hidden = !open; };
    rangePop.onclick = e => e.stopPropagation();
    const today = new Date(), iso = d => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    rangePop.querySelectorAll('[data-preset]').forEach(b => b.onclick = () => {
      const k = b.dataset.preset;
      let from = '', to = iso(today);
      if (k === 'today') from = to;
      else if (k === 'month') from = iso(new Date(today.getFullYear(), today.getMonth(), 1));
      else if (k === 'all') to = '';
      else from = iso(new Date(today.getTime() - (+k - 1) * 864e5));
      st.f.from = from; st.f.to = to;
      if (!from) delete st.f.from; if (!to) delete st.f.to;
      st.page = 0; load();
    });
    rangePop.querySelector('[data-range-apply]').onclick = () => {
      const from = rangePop.querySelector('[data-from]').value, to = rangePop.querySelector('[data-to]').value;
      if (from && to && from > to) { toast('Дата «с» позже даты «по»'); return; }
      if (from) st.f.from = from; else delete st.f.from;
      if (to) st.f.to = to; else delete st.f.to;
      st.page = 0; load();
    };
    rangePop.querySelector('[data-range-clear]').onclick = () => { delete st.f.from; delete st.f.to; st.page = 0; load(); };
    // теги
    const tagPop = box.querySelector('.rv-tag-pop');
    box.querySelector('.rv-tagbox').onclick = e => {
      if (e.target.closest('[data-untag],[data-clear]')) return;
      e.stopPropagation(); const open = tagPop.hidden; box.querySelectorAll('.rv-pop').forEach(p => p.hidden = true); tagPop.hidden = !open;
    };
    tagPop.onclick = e => e.stopPropagation();
    box.querySelectorAll('[data-untag]').forEach(b => b.onclick = e => {
      e.stopPropagation();
      setFilter('tag', (st.f.tag || '').split(',').filter(t => t && t !== b.dataset.untag).join(','));
    });
    tagPop.querySelectorAll('[data-tag]').forEach(b => b.onclick = () => {
      const [k] = b.dataset.tag.split(':');
      let tags = (st.f.tag || '').split(',').filter(Boolean);
      const had = tags.includes(b.dataset.tag);
      tags = tags.filter(t => !t.startsWith(k + ':'));
      if (!had) tags.push(b.dataset.tag);
      setFilter('tag', tags.slice(0, 6).join(','));
      setTimeout(() => root.querySelector('.rv-tag-pop')?.removeAttribute('hidden'), 0);
    });
  }

  // ---------------------------------------------------------------- список
  function drawList() {
    const d = st.data, list = root.querySelector('[data-list]');
    if (!d.items.length) {
      list.innerHTML = `<div class="rv-empty"><b>${activeFilters() ? 'По фильтрам ничего не найдено' : 'Отзывов пока нет'}</b>
        <p class="muted">${activeFilters() ? 'Измените или сбросьте фильтры.' : 'Нажмите «Обновить список», чтобы загрузить отзывы из Wildberries, или загрузите историю в «Импорте отзывов».'}</p></div>`;
      return;
    }
    const allOnPage = d.items.length && d.items.every(i => st.selected.has(i.id));
    const pages = Math.max(1, Math.ceil(d.total / st.size));
    list.innerHTML = `<div class="rv-row rv-th"><label class="rv-cb"><input type="checkbox" data-select-all ${allOnPage ? 'checked' : ''} aria-label="Выбрать все на странице"></label>
        <button type="button" class="rv-sort" data-sort aria-label="Сортировка по дате">Дата ${st.f.sort === 'asc' ? '↑' : '↓'}</button><div>Товар</div><div>Отзыв</div><div>Ответ</div></div>
      ${d.items.map(rowHtml).join('')}
      <div class="rv-pager"><select data-size aria-label="Отзывов на странице">${[10, 20, 50, 100].map(n => `<option value="${n}" ${n === st.size ? 'selected' : ''}>${n}</option>`).join('')}</select>
        <span>${st.page * st.size + 1} – ${Math.min(d.total, (st.page + 1) * st.size)} из ${d.total}</span>
        <button type="button" class="rv-pg" data-page="${st.page - 1}" ${st.page ? '' : 'disabled'} aria-label="Предыдущая страница">‹</button>
        ${pageButtons(pages)}
        <button type="button" class="rv-pg" data-page="${st.page + 1}" ${st.page < pages - 1 ? '' : 'disabled'} aria-label="Следующая страница">›</button></div>`;
    bindList();
  }
  function pageButtons(pages) {
    const set = new Set([0, pages - 1, st.page - 1, st.page, st.page + 1].filter(p => p >= 0 && p < pages));
    let prev = -1, out = '';
    [...set].sort((a, b) => a - b).forEach(p => {
      if (p - prev > 1) out += '<span class="rv-gap">…</span>';
      out += `<button type="button" class="rv-pg ${p === st.page ? 'on' : ''}" data-page="${p}" ${p === st.page ? 'aria-current="page"' : ''}>${p + 1}</button>`;
      prev = p;
    });
    return out;
  }

  function rowHtml(i) {
    const [d1, d2, d3] = dateParts(i.created_at);
    const mp = MP[i.marketplace] || ['', i.marketplace];
    const wbUrl = i.marketplace === 'wildberries' && i.product_id ? `https://www.wildberries.ru/catalog/${i.product_id}/detail.aspx` : '';
    const longText = (i.text || '').length > 160, open = st.open.has(i.id), openAns = st.open.has('a:' + i.id);
    const editing = st.editing === i.id, busy = st.busy.has(i.id);
    const canSend = st.meta?.publishing?.enabled;
    const warn = i.warnings && i.warnings.length;
    return `<div class="rv-row ${st.selected.has(i.id) ? 'sel' : ''}" data-id="${esc(i.id)}">
      <label class="rv-cb rv-cell"><input type="checkbox" data-select="${esc(i.id)}" ${st.selected.has(i.id) ? 'checked' : ''} aria-label="Выбрать отзыв"></label>
      <div class="rv-cell rv-date">${d1}<br>${d2}<br>${d3}</div>
      <div class="rv-cell rv-product">
        <div class="rv-photo" data-photo="${i.product_id || ''}">${photos[i.product_id] ? `<img loading="lazy" src="${esc(photos[i.product_id])}" alt="" onerror="this.remove()">` : ''}<span aria-hidden="true">👖</span></div>
        <div class="rv-pinfo"><div class="rv-badges"><span class="rv-mp ${mp[0]}">${esc(mp[1])}</span><span class="rv-cab">${esc(i.cabinet_name)}</span></div>
          <div class="rv-pname" title="${esc(i.product_name)}">${esc(i.product_name || 'Товар')}</div>
          <div class="rv-art"><span>${esc([i.vendor_code, i.product_id].filter(Boolean).join(' · '))}</span>${wbUrl ? `<a href="${wbUrl}" target="_blank" rel="noopener" aria-label="Открыть товар на Wildberries" title="Открыть на Wildberries">↗</a>` : ''}</div></div></div>
      <div class="rv-cell rv-review">
        <div class="rv-icons">${i.tags.map(t => `<span class="rv-tag ${t.polarity}" title="${esc(t.title)}: ${t.polarity === 'positive' ? 'хвалят' : 'жалуются'}">${t.icon}</span>`).join('')}
          ${i.has_photo ? '<span class="rv-tag media" title="Есть фото">📷</span>' : ''}${i.has_video ? '<span class="rv-tag media" title="Есть видео">🎬</span>' : ''}
          <span class="rv-order ${i.order_status}">${esc(i.order_status_title)}</span></div>
        ${stars(i.rating)}
        <div class="rv-line"><b>Имя:</b> ${esc(i.author_name || 'Аноним')}</div>
        <div class="rv-line rv-text ${open ? 'open' : ''}"><b>Текст:</b> ${i.text ? esc(i.text) : '<span class="muted">без текста</span>'}</div>
        ${longText ? `<button type="button" class="rv-more" data-toggle="${esc(i.id)}">${open ? 'свернуть' : 'подробнее'}</button>` : ''}</div>
      <div class="rv-cell rv-answer">
        <div class="rv-ahead">${i.method_title ? `<span class="rv-method">${METHOD_ICON[i.method] || ''} ${esc(i.method_title)}</span>` : ''}
          <span class="rv-status ${STATUS_CLS[i.status] || ''}">${i.status === 'published' ? '✓ ' : ''}${esc(i.status_title)}${i.status === 'published' && i.published_at ? ` ${shortDate(i.published_at)}` : ''}</span>
          ${warn ? `<span class="rv-warn" tabindex="0" title="${esc(i.warnings.join('\n'))}" aria-label="Есть замечания">ⓘ</span>` : ''}
          ${i.status !== 'published' && !editing ? `<button type="button" class="rv-icon" data-edit="${esc(i.id)}" aria-label="Изменить ответ" title="Изменить">✎</button>` : ''}</div>
        ${editing ? `<textarea data-text rows="6" maxlength="5000" aria-label="Текст ответа" ${busy ? 'readonly' : ''}>${esc(i.pending ?? i.answer)}</textarea>
            <div class="rv-counter"><span data-len>${(i.pending ?? i.answer).length}</span> / 5000 · Ctrl+Enter — сохранить</div>
            <div class="rv-actions"><button type="button" class="btn" data-save ${busy ? 'disabled' : ''}>Сохранить</button><button type="button" class="btn secondary" data-cancel>Отмена</button>
              ${canSend ? `<button type="button" class="btn secondary" data-send ${busy ? 'disabled' : ''}>Отправить</button>` : ''}</div>`
          : `${i.answer ? `<div class="rv-atext ${openAns ? 'open' : ''}">${esc(i.answer)}</div>${i.answer.length > 170 ? `<button type="button" class="rv-more" data-toggle="a:${esc(i.id)}">${openAns ? 'свернуть' : 'подробнее'}</button>` : ''}` : '<p class="muted">Черновика нет — нажмите «Составить».</p>'}
            ${warn ? `<div class="rv-warnbox">${i.warnings.map(esc).join('<br>')}</div>` : ''}
            ${i.moderation_comment ? `<div class="rv-note">Комментарий: ${esc(i.moderation_comment)}</div>` : ''}
            ${i.status === 'published' ? `<div class="rv-sent muted">Отправлен${i.published_by ? ` · ${esc(i.published_by)}` : ''}</div>`
              : `<div class="rv-actions">${canSend && i.answer ? `<button type="button" class="btn rv-send" data-send ${busy || warn ? 'disabled' : ''} title="${warn ? 'Сначала исправьте замечания' : 'Одобрить и отправить в маркетплейс'}">${busy ? 'Отправка…' : 'Отправить'}</button>` : ''}
                ${!canSend && i.answer && i.status !== 'approved' ? `<button type="button" class="btn secondary" data-approve ${busy ? 'disabled' : ''}>Одобрить</button>` : ''}
                <button type="button" class="btn secondary" data-regen ${busy ? 'disabled' : ''} title="Составить ответ заново">${i.answer ? '↻ Заново' : 'Составить'}</button></div>`}`}
      </div></div>`;
  }

  function item(id) { return st.data.items.find(i => i.id === id); }
  function redrawRow(id) {
    const el = root.querySelector(`.rv-row[data-id="${CSS.escape(id)}"]`), i = item(id);
    if (!el || !i) return;
    el.outerHTML = rowHtml(i);
    bindRow(root.querySelector(`.rv-row[data-id="${CSS.escape(id)}"]`));
  }

  function bindList() {
    const list = root.querySelector('[data-list]');
    list.querySelector('[data-sort]').onclick = () => { st.f.sort = st.f.sort === 'asc' ? 'desc' : 'asc'; st.page = 0; load(); };
    list.querySelector('[data-select-all]').onchange = e => {
      st.data.items.forEach(i => e.target.checked ? st.selected.add(i.id) : st.selected.delete(i.id));
      drawList(); drawBulk();
    };
    list.querySelector('[data-size]').onchange = e => { st.size = +e.target.value; st.page = 0; load(); };
    list.querySelectorAll('[data-page]').forEach(b => b.onclick = () => { st.page = +b.dataset.page; load(); root.scrollIntoView({block: 'start'}); });
    list.querySelectorAll('.rv-row[data-id]').forEach(bindRow);
  }

  function bindRow(row) {
    const id = row.dataset.id;
    row.querySelector('[data-select]').onchange = e => {
      e.target.checked ? st.selected.add(id) : st.selected.delete(id);
      row.classList.toggle('sel', e.target.checked); drawBulk();
      const all = root.querySelector('[data-select-all]');
      if (all) all.checked = st.data.items.every(i => st.selected.has(i.id));
    };
    row.querySelectorAll('[data-toggle]').forEach(b => b.onclick = () => {
      const key = b.dataset.toggle; st.open.has(key) ? st.open.delete(key) : st.open.add(key); redrawRow(id);
    });
    const edit = row.querySelector('[data-edit]');
    if (edit) edit.onclick = () => {
      st.editing = id; redrawRow(id);
      const ta = root.querySelector(`.rv-row[data-id="${CSS.escape(id)}"] [data-text]`);
      if (ta) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
    };
    const ta = row.querySelector('[data-text]');
    if (ta) {
      const resize = () => { ta.style.height = 'auto'; ta.style.height = Math.min(400, ta.scrollHeight + 2) + 'px'; };
      resize();
      ta.oninput = () => { row.querySelector('[data-len]').textContent = ta.value.length; resize(); };
      ta.onkeydown = e => {
        if (e.key === 'Escape') { st.editing = null; redrawRow(id); }
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) save(id, ta.value);
      };
      row.querySelector('[data-cancel]').onclick = () => { st.editing = null; redrawRow(id); };
      row.querySelector('[data-save]').onclick = () => save(id, ta.value);
    }
    row.querySelector('[data-send]')?.addEventListener('click', () => send(id, ta ? ta.value : null));
    row.querySelector('[data-approve]')?.addEventListener('click', () => approve(id));
    row.querySelector('[data-regen]')?.addEventListener('click', () => regenerate(id));
  }

  // ---------------------------------------------------------------- действия
  async function refreshItem(id) {
    const d = await api(`/api/reviews?search=${encodeURIComponent(id)}&limit=5`).catch(() => null);
    const fresh = d && d.items.find(x => x.id === id);
    const idx = st.data.items.findIndex(x => x.id === id);
    if (fresh && idx >= 0) {
      if (st.editing === id) fresh.pending = st.data.items[idx].pending;   // правка не теряется при ошибке
      st.data.items[idx] = fresh;
    }
  }
  async function withBusy(id, fn) {
    st.busy.add(id); redrawRow(id);
    try { await fn(); } catch (e) { toast(e.message); }
    st.busy.delete(id);
    await refreshItem(id);
    redrawRow(id);
  }
  const keepText = (id, text) => { const it = item(id); if (it && text != null) it.pending = text; };
  async function save(id, text) {
    text = (text || '').trim();
    if (text.length < 2) { toast('Ответ слишком короткий'); return; }
    keepText(id, text);
    await withBusy(id, async () => {
      await api(`/api/replies/drafts/${encodeURIComponent(id)}`, {method: 'PUT', body: {text}});
      st.editing = null; toast('Ответ сохранён');
    });
  }
  async function approve(id) {
    await withBusy(id, async () => { await api(`/api/replies/drafts/${encodeURIComponent(id)}/moderate`, {method: 'POST', body: {action: 'approve'}}); toast('Ответ одобрен'); });
  }
  async function regenerate(id) {
    await withBusy(id, async () => {
      const it = item(id);
      if (it && !it.answer) await api('/api/replies/draft', {method: 'POST', body: (await api(`/api/inbox?search=${encodeURIComponent(id)}&limit=1`))[0]});
      else await api(`/api/replies/drafts/${encodeURIComponent(id)}/regenerate`, {method: 'POST'});
      toast('Ответ составлен заново');
    });
  }
  async function sendOne(id, text) {
    const it = item(id);
    const body = (text ?? it.answer ?? '').trim();
    if (body.length < 2) throw new Error('Пустой ответ');
    await api(`/api/replies/check/${encodeURIComponent(id)}`, {method: 'POST', body: {text: body}}).then(r => { if (!r.ok) throw new Error('Замечания: ' + r.warnings.join('; ')); });
    if (text != null && text.trim() !== it.answer) await api(`/api/replies/drafts/${encodeURIComponent(id)}`, {method: 'PUT', body: {text: body}});
    await api(`/api/replies/drafts/${encodeURIComponent(id)}/moderate`, {method: 'POST', body: {action: 'approve'}});
    await api(`/api/replies/send/${encodeURIComponent(id)}`, {method: 'POST', body: {text: body}});
  }
  async function send(id, text) {
    if (st.busy.has(id)) return;
    keepText(id, text);
    await withBusy(id, async () => { await sendOne(id, text); st.editing = null; toast('Ответ отправлен'); });
    drawCounters();
  }

  // ---------------------------------------------------------------- групповые действия
  function drawBulk() {
    const bar = root.querySelector('[data-bulk]'), n = st.selected.size;
    bar.hidden = !n;
    if (!n) return;
    const canSend = st.meta?.publishing?.enabled;
    bar.innerHTML = `<span><b>Выбрано: ${n}</b></span>
      ${canSend ? '<button type="button" class="btn" data-bulk-send>Отправить выбранные</button>' : ''}
      <button type="button" class="btn secondary" data-bulk-approve>Одобрить</button>
      <button type="button" class="btn secondary" data-bulk-regen>Составить заново</button>
      <button type="button" class="rv-link" data-bulk-clear>Снять выбор</button><span class="muted" data-bulk-state role="status"></span>`;
    bar.querySelector('[data-bulk-clear]').onclick = () => { st.selected.clear(); drawList(); drawBulk(); };
    bar.querySelector('[data-bulk-approve]').onclick = () => groupAction('confirm', 'Одобрено');
    bar.querySelector('[data-bulk-regen]').onclick = () => groupAction('regenerate', 'Ответы составлены заново');
    const sendBtn = bar.querySelector('[data-bulk-send]');
    if (sendBtn) sendBtn.onclick = bulkSend;
  }
  async function groupAction(action, done) {
    const ids = [...st.selected];
    try { await api('/api/replies/group-action', {method: 'POST', body: {item_ids: ids, action}}); toast(`${done}: ${ids.length}`); }
    catch (e) { toast(e.message); }
    load();
  }
  async function bulkSend() {
    const ids = [...st.selected].filter(id => { const i = item(id); return !i || i.status !== 'published'; });
    const bar = root.querySelector('[data-bulk]'), state = bar.querySelector('[data-bulk-state]');
    if (!ids.length) { toast('Среди выбранных нет неотправленных ответов'); return; }
    if (bar.dataset.armed !== '1') {
      bar.dataset.armed = '1';
      bar.querySelector('[data-bulk-send]').textContent = `Подтвердить отправку ${ids.length}`;
      state.textContent = 'Ответы уйдут покупателям в том виде, как показаны.';
      return;
    }
    bar.dataset.armed = '';
    bar.querySelectorAll('button').forEach(b => b.disabled = true);
    let ok = 0; const failed = [];
    for (const [n, id] of ids.entries()) {
      state.textContent = `Отправка ${n + 1} из ${ids.length}…`;
      try {
        if (!item(id)) await refreshItem(id);
        await sendOne(id, null); ok++; st.selected.delete(id);
      } catch (e) { failed.push(e.message); }
      await new Promise(r => setTimeout(r, 1100));      // бережно к лимитам API маркетплейса
    }
    toast(`Отправлено: ${ok}${failed.length ? `. Не отправлено: ${failed.length} (${failed[0]})` : ''}`);
    load();
  }

  const style = document.createElement('style');
  style.textContent = `
.rv-page{overflow:visible!important;--rv-line:#e7e9ee;--rv-ink:#1f2328;--rv-muted:#6b7280;background:#f6f7fb!important;border:0!important;box-shadow:none!important;padding:4px 0 0!important}
.rv-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}.rv-head h2{font-size:22px;font-weight:600;margin:0 0 8px}
.rv-head-actions{display:flex;gap:8px;align-items:center}.rv-dark{height:36px;padding:0 14px;border:0;border-radius:8px;background:#1f2328;color:#fff;font:inherit;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
.rv-dark:hover{background:#000}.rv-dark:disabled{opacity:.6;cursor:wait}.rv-dark.on{background:#15803d}.rv-sq{width:36px;padding:0;font-size:16px}
.rv-menu-wrap,.rv-pop-wrap{position:relative}.rv-menu{position:absolute;right:0;top:42px;z-index:30;background:#fff;border:1px solid var(--rv-line);border-radius:10px;box-shadow:0 10px 30px #0000001f;min-width:170px;padding:6px}
.rv-menu a{display:block;padding:9px 12px;border-radius:7px;cursor:pointer;font-size:14px;color:var(--rv-ink)}.rv-menu a:hover{background:#f3f4f6}
.rv-counters{display:flex;flex-wrap:wrap;gap:6px}.rv-count{border:1px solid var(--rv-line);background:#fff;border-radius:999px;padding:4px 11px;font:inherit;font-size:13px;color:#374151;cursor:pointer;text-decoration:none}
.rv-count b{font-weight:700;margin-left:3px}.rv-count.on{background:#1f2328;color:#fff;border-color:#1f2328}.rv-complaints{border-color:#fecaca;color:#b91c1c}
.rv-banner{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:10px;padding:10px 14px;font-size:13px;margin-bottom:12px}
.rv-filters{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:16px}.rv-search{grid-column:span 2;position:relative}
.rv-search input,.rv-select select,.rv-range,.rv-tagbox{width:100%;height:42px;border:1px solid #dfe2e8;border-radius:9px;background:#fff;font:inherit;font-size:14px;color:var(--rv-ink)}
.rv-search input{padding:0 40px 0 14px}.rv-search span{position:absolute;right:14px;top:9px;font-size:18px;color:#6b7280}.rv-search input::placeholder{color:#9aa0a8}
.rv-select{position:relative;display:block}.rv-select select{appearance:none;padding:0 54px 0 14px;color:#6b7280;cursor:pointer;background:#fff url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%236b7280' stroke-width='1.6' fill='none'/%3E%3C/svg%3E") no-repeat right 14px center}
.rv-select.set select,.rv-select select:focus{color:var(--rv-ink)}.rv-select.set select{border-color:#c9cdd6;font-weight:500}
.rv-x{position:absolute;right:32px;top:9px;width:24px;height:24px;border:0;background:transparent;font-size:18px;color:#6b7280;cursor:pointer;border-radius:6px;line-height:1}.rv-x:hover{background:#f3f4f6;color:#111}
.rv-range{cursor:pointer;font-weight:600;color:#374151}.rv-range.set{font-weight:600;color:var(--rv-ink);border-color:#c9cdd6}
.rv-pop{position:absolute;top:48px;left:0;right:0;z-index:30;background:#fff;border:1px solid var(--rv-line);border-radius:12px;box-shadow:0 14px 40px #00000026;padding:12px}
.rv-presets{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}.rv-presets button,.rv-tagopts button{border:1px solid var(--rv-line);background:#fff;border-radius:8px;padding:6px 10px;font:inherit;font-size:13px;cursor:pointer}
.rv-presets button:hover,.rv-tagopts button:hover{background:#f3f4f6}.rv-tagopts button.on{background:#1f2328;color:#fff;border-color:#1f2328}
.rv-range-inputs{display:flex;gap:10px}.rv-range-inputs label{flex:1;display:grid;gap:4px;font-size:12px;color:var(--rv-muted)}.rv-range-inputs input{height:38px;border:1px solid #dfe2e8;border-radius:8px;padding:0 8px;font:inherit}
.rv-pop-actions{display:flex;gap:8px;margin-top:12px}.rv-pop-actions .btn{height:36px;padding:0 14px}
.rv-tagbox{display:flex;flex-wrap:wrap;align-items:center;gap:6px;padding:4px 54px 4px 8px;height:auto;min-height:42px;position:relative;cursor:pointer}
.rv-tagbtn{border:0;background:transparent;font:inherit;padding:0;position:absolute;inset:0;cursor:pointer;text-align:left}.rv-ph{position:absolute;left:14px;top:10px;color:#6b7280}
.rv-chip{position:relative;z-index:1;display:inline-flex;align-items:center;gap:4px;background:#f3f4f6;border:1px solid #e5e7eb;border-radius:7px;padding:3px 4px 3px 8px;font-size:13px;font-weight:500}
.rv-chip button{border:0;background:transparent;cursor:pointer;color:#6b7280;font-size:15px;line-height:1;padding:0 3px}.rv-tagbox .rv-x{z-index:1}.rv-chev{position:absolute;right:14px;top:17px;width:12px;height:8px;font-size:0;background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%236b7280' stroke-width='1.6' fill='none'/%3E%3C/svg%3E") no-repeat}
.rv-tag-pop{left:auto;width:min(500px,92vw);max-height:420px;overflow:auto}.rv-tag-pop p{font-size:12px;margin:0 0 8px}.rv-tagrow{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:6px 0;border-top:1px solid #f1f2f5;font-size:14px}.rv-tagopts{display:flex;gap:4px;flex:none}.rv-tagopts button{white-space:nowrap}
.rv-found{grid-column:1/-1;font-size:13px;color:var(--rv-muted);display:flex;gap:12px;align-items:center}.rv-link{border:0;background:none;padding:0;font:inherit;color:#5b21b6;cursor:pointer;text-decoration:underline}
.rv-list{transition:opacity .15s}.rv-list.rv-loading{opacity:.55;pointer-events:none}
.rv-row{display:grid;grid-template-columns:40px 92px minmax(0,1.05fr) minmax(0,1.15fr) minmax(0,1.15fr);gap:10px;margin-bottom:10px;align-items:stretch}
.rv-th{margin:0 0 6px;align-items:center;font-size:14px;font-weight:600;color:#4b5563;text-align:center}.rv-th .rv-cb{background:transparent;border:0}
.rv-sort{border:0;background:none;font:inherit;font-weight:600;color:#4b5563;cursor:pointer}.rv-sort:hover{color:#111}
.rv-cell{background:#fff;border:1px solid var(--rv-line);border-radius:10px;padding:12px;min-width:0}.rv-row.sel .rv-cell{border-color:#a78bfa;background:#fdfcff}
.rv-cb{display:flex;align-items:center;justify-content:center;padding:0}.rv-cb input{width:18px;height:18px;accent-color:#1f2328;cursor:pointer}
.rv-date{display:flex;align-items:center;justify-content:center;text-align:center;font-size:12px;color:#4b5563;line-height:1.6}
.rv-product{display:flex;gap:12px}.rv-photo{position:relative;flex:none;width:76px;height:100px;border-radius:8px;background:#f3f4f6;display:grid;place-items:center;overflow:hidden;font-size:26px}
.rv-photo img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}.rv-pinfo{min-width:0;display:flex;flex-direction:column;gap:6px;flex:1}
.rv-badges{display:flex;flex-wrap:wrap;gap:6px}.rv-mp{display:inline-block;padding:2px 9px;border-radius:7px;font-size:12px;font-weight:600;border:1px solid}
.rv-mp.wb{background:#fbe8f6;border-color:#eba6d8;color:#b0127f}.rv-mp.ozon{background:#e6f0fd;border-color:#a9c6f0;color:#1d5fc4}.rv-mp.ym{background:#fdecea;border-color:#f0b2a9;color:#c8372a}
.rv-cab{display:inline-block;padding:2px 9px;border-radius:7px;font-size:12px;font-weight:600;border:1px solid #e5e7eb;color:#374151;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rv-pname{font-size:13px;font-weight:600;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.rv-art{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:12px;color:#4b5563;margin-top:auto}.rv-art span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rv-art a{flex:none;text-decoration:none;color:#374151;border:1px solid #e5e7eb;border-radius:6px;width:24px;height:24px;display:grid;place-items:center;font-size:13px}.rv-art a:hover{background:#f3f4f6}
.rv-review,.rv-answer{font-size:13px;line-height:1.45;display:flex;flex-direction:column;gap:5px}
.rv-icons{display:flex;flex-wrap:wrap;gap:5px;align-items:center}.rv-tag{width:24px;height:24px;border-radius:6px;display:inline-grid;place-items:center;font-size:13px;border:1px solid}
.rv-tag.positive{background:#effaf3;border-color:#bbe5c8}.rv-tag.negative{background:#fdf0ef;border-color:#f3c0b9}.rv-tag.media{background:#f5f6f8;border-color:#e5e7eb}
.rv-order{margin-left:auto;font-size:11px;font-weight:600;padding:2px 7px;border-radius:6px;background:#f3f4f6;color:#4b5563;white-space:nowrap}.rv-order.rejected,.rv-order.returned{background:#fff4e5;color:#b45309}.rv-order.buyout{background:#ecfdf3;color:#15803d}
.rv-stars{color:#f5b301;font-size:15px;letter-spacing:1px}.rv-stars i{font-style:normal;color:#d9dce1}
.rv-line b{font-weight:600}.rv-text{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;white-space:pre-line}.rv-text.open{display:block}
.rv-more{align-self:flex-start;border:0;background:none;padding:0;font:inherit;font-weight:600;color:#1f2328;cursor:pointer}.rv-more:hover{text-decoration:underline}
.rv-ahead{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.rv-method{display:inline-flex;gap:4px;align-items:center;padding:3px 9px;border:1px solid #e5e7eb;border-radius:7px;font-size:12px;font-weight:600}
.rv-status{font-size:11px;font-weight:600;padding:3px 8px;border-radius:6px;background:#f3f4f6;color:#4b5563}.rv-status.ok{background:#ecfdf3;color:#15803d}.rv-status.info{background:#eef4ff;color:#1d4ed8}
.rv-status.warn{background:#fff4e5;color:#b45309}.rv-status.bad{background:#fef2f2;color:#be123c}.rv-status.muted{color:#9ca3af}
.rv-warn{color:#d97706;font-size:16px;cursor:help}.rv-icon{margin-left:auto;width:30px;height:30px;border:1px solid #e5e7eb;border-radius:7px;background:#fff;cursor:pointer;color:#374151}.rv-icon:hover{background:#f3f4f6}
.rv-atext{display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden;white-space:pre-line;color:#374151}.rv-atext.open{display:block}
.rv-answer textarea{width:100%;min-width:0;min-height:120px;border:1px solid #c9cdd6;border-radius:8px;padding:9px;font:inherit;font-size:13px;line-height:1.5;resize:vertical}
.rv-answer textarea:focus{outline:3px solid #ede9fe;border-color:#8b5cf6}.rv-counter{font-size:11px;color:#9ca3af;text-align:right}
.rv-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:auto;padding-top:4px}.rv-actions .btn{height:32px;padding:0 12px;font-size:12px}
.rv-send{background:#1f2328!important;box-shadow:none!important}.rv-send:disabled{opacity:.45;cursor:not-allowed}
.rv-warnbox{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:8px;padding:6px 9px;font-size:12px}.rv-note{font-size:12px;color:#b45309}.rv-sent{font-size:12px}
.rv-empty{background:#fff;border:1px dashed #d8dbe2;border-radius:12px;padding:40px;text-align:center}.rv-error{color:#b91c1c}
.rv-pager{display:flex;align-items:center;justify-content:flex-end;gap:6px;margin-top:14px;font-size:13px;color:#6b7280}.rv-pager select{margin-right:auto;height:36px;border:1px solid #dfe2e8;border-radius:8px;padding:0 8px;font:inherit;background:#fff}
.rv-pager>span{margin-right:8px}.rv-pg{min-width:32px;height:32px;border:1px solid transparent;border-radius:8px;background:transparent;font:inherit;cursor:pointer;color:#374151}.rv-pg:hover:not(:disabled){background:#fff;border-color:#e5e7eb}
.rv-pg.on{background:#fff;border-color:#d1d5db;font-weight:700;color:#111}.rv-pg:disabled{opacity:.35;cursor:default}.rv-gap{padding:0 2px}
.rv-bulk{position:fixed;left:50%;transform:translateX(-50%);bottom:16px;width:max-content;max-width:calc(100vw - 32px);z-index:40;display:flex;flex-wrap:wrap;align-items:center;gap:10px;background:#1f2328;color:#fff;border-radius:12px;padding:10px 14px;box-shadow:0 10px 30px #0000003d;margin-top:10px}
.rv-bulk .btn{height:34px;padding:0 12px;font-size:13px}.rv-bulk .btn.secondary{background:#fff;color:#1f2328;border:0}.rv-bulk .btn:not(.secondary){background:#8b5cf6;box-shadow:none}.rv-bulk .rv-link{color:#d1d5db;font-size:13px}.rv-bulk .muted{font-size:13px}.rv-bulk .muted{color:#cbd5e1}
.rv-bulk[hidden],.rv-pop[hidden],.rv-menu[hidden]{display:none!important}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
@media(max-width:1100px){.rv-row{grid-template-columns:34px minmax(0,1fr) minmax(0,1fr)}.rv-row .rv-date{display:none}.rv-row .rv-product{grid-column:2/-1}.rv-row .rv-review{grid-column:2}.rv-row .rv-answer{grid-column:3}.rv-th>div,.rv-th .rv-sort{display:none}.rv-th{grid-template-columns:34px auto}.rv-th::after{content:'Выбрать все';font-weight:500}}
@media(max-width:760px){.rv-bulk{bottom:8px}.rv-filters{grid-template-columns:1fr}.rv-search{grid-column:auto}.rv-row{grid-template-columns:30px minmax(0,1fr)}.rv-row .rv-product,.rv-row .rv-review,.rv-row .rv-answer{grid-column:2}.rv-head-actions{flex-wrap:wrap}}`;
  document.head.appendChild(style);

  return {render};
})();
