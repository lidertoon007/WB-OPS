/* WB OPS: «Товары → Номенклатура и себестоимость». Цены пишутся в opiu/cost.csv — их же берёт ОПиУ. */
window.WBOPSProducts = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const rub = v => v == null ? '—' : Math.round(v).toLocaleString('ru-RU') + ' ₽';
  const money = v => v == null ? '' : (Math.round(v * 100) / 100).toString().replace('.', ',');
  const num = v => { const t = String(v ?? '').replace(/\s/g, '').replace(',', '.'); return t === '' ? null : Number(t); };
  const monthLabel = d => { if (!d) return 'всегда'; const [y, m] = d.split('-'); return `с ${['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'][+m - 1]} ${y}`; };
  const FILTERS = [['all', 'Все'], ['nocost', 'Без себестоимости'], ['sold', 'Продавались'], ['stock', 'Есть остаток']];
  const st = {data: null, filter: 'all', search: '', brand: '', category: '', mp: '', selected: new Set(), open: null, saved: {}, dirty: false,
              retroOpen: false, retroQ: ''};
  const brandKey = v => String(v ?? '').replace(/\s+/g, ' ').trim().toUpperCase();
  const pctText = v => String(Math.round(v * 100) / 100).replace('.', ',') + '%';
  let root = null, timer = null;

  async function api(url, options = {}) {
    if (options.body && typeof options.body !== 'string' && !(options.body instanceof FormData)) options = {...options, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(options.body)};
    const r = await fetch(url, options);
    let d = null;
    try { d = await r.json(); } catch (e) { /* пусто */ }
    if (!r.ok) throw new Error((d && (typeof d.detail === 'string' ? d.detail : 'Проверьте значения')) || `Ошибка ${r.status}`);
    return d;
  }
  function toast(text) {
    const t = document.getElementById('toast');
    if (!t) return;
    t.textContent = text; t.style.display = 'block';
    clearTimeout(toast.timer); toast.timer = setTimeout(() => t.style.display = 'none', 4000);
  }

  async function render(section) {
    root = section;
    root.className = 'panel rv-page pr-page';
    root.innerHTML = `<div class="rv-head"><div class="rv-counters" data-counters></div>
        <div class="rv-head-actions"><label class="rv-dark pr-import" role="button" tabindex="0" title="Excel или CSV: колонки «Артикул», «Себестоимость», по желанию «Подготовка» (или «ФФ»), «Бренд», «Категория», «Наименование». Один справочник на все кабинеты и юрлица — новые кабинеты берут себестоимость отсюда же">Загрузить себестоимость<input type="file" accept=".xlsx,.xlsm,.csv" data-import hidden></label>
          <button type="button" class="rv-dark" data-refresh title="Загрузить названия, фото и WB ID из карточек товаров Wildberries">Обновить из WB</button>
          <button type="button" class="rv-dark" data-fill-missing title="Установить 1000 ₽ только товарам без себестоимости">Заполнить отсутствующую себестоимость</button>
          <button type="button" class="rv-dark" data-fill-prep title="Установить подготовку 85 ₽ всем артикулам">Подготовка 85 ₽ всем</button>
          <div class="rv-menu-wrap"><button type="button" class="rv-dark rv-sq" data-export aria-label="Скачать" title="Скачать номенклатуру">⇩</button>
          <div class="rv-menu" role="menu" hidden><a role="menuitem" href="/api/nomenclature/export?format=xlsx">Excel (.xlsx)</a><a role="menuitem" href="/api/nomenclature/export?format=csv">CSV</a></div></div></div></div>
      <div data-banner></div>
      <div data-retro></div>
      <div class="pr-filters"><label class="rv-search"><span class="sr">Поиск</span><input type="search" data-search placeholder="Поиск по артикулу, названию или WB ID…"><span aria-hidden="true">⌕</span></label>
        <label class="rv-select"><span class="sr">Бренд</span><select data-f="brand"></select></label>
        <label class="rv-select"><span class="sr">Категория</span><select data-f="category"></select></label>
        <label class="rv-select"><span class="sr">Маркетплейс</span><select data-f="mp"></select></label></div>
      <div data-list><p class="muted">Загрузка…</p></div>
      <div class="rv-bulk pr-bulk" data-bulk hidden></div>`;
    root.querySelector('[data-search]').oninput = e => { clearTimeout(timer); timer = setTimeout(() => { st.search = e.target.value.trim().toLowerCase(); draw(); }, 200); };
    root.querySelectorAll('[data-f]').forEach(s => s.onchange = () => { st[s.dataset.f] = s.value; draw(); });
    root.querySelector('[data-refresh]').onclick = refreshCards;
    root.querySelector('[data-fill-missing]').onclick = fillMissingCost;
    root.querySelector('[data-fill-prep]').onclick = fillPrep;
    root.querySelector('[data-import]').onchange = e => importCosts(e.target);
    root.querySelector('.pr-import').onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); root.querySelector('[data-import]').click(); } };
    const menu = root.querySelector('.rv-menu');
    root.querySelector('[data-export]').onclick = e => { e.stopPropagation(); menu.hidden = !menu.hidden; };
    document.addEventListener('click', e => { if (root?.isConnected && !e.target.closest('.rv-menu-wrap')) menu.hidden = true; });
    await load();
  }

  async function load() {
    try { st.data = await api('/api/nomenclature'); }
    catch (e) { root.querySelector('[data-list]').innerHTML = `<p class="rv-error">${esc(e.message)}</p>`; return; }
    fillSelects(); draw();
  }

  function fillSelects() {
    const items = st.data.items;
    const uniq = key => [...new Set(items.flatMap(i => Array.isArray(i[key]) ? i[key] : [i[key]]).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'ru'));
    const set = (name, label, values, cur) => root.querySelector(`[data-f="${name}"]`).innerHTML =
      `<option value="">${label}</option>` + values.map(v => `<option ${v === cur ? 'selected' : ''}>${esc(v)}</option>`).join('');
    set('brand', 'Все бренды', uniq('brand'), st.brand);
    set('category', 'Все категории', uniq('category'), st.category);
    set('mp', 'Все маркетплейсы', uniq('marketplaces'), st.mp);
  }

  function visible() {
    return st.data.items.filter(i =>
      (st.filter !== 'nocost' || !i.has_cost) && (st.filter !== 'sold' || i.sold > 0) && (st.filter !== 'stock' || i.stock > 0) &&
      (!st.brand || i.brand === st.brand) && (!st.category || i.category === st.category) && (!st.mp || i.marketplaces.includes(st.mp)) &&
      (!st.search || [i.article, i.name, i.brand, ...i.nm_ids].join(' ').toLowerCase().replace(/ё/g, 'е').includes(st.search.replace(/ё/g, 'е'))));
  }

  function draw() {
    const items = st.data.items;
    const counts = {all: items.length, nocost: items.filter(i => !i.has_cost).length, sold: items.filter(i => i.sold > 0).length, stock: items.filter(i => i.stock > 0).length};
    root.querySelector('[data-counters]').innerHTML = FILTERS.map(([k, t]) =>
      `<button type="button" class="rv-count ${st.filter === k ? 'on' : ''} ${k === 'nocost' && counts.nocost ? 'pr-warn' : ''}" data-filter="${k}">${t} <b>${counts[k]}</b></button>`).join('');
    root.querySelectorAll('[data-filter]').forEach(b => b.onclick = () => { st.filter = b.dataset.filter; draw(); });
    const d = st.data;
    root.querySelector('[data-banner]').innerHTML = `
      ${st.dirty ? `<div class="pr-rebuild">Себестоимость или ретро-бонусы изменены. Чтобы «Финансы» учли новые значения, пересоберите ОПиУ — из кэша это быстро и без запросов к маркетплейсам.
        <button type="button" class="btn" data-rebuild ${d.finance === 'running' ? 'disabled' : ''}>${d.finance === 'running' ? 'Пересобирается…' : 'Пересобрать ОПиУ'}</button></div>` : ''}
      <p class="muted pr-sources">Источники: карточки WB — ${d.cards_at ? `обновлены ${esc(d.cards_at)}` : '<b>не загружены</b> (нажмите «Обновить из WB», чтобы появились названия и фото)'};
        продажи — ${d.report ? esc(d.report) : 'отчёт ОПиУ ещё не собран'}; остатки и цены WB — ${d.goods_at ? esc(d.goods_at) : 'нет данных'}.
        Себестоимость и подготовка хранятся в <code>opiu/cost.csv</code> — их же использует расчёт ОПиУ.</p>
      ${(d.cards_errors || []).length ? `<div class="rv-banner">Не все кабинеты отдали карточки: ${esc(d.cards_errors.join('; '))}</div>` : ''}`;
    root.querySelector('[data-rebuild]')?.addEventListener('click', rebuild);
    drawRetro();
    const list = visible();
    const box = root.querySelector('[data-list]');
    if (!list.length) { box.innerHTML = `<div class="rv-empty"><b>Ничего не найдено</b><p class="muted">Измените фильтры или поиск.</p></div>`; drawBulk(); return; }
    const allSel = list.every(i => st.selected.has(i.key));
    box.innerHTML = `<div class="pr-table" role="table">
      <div class="pr-row pr-th" role="row"><label class="rv-cb"><input type="checkbox" data-all ${allSel ? 'checked' : ''} aria-label="Выбрать все"></label>
        <div>Товар</div><div class="r">Продано</div><div class="r">Остаток WB</div><div class="r">Цена WB</div><div class="r">Себестоимость, ₽</div><div class="r">Подготовка, ₽</div><div class="r">Итого на ед.</div><div>Действует</div></div>
      ${list.map(rowHtml).join('')}</div>`;
    box.querySelector('[data-all]').onchange = e => { list.forEach(i => e.target.checked ? st.selected.add(i.key) : st.selected.delete(i.key)); draw(); };
    list.forEach(bindRow);
    drawBulk();
  }

  function rowHtml(i) {
    const total = (i.cost ?? 0) + (i.prep ?? 0);
    const saved = st.saved[i.key];
    const open = st.open === i.key;
    const wb = i.nm_ids[0] ? `https://www.wildberries.ru/catalog/${i.nm_ids[0]}/detail.aspx` : '';
    return `<div class="pr-row ${i.has_cost ? '' : 'nocost'} ${st.selected.has(i.key) ? 'sel' : ''}" data-key="${esc(i.key)}" role="row">
      <label class="rv-cb"><input type="checkbox" data-sel ${st.selected.has(i.key) ? 'checked' : ''} aria-label="Выбрать ${esc(i.article)}"></label>
      <div class="pr-prod"><div class="pr-photo">${i.photo ? `<img loading="lazy" src="${esc(i.photo)}" alt="" onerror="this.remove()">` : ''}<span aria-hidden="true">👖</span></div>
        <div class="pr-pinfo"><b>${esc(i.article)}</b>${i.name ? `<span class="pr-name">${esc(i.name)}</span>` : ''}
          <span class="muted pr-meta">${[i.brand, i.category, i.nm_ids.length ? `WB ${i.nm_ids.slice(0, 3).join(', ')}${i.nm_ids.length > 3 ? '…' : ''}` : ''].filter(Boolean).map(esc).join(' · ')}
          ${wb ? `<a href="${wb}" target="_blank" rel="noopener" aria-label="Открыть на Wildberries">↗</a>` : ''}
          ${i.model_key ? `<a href="/products?model=${encodeURIComponent(i.model_key)}">Характеристики</a>` : ''}</span></div></div>
      <div class="r"><b>${i.sold ? i.sold.toLocaleString('ru-RU') : '—'}</b>${i.revenue ? `<span class="muted pr-sub">${rub(i.revenue)}</span>` : ''}</div>
      <div class="r">${i.stock != null ? Math.round(i.stock).toLocaleString('ru-RU') : '—'}</div>
      <div class="r">${i.price ? rub(i.price) : '—'}</div>
      <div class="r"><input class="pr-in" data-cost inputmode="decimal" value="${esc(money(i.cost))}" placeholder="не задана" aria-label="Себестоимость ${esc(i.article)}"></div>
      <div class="r"><input class="pr-in" data-prep inputmode="decimal" value="${esc(money(i.prep))}" placeholder="0" aria-label="Подготовка ${esc(i.article)}"></div>
      <div class="r"><b>${i.has_cost ? rub(total) : '—'}</b>${i.multiplier && i.multiplier !== 1 ? `<span class="muted pr-sub">кратность ${i.multiplier}</span>` : ''}</div>
      <div class="pr-when"><button type="button" class="pr-link" data-open>${i.has_cost ? monthLabel(i.effective_from) : 'задать'}${i.future.length ? ` <span class="pr-future" title="Запланирована новая цена">+${i.future.length}</span>` : ''}</button>
        <span class="pr-saved" data-state>${saved ? esc(saved) : ''}</span></div>
      ${open ? `<div class="pr-detail">${detailHtml(i)}</div>` : ''}</div>`;
  }

  function detailHtml(i) {
    const month = new Date(); month.setMonth(month.getMonth() + 1);
    const next = month.toISOString().slice(0, 7);
    return `<div class="pr-hist"><b>История цен</b>${i.history.length ? `<table><thead><tr><th>Действует</th><th>Себестоимость</th><th>Подготовка</th><th>Кратность</th><th></th></tr></thead><tbody>
        ${i.history.map(h => `<tr><td>${h.from ? esc(monthLabel(h.from)) : 'базовая (всегда)'}</td><td>${rub(h.cost)}</td><td>${rub(h.prep)}</td><td>${h.multiplier ?? 1}</td>
          <td>${h.from ? `<button type="button" class="pr-link danger" data-del="${esc(h.from)}">удалить</button>` : ''}</td></tr>`).join('')}</tbody></table>` : '<p class="muted">Цены ещё не задавались.</p>'}</div>
      <form class="pr-new" data-new><b>Новая цена с месяца</b>
        <p class="muted">Прошлые месяцы в ОПиУ останутся по старой цене, с выбранного месяца — по новой.</p>
        <div class="pr-new-grid"><label>С месяца<input type="month" name="from" value="${next}" required></label>
          <label>Себестоимость, ₽<input name="cost" inputmode="decimal" value="${esc(money(i.cost))}"></label>
          <label>Подготовка, ₽<input name="prep" inputmode="decimal" value="${esc(money(i.prep))}"></label>
          <label>Кратность<input name="multiplier" inputmode="decimal" value="${esc(i.multiplier ?? 1)}"></label></div>
        <div class="rv-actions"><button class="btn" type="submit">Добавить цену</button><button type="button" class="btn secondary" data-close>Закрыть</button></div></form>`;
  }

  function bindRow(i) {
    const row = root.querySelector(`.pr-row[data-key="${CSS.escape(i.key)}"]`);
    if (!row) return;
    row.querySelector('[data-sel]').onchange = e => { e.target.checked ? st.selected.add(i.key) : st.selected.delete(i.key); row.classList.toggle('sel', e.target.checked); drawBulk(); };
    row.querySelector('[data-open]').onclick = () => { st.open = st.open === i.key ? null : i.key; draw(); };
    for (const field of ['cost', 'prep']) {
      const input = row.querySelector(`[data-${field}]`);
      const commit = () => {
        const value = num(input.value);
        const before = i[field];
        if (input.value.trim() === '' && before == null) return;
        if (value === before) return;
        if (input.value.trim() !== '' && (value == null || isNaN(value) || value < 0)) { input.classList.add('bad'); toast('Введите число, например 1050 или 85,50'); return; }
        input.classList.remove('bad');
        save(i, {[field]: value ?? 0});
      };
      input.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); input.blur(); } if (e.key === 'Escape') { input.value = money(i[field]); input.blur(); } };
      input.onchange = commit;
    }
    row.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
      if (b.dataset.armed !== '1') { b.dataset.armed = '1'; b.textContent = 'точно удалить?'; return; }
      try { await api(`/api/nomenclature/price?article=${encodeURIComponent(i.article)}&from=${encodeURIComponent(b.dataset.del)}`, {method: 'DELETE'}); st.dirty = true; toast('Цена удалена'); await load(); }
      catch (e) { toast(e.message); }
    });
    const form = row.querySelector('[data-new]');
    if (form) {
      form.querySelector('[data-close]').onclick = () => { st.open = null; draw(); };
      form.onsubmit = async e => {
        e.preventDefault();
        const body = {article: i.article, effective_from: form.from.value, cost: form.cost.value, prep: form.prep.value, multiplier: form.multiplier.value};
        try { await api('/api/nomenclature/price', {method: 'PUT', body}); st.dirty = true; toast(`Цена ${monthLabel(form.from.value + '-01')} сохранена`); await load(); }
        catch (err) { toast(err.message); }
      };
    }
  }

  async function save(i, values) {
    const row = root.querySelector(`.pr-row[data-key="${CSS.escape(i.key)}"]`);
    const state = row?.querySelector('[data-state]');
    if (state) state.textContent = 'сохраняю…';
    try {
      await api('/api/nomenclature/price', {method: 'PUT', body: {article: i.article, ...values}});
      Object.assign(i, values, {has_cost: values.cost != null ? true : i.has_cost});
      st.saved[i.key] = '✓ сохранено'; st.dirty = true;
      await load();
      setTimeout(() => { delete st.saved[i.key]; const s = root.querySelector(`.pr-row[data-key="${CSS.escape(i.key)}"] [data-state]`); if (s) s.textContent = ''; }, 2500);
    } catch (e) { if (state) state.textContent = ''; toast(e.message); }
  }

  async function fillMissingCost() {
    const count = st.data?.items?.filter(i => !i.has_cost).length || 0;
    if (!count) { toast('Артикулов без себестоимости нет'); return; }
    const button = root.querySelector('[data-fill-missing]');
    button.disabled = true;
    try {
      const result = await api('/api/nomenclature/fill-missing-cost', {method: 'POST'});
      st.dirty = st.dirty || result.updated > 0;
      toast(`Заполнено артикулов: ${result.updated}`);
      await load();
    } catch (e) { toast(e.message); }
    finally { if (button) button.disabled = false; }
  }

  async function fillPrep() {
    const articles = st.data?.items?.map(i => i.article).filter(Boolean) || [];
    if (!articles.length) { toast('Артикулы не найдены'); return; }
    const button = root.querySelector('[data-fill-prep]');
    button.disabled = true;
    try {
      const result = await api('/api/nomenclature/set-prep-all', {method: 'POST'});
      st.dirty = st.dirty || result.updated > 0;
      toast(`Подготовка 85 ₽ установлена: ${result.updated}`);
      await load();
    } catch (e) { toast(e.message); }
    finally { if (button) button.disabled = false; }
  }

  function drawBulk() {
    const bar = root.querySelector('[data-bulk]'), n = st.selected.size;
    bar.hidden = !n;
    if (!n) return;
    bar.innerHTML = `<b>Выбрано: ${n}</b>
      <label>Себестоимость<input data-b="cost" inputmode="decimal" placeholder="не менять"></label>
      <label>Подготовка<input data-b="prep" inputmode="decimal" placeholder="не менять"></label>
      <label>С месяца<input data-b="effective_from" type="month" title="Пусто — изменить действующую цену"></label>
      <button type="button" class="btn" data-apply>Применить</button><button type="button" class="rv-link" data-clear>Снять выбор</button>`;
    bar.querySelector('[data-clear]').onclick = () => { st.selected.clear(); draw(); };
    bar.querySelector('[data-apply]').onclick = async () => {
      const body = {articles: st.data.items.filter(i => st.selected.has(i.key)).map(i => i.article)};
      bar.querySelectorAll('[data-b]').forEach(x => { if (x.value.trim()) body[x.dataset.b] = x.value.trim(); });
      if (!body.cost && !body.prep) { toast('Укажите себестоимость и/или подготовку'); return; }
      try { const r = await api('/api/nomenclature/bulk', {method: 'POST', body}); toast(`Обновлено товаров: ${r.updated}`); st.dirty = true; st.selected.clear(); await load(); }
      catch (e) { toast(e.message); }
    };
  }

  // Ретро-бонусы производителей: % от себестоимости проданного по бренду (opiu/retro_bonus.csv)
  function retroBrands() {
    const rows = st.data.retro || [], today = new Date().toISOString().slice(0, 7), out = new Map();
    for (const i of st.data.items) {
      if (!i.brand) continue;
      const k = brandKey(i.brand);
      if (!out.has(k)) out.set(k, {brand: i.brand, count: 0, rows: []});
      out.get(k).count++;
    }
    for (const r of rows) {
      const k = brandKey(r.brand);
      if (!out.has(k)) out.set(k, {brand: r.brand, count: 0, rows: []});
      out.get(k).rows.push(r);
    }
    for (const b of out.values()) {
      const cur = b.rows.filter(r => !r.since || r.since.slice(0, 7) <= today);
      b.current = cur.length ? cur[cur.length - 1].pct : null;
      b.future = b.rows.filter(r => r.since && r.since.slice(0, 7) > today);
    }
    return [...out.values()].sort((a, b) => (b.current > 0) - (a.current > 0) || b.count - a.count || a.brand.localeCompare(b.brand, 'ru'));
  }

  function drawRetro() {
    const box = root.querySelector('[data-retro]'), brands = retroBrands(), active = brands.filter(b => b.current > 0);
    const q = st.retroQ.toLowerCase(), shown = brands.filter(b => !q || b.brand.toLowerCase().includes(q));
    box.innerHTML = `<div class="pr-retro">
      <button type="button" class="pr-retro-head" data-retro-toggle aria-expanded="${st.retroOpen}">
        <b>Ретро-бонусы производителей</b><span class="muted">${active.length ? active.map(b => `${esc(b.brand)} ${pctText(b.current)}`).slice(0, 4).join(' · ') + (active.length > 4 ? ` и ещё ${active.length - 4}` : '') : 'не заданы'}</span><i aria-hidden="true">${st.retroOpen ? '▴' : '▾'}</i></button>
      ${st.retroOpen ? `<div class="pr-retro-body">
        <p class="muted">Процент от себестоимости проданного, который производитель возвращает за бренд. В ОПиУ, дашборде и РНП — отдельной строкой дохода «Ретро-бонусы производителей»; себестоимость не меняется. «С месяца» — новый процент с этой даты (пусто — изменить действующий).</p>
        ${brands.length > 8 ? `<input type="search" class="pr-retro-q" data-retro-q placeholder="Найти бренд…" value="${esc(st.retroQ)}">` : ''}
        <div class="pr-retro-table" role="table"><div class="pr-retro-row pr-th" role="row"><div>Бренд</div><div class="r">Товаров</div><div class="r">Ретро-бонус, %</div><div>С месяца</div><div>История</div></div>
        ${shown.map(b => `<div class="pr-retro-row" role="row" data-brand="${esc(b.brand)}"><div><b>${esc(b.brand)}</b></div><div class="r muted">${b.count || '—'}</div>
          <div><input class="pr-in" data-retro-pct inputmode="decimal" value="${b.current != null ? String(b.current).replace('.', ',') : ''}" placeholder="0" aria-label="Ретро-бонус ${esc(b.brand)}, %"></div>
          <div><input class="pr-in pr-month" data-retro-from type="month" aria-label="С месяца"></div>
          <div class="muted pr-retro-hist">${b.rows.map(r => `${pctText(r.pct)} ${monthLabel(r.since)}${r.since && r.since.slice(0, 7) > new Date().toISOString().slice(0, 7) ? ' <span class="pr-future">будет</span>' : ''}`).join('; ') || '—'}</div></div>`).join('')
          || '<p class="muted pr-retro-empty">Бренды не найдены. Бренды берутся из карточек WB и отчёта ОПиУ.</p>'}</div></div>` : ''}</div>`;
    box.querySelector('[data-retro-toggle]').onclick = () => { st.retroOpen = !st.retroOpen; drawRetro(); };
    const qi = box.querySelector('[data-retro-q]');
    if (qi) qi.oninput = () => { st.retroQ = qi.value; const pos = qi.selectionStart; drawRetro(); const n = root.querySelector('[data-retro-q]'); n.focus(); n.setSelectionRange(pos, pos); };
    box.querySelectorAll('[data-brand]').forEach(row => {
      const input = row.querySelector('[data-retro-pct]');
      const save = async () => {
        const raw = input.value.trim(), pct = raw === '' ? null : num(raw), from = row.querySelector('[data-retro-from]').value;
        if (raw !== '' && !(pct >= 0 && pct <= 100)) { input.classList.add('bad'); toast('Ретро-бонус — процент от 0 до 100'); return; }
        input.classList.remove('bad');
        try {
          await api('/api/nomenclature/retro', {method: 'PUT', body: {brand: row.dataset.brand, pct, effective_from: from ? from + '-01' : null}});
          toast(pct == null ? `Ретро-бонус ${row.dataset.brand} убран` : `Ретро-бонус ${row.dataset.brand}: ${pctText(pct)}${from ? ' ' + monthLabel(from + '-01') : ''}`);
          st.dirty = true; await load();
        } catch (e) { toast(e.message); }
      };
      input.onchange = save;
      input.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); input.blur(); } };
    });
  }

  async function refreshCards(e) {
    const b = e.currentTarget; b.disabled = true; b.textContent = 'Загрузка карточек…';
    try { const r = await api('/api/nomenclature/refresh', {method: 'POST'}); toast(`Карточек загружено: ${r.cards}${r.errors.length ? `. Ошибки: ${r.errors.join('; ')}` : ''}`); await load(); }
    catch (err) { toast(err.message); }
    b.disabled = false; b.textContent = 'Обновить из WB';
  }
  async function importCosts(input) {
    const file = input.files[0];
    if (!file) return;
    const label = input.closest('label'), text = label.firstChild;
    text.textContent = 'Загрузка…';
    try {
      const fd = new FormData(); fd.append('file', file);
      const r = await api('/api/nomenclature/import', {method: 'POST', body: fd});
      const ch = r.changes.slice(0, 5).map(c => c.article).join(', ');
      toast(`Справочник: добавлено ${r.added}, изменено ${r.updated}${ch ? ` (${ch}${r.changes.length > 5 ? '…' : ''})` : ''}, без изменений ${r.unchanged}`
        + (r.skipped.length ? `, пропущено без цены: ${r.skipped.slice(0, 5).join(', ')}` : '') + '. Пересоберите ОПиУ.');
      st.dirty = true; await load();
    } catch (e) { toast(e.message); }
    text.textContent = 'Загрузить себестоимость'; input.value = '';
  }
  async function rebuild() {
    try { await api('/api/finance/update', {method: 'POST', body: {mode: 'cache'}}); toast('ОПиУ пересобирается — прогресс в разделе «Финансы»'); st.dirty = false; st.data.finance = 'running'; draw(); }
    catch (e) { toast(e.message); }
  }

  const style = document.createElement('style');
  style.textContent = `
.pt-tabs{display:flex;gap:4px;background:#eef0f4;padding:4px;border-radius:12px;width:max-content;max-width:100%;overflow-x:auto;margin:0 0 14px}
.pt-tabs a{padding:9px 16px;border-radius:9px;color:#4b5563;text-decoration:none;font-weight:600;font-size:14px;white-space:nowrap}.pt-tabs a.active{background:#fff;color:#1f2328;box-shadow:0 1px 3px #0000001a}
.wb-member .pt-tabs a[data-nav=nomenclature]{display:none}
.pr-filters{display:grid;grid-template-columns:minmax(0,2fr) repeat(3,minmax(0,1fr));gap:10px;margin-bottom:12px}.pr-filters .rv-search{grid-column:auto}
.pr-sources{font-size:12px;margin:0 0 12px}.pr-sources code{font-size:12px}.rv-count.pr-warn:not(.on){border-color:#fecaca;color:#b91c1c}
.pr-rebuild{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;background:#eef4ff;border:1px solid #c7d7fe;color:#1e3a8a;border-radius:10px;padding:10px 14px;font-size:13px;margin-bottom:10px}
.pr-rebuild .btn{height:34px;padding:0 14px;font-size:13px}
.pr-table{background:#fff;border:1px solid #e7e9ee;border-radius:12px;overflow:hidden}
.pr-row{display:grid;grid-template-columns:40px minmax(220px,2.4fr) 90px 90px 90px 120px 110px 100px 110px;gap:10px;align-items:center;padding:10px 12px;border-bottom:1px solid #f0f1f5;font-size:13px}
.pr-row:last-child{border-bottom:0}.pr-row.sel{background:#faf8ff}.pr-row.nocost{background:#fffafa}.pr-th{background:#f8f9fb;font-weight:600;color:#4b5563;font-size:12px;position:sticky;top:0;z-index:2}
.pr-row .r{text-align:right;font-variant-numeric:tabular-nums}.pr-sub{display:block;font-size:11px}
.pr-prod{display:flex;gap:10px;align-items:center;min-width:0}.pr-photo{position:relative;flex:none;width:42px;height:56px;border-radius:6px;background:#f3f4f6;display:grid;place-items:center;overflow:hidden;font-size:18px}
.pr-photo img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}.pr-pinfo{min-width:0;display:flex;flex-direction:column;gap:2px}
.pr-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#374151}.pr-meta{font-size:12px}.pr-meta a{color:#374151;text-decoration:none;margin-left:4px}
.pr-in{width:100%;height:34px;border:1px solid #dfe2e8;border-radius:8px;padding:0 9px;font:inherit;text-align:right;background:#fff}
.pr-in:hover{border-color:#c9cdd6}.pr-in:focus{outline:3px solid #ede9fe;border-color:#8b5cf6}.pr-in.bad{border-color:#ef4444}.nocost .pr-in[data-cost]{border-color:#fca5a5;background:#fff}
.pr-in::placeholder{color:#c0c4cc}.pr-when{display:flex;flex-direction:column;gap:2px}.pr-link{border:0;background:none;padding:0;font:inherit;color:#5b21b6;cursor:pointer;text-align:left}.pr-link:hover{text-decoration:underline}.pr-link.danger{color:#b91c1c}
.pr-future{display:inline-block;background:#eef4ff;color:#1d4ed8;border-radius:5px;padding:0 5px;font-size:11px;font-weight:700}.pr-saved{font-size:11px;color:#15803d}
.pr-detail{grid-column:1/-1;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;background:#fafbfc;border:1px solid #eef0f5;border-radius:10px;padding:12px 14px;margin:4px 0}
.pr-hist table{width:100%;font-size:12px;margin-top:6px}.pr-hist th,.pr-hist td{padding:6px 8px;text-transform:none;letter-spacing:0}
.pr-new p{font-size:12px;margin:4px 0 8px}.pr-new-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.pr-new-grid label{display:grid;gap:4px;font-size:12px;font-weight:600;color:#374151}
.pr-new-grid input{height:34px;border:1px solid #dfe2e8;border-radius:8px;padding:0 8px;font:inherit;min-width:0}
.pr-import{cursor:pointer;display:inline-flex;align-items:center}.pr-import:focus-visible{outline:3px solid #ede9fe}
.pr-retro{background:#fff;border:1px solid #e7e9ee;border-radius:12px;margin-bottom:12px}
.pr-retro-head{display:flex;gap:10px;align-items:baseline;width:100%;border:0;background:none;padding:12px 14px;font:inherit;font-size:14px;text-align:left;cursor:pointer;color:#1f2328}
.pr-retro-head span{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}.pr-retro-head i{font-style:normal;color:#6b7280}
.pr-retro-head:focus-visible{outline:3px solid #ede9fe;border-radius:12px}
.pr-retro-body{padding:0 14px 14px}.pr-retro-body>p{font-size:12px;margin:0 0 10px}.pr-retro-q{width:100%;max-width:320px;height:34px;border:1px solid #dfe2e8;border-radius:8px;padding:0 10px;font:inherit;margin-bottom:8px}
.pr-retro-table{border:1px solid #eef0f5;border-radius:10px;overflow:hidden;max-height:420px;overflow-y:auto}
.pr-retro-row{display:grid;grid-template-columns:minmax(140px,1.4fr) 70px 120px 150px minmax(140px,2fr);gap:10px;align-items:center;padding:8px 12px;border-bottom:1px solid #f0f1f5;font-size:13px}
.pr-retro-row:last-child{border-bottom:0}.pr-retro-row .r{text-align:right}.pr-retro-hist{font-size:12px}.pr-month{text-align:left}.pr-retro-empty{padding:12px;margin:0}
@media(max-width:760px){.pr-retro-row{grid-template-columns:minmax(0,1fr) 90px 130px}.pr-retro-row>:nth-child(2),.pr-retro-row>:nth-child(5){display:none}}
.pr-bulk label{display:flex;align-items:center;gap:6px;font-size:13px}.pr-bulk input{width:110px;height:32px;border:0;border-radius:7px;padding:0 8px;font:inherit}.pr-bulk input[type=month]{width:140px}
@media(max-width:1200px){.pr-row{grid-template-columns:34px minmax(180px,2fr) 80px 80px 110px 100px 90px}.pr-row>:nth-child(4),.pr-row>:nth-child(5){display:none}}
@media(max-width:760px){.pr-filters{grid-template-columns:1fr}.pr-row{grid-template-columns:30px minmax(0,1fr) 90px 80px}.pr-row>:nth-child(3),.pr-row>:nth-child(8),.pr-row>:nth-child(9){display:none}.pr-detail{grid-template-columns:1fr}.pr-new-grid{grid-template-columns:1fr 1fr}}`;
  document.head.appendChild(style);

  return {render};
})();

/* Separate state and actions for card merging; cost editing never uses this selection. */
window.WBOPSMerges = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let root, cards = [], selected = [], busy = false, loaded = false, preview = null, batchResult = null;
  const widthKey = 'wbops.wb-merges.column-widths.v3';
  const columns = [['Выбор',90],['Номер склейки',150],['nmID',115],['Артикул',130],['Название',420],['Бренд',125],['Предмет',200],['Кошки/Собаки',160],['Кабинет',190],['imtID',125],['Статус WB',185]];
  let widths = columns.map(c => c[1]);
  try {
    const saved = JSON.parse(localStorage.getItem(widthKey));
    if (Array.isArray(saved)) widths = widths.map((w, i) => Number.isFinite(saved[i]) ? Math.max(70, Math.min(1200, saved[i])) : w);
  } catch (e) { /* Ширины остаются доступными и при отключённом localStorage. */ }
  const filters = {q: '', brand: '', subject: '', cabinet_id: '', state: '', pet_kind: '', merge_label: ''};
  const key = c => JSON.stringify([c.cabinet_id, c.nm]);
  const picked = () => selected.map(k => cards.find(c => key(c) === k)).filter(Boolean);
  async function api(url, body) {
    let r;
    try { r = await fetch(url, body === undefined ? {cache: 'no-store'} : body instanceof FormData ? {method: 'POST', body} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}); }
    catch (e) { throw new Error('Не удалось связаться с сервером. Проверьте подключение и повторите.'); }
    let d;
    try { d = await r.json(); } catch (e) { /* Сервер или прокси может вернуть HTML. */ }
    if (r.status === 404) throw new Error('Раздел склеек пока недоступен на сервере. Обновите страницу позже или обратитесь к администратору.');
    if (!r.ok) throw new Error(typeof d?.detail === 'string' ? d.detail : `Сервер не смог обработать запрос (HTTP ${r.status}). Повторите позже.`);
    if (!d || typeof d !== 'object') throw new Error('Сервер вернул некорректный ответ. Повторите позже.');
    return d;
  }
  function message(s) { root.querySelector('[data-merge-message]').textContent = s; }
  function applyWidths() {
    const table = root.querySelector('.wm-card-table');
    table.style.width = `${widths.reduce((sum, w) => sum + w, 0)}px`;
    table.querySelectorAll('col').forEach((col, i) => { col.style.width = `${widths[i]}px`; });
  }
  function resizeColumns() {
    applyWidths();
    root.querySelectorAll('[data-merge-resize]').forEach(handle => {
      const index = Number(handle.dataset.mergeResize);
      const save = () => { try { localStorage.setItem(widthKey, JSON.stringify(widths)); } catch (e) { /* Недоступное хранилище не мешает работе. */ } };
      handle.onpointerdown = ev => {
        if (ev.button !== 0) return;
        ev.preventDefault();
        const startX = ev.clientX, initialWidth = widths[index];
        handle.setPointerCapture(ev.pointerId);
        handle.classList.add('wm-resizing');
        handle.onpointermove = move => {
          widths[index] = Math.max(70, Math.min(1200, initialWidth + move.clientX - startX));
          applyWidths();
        };
        const finish = () => {
          handle.onpointermove = null;
          handle.classList.remove('wm-resizing');
          save();
        };
        handle.onpointerup = finish;
        handle.onpointercancel = finish;
        handle.onlostpointercapture = finish;
      };
      handle.onkeydown = ev => {
        if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
        ev.preventDefault();
        widths[index] = Math.max(70, Math.min(1200, widths[index] + (ev.key === 'ArrowRight' ? 10 : -10)));
        applyWidths(); save();
      };
    });
  }
  function previewPanel() {
    const panel = root.querySelector('[data-merge-preview]');
    panel.hidden = !preview && !batchResult;
    const groups = preview?.groups || [];
    const results = batchResult?.groups || [];
    const summary = batchResult?.summary;
    panel.innerHTML = `${preview ? `<h3>Проверка Excel</h3><p>Готово групп: ${esc(preview.valid_count)}. С ошибками: ${esc(preview.invalid_count)}. Первая строка каждой группы — целевая карточка.</p>
      <div class="wm-preview-groups">${groups.map(g => `<div class="wm-preview-group ${g.valid ? '' : 'wm-preview-error'}"><strong>${g.kind === 'single' ? 'Очистка номера · одиночный товар' : `Склейка ${esc(g.number)}`} — ${g.valid ? 'готова' : 'ошибка'}</strong>
        <p>Кабинет: ${esc(g.cabinet || g.cabinet_id || '—')}. Предмет: ${esc(g.subject || g.subject_id || '—')}. Цель: nmID ${esc(g.target_nm ?? g.nm_ids?.[0] ?? '—')}, imtID ${esc(g.target_imt || '—')}.</p>
        <p>Желаемый состав: ${esc((g.desired_members || g.nm_ids || []).join(', '))}</p>
        <p>Выйдут из склейки по отдельности: ${esc(g.exits?.join(', ') || 'нет')}. ${g.no_api ? 'Без API-действий — сохранить номер.' : g.join_needed ? 'После выхода лишних — объединить желаемый состав.' : 'Объединение не требуется.'}</p>${g.error ? `<p>${esc(g.error)}</p>` : ''}</div>`).join('')}</div>
      <button type="button" class="rv-dark" data-merge-batch ${busy || !preview.preview_id || !preview.valid_count ? 'disabled' : ''}>Подтвердить состав ${esc(preview.valid_count)} групп</button>` : ''}
      ${batchResult ? `<h3>Результат склеек</h3>${summary ? `<p>Всего: ${esc(summary.total)}. Принято WB: ${esc(summary.success)}. Ошибок: ${esc(summary.error)}. Неизвестный статус: ${esc(summary.unknown)}.</p>` : ''}
      <div class="wm-preview-groups">${results.map(g => `<div class="wm-preview-group ${g.status === 'success' ? '' : 'wm-preview-error'}"><strong>${g.kind === 'single' ? 'Одиночный товар' : `Склейка ${esc(g.number)}`} — ${g.status === 'success' ? (g.no_api ? 'сохранена без API-действий' : 'запросы приняты WB') : g.status === 'unknown' ? 'статус неизвестен' : 'ошибка'}</strong>
        ${(g.operations || []).map(op => `<p>${op.operation === 'detach' ? 'Отделение' : 'Объединение'} nmID ${esc(op.nm_ids.join(', '))}: ${op.status === 'success' ? 'принято WB' : op.status === 'unknown' ? 'статус неизвестен' : 'ошибка'}</p>`).join('')}
        ${g.error ? `<p>${esc(g.error)}</p>` : ''}${g.warning ? `<p>${esc(g.warning)}</p>` : ''}</div>`).join('')}</div>` : ''}`;
    const button = panel.querySelector('[data-merge-batch]');
    if (button) button.onclick = submitBatch;
  }
  function invalidatePreview() { preview = null; batchResult = null; previewPanel(); }
  function invalid(chosen) {
    if (chosen.length < 2 || chosen.length > 30) return 'Выберите от 2 до 30 карточек.';
    if (chosen.some(c => !c.cabinet_id || !c.imt_id || !c.subject_id)) return 'Для выбранных карточек не хватает данных. Обновите карточки WB.';
    if (new Set(chosen.map(c => c.cabinet_id)).size !== 1) return 'Выберите карточки одного кабинета.';
    if (new Set(chosen.map(c => c.subject_id)).size !== 1) return 'Выберите карточки одного предмета.';
    if (chosen.every(c => c.imt_id === chosen[0].imt_id)) return 'Карточки уже находятся в одной склейке.';
    return '';
  }
  function table() {
    const rows = cards.filter(c => (!filters.brand || c.brand === filters.brand) && (!filters.subject || c.subject === filters.subject) &&
      (!filters.pet_kind || c.pet_kind === filters.pet_kind) && (!filters.merge_label || c.merge_label === filters.merge_label) &&
      (!filters.cabinet_id || c.cabinet_id === filters.cabinet_id) && (!filters.state || (filters.state === 'single' ? c.is_single === true : c.is_single === false)) && (!filters.q ||
      [c.nm,c.vendor,c.title,c.brand,c.subject,c.cabinet,c.imt_id,c.merge_label].join(' ').toLocaleLowerCase('ru').includes(filters.q.toLocaleLowerCase('ru'))));
    const chosen = picked(), first = chosen[0], error = invalid(chosen);
    root.querySelector('[data-merge-target]').textContent = first
      ? `Цель — первая выбранная: nmID ${first.nm}, ${first.vendor}, кабинет ${first.cabinet}, imtID ${first.imt_id || 'неизвестен'}. Выбрано: ${chosen.length}/30. ${error}`
      : 'Первая выбранная карточка станет целевой. Выберите от 2 до 30 карточек одного кабинета и предмета.';
    root.querySelector('[data-merge-submit]').disabled = busy || !!error;
    root.querySelector('[data-merge-clear]').disabled = busy || !selected.length;
    root.querySelector('[data-merge-refresh]').disabled = busy;
    root.querySelector('[data-merge-import]').disabled = busy;
    root.querySelector('.wm-excel-import').classList.toggle('wm-disabled', busy);
    root.querySelector('.wm-excel-import').setAttribute('aria-disabled', String(busy));
    root.querySelector('[data-merge-count]').textContent = `Показано: ${rows.length} из ${cards.length}. Выбор сохраняется при фильтрации.`;
    root.querySelector('tbody').innerHTML = rows.map(c => `<tr${first && key(c) === key(first) ? ' style="background:#f3e8ff"' : ''}>
      <td><input type="checkbox" data-merge-pick="${esc(key(c))}" aria-label="Выбрать nmID ${esc(c.nm)}" ${selected.includes(key(c)) ? 'checked' : ''} ${busy ? 'disabled' : ''}>${first && key(c) === key(first) ? ' Цель' : ''}</td>
      <td>${esc(c.merge_label || '')}</td><td>${esc(c.nm)}</td><td>${esc(c.vendor)}</td><td>${esc(c.title)}</td><td>${esc(c.brand)}</td><td>${esc(c.subject)} (${esc(c.subject_id || '—')})</td><td>${esc(c.pet_kind || 'Не определено')}</td><td>${esc(c.cabinet)}</td><td>${esc(c.imt_id || '—')}</td><td>${c.is_single === true ? 'Одиночный · 1' : c.is_single === false ? `В склейке · ${esc(c.merge_size)}` : 'Неизвестен — обновите WB'}</td></tr>`).join('') || '<tr><td colspan="11">Карточки не найдены. При первом запуске обновите список WB.</td></tr>';
    previewPanel();
  }
  function options() {
    for (const [field, label] of [['brand','Все бренды'],['subject','Все предметы'],['cabinet_id','Все кабинеты'],['pet_kind','Кошки/Собаки: все'],['merge_label','Все номера склеек']]) {
      const values = field === 'pet_kind' ? ['Кошки', 'Собаки', 'Кошки и собаки', 'Не определено'] : [...new Set(cards.map(c => c[field]).filter(Boolean))].sort();
      root.querySelector(`[name=${field}]`).innerHTML = `<option value="">${label}</option>` + values.map(v =>
        `<option value="${esc(v)}" ${filters[field] === v ? 'selected' : ''}>${esc(field === 'cabinet_id' ? `${cards.find(c => c.cabinet_id === v).cabinet} (${v})` : v)}</option>`).join('');
    }
  }
  async function load() {
    const d = await api('/api/nomenclature/wb-merges');
    cards = d.cards || []; loaded = true;
    selected = selected.filter(k => cards.some(c => key(c) === k));
    options(); table();
    root.querySelector('[data-merge-updated]').textContent = `Обновлено: ${d.updated_at || 'ещё не загружено'}${d.errors?.length ? '. Ошибки загрузки: ' + d.errors.join('; ') : ''}`;
  }
  async function submit() {
    if (busy) return;
    const chosen = picked(), error = invalid(chosen);
    if (error) { message(error); return; }
    const first = chosen[0];
    if (!window.confirm(`Склеить ${chosen.length} карточек WB?\nКабинет: ${first.cabinet}. Предмет: ${first.subject}.\nЦель: nmID ${first.nm}, артикул ${first.vendor}, imtID ${first.imt_id}.\nКарточки: ${chosen.map(c => c.nm).join(', ')}.\nКарточки будут перенесены в склейку целевой карточки.`)) return;
    invalidatePreview();
    busy = true; table(); message('Проверяю карточки в кабинете WB и отправляю склейку…');
    try {
      const result = await api('/api/nomenclature/wb-merges', {confirmed: true, cabinet_id: first.cabinet_id,
        target_imt: first.imt_id, nm_ids: chosen.map(c => c.nm)});
      selected = [];
      await load(); message(result.warning || 'Запрос склейки принят WB. Карточки обновлены.');
    } catch (e) { message(e.message); }
    finally { busy = false; table(); }
  }
  async function refresh() {
    if (busy) return;
    invalidatePreview();
    busy = true; table(); message('Загружаю отдельные карточки WB по кабинетам…');
    try { const d = await api('/api/nomenclature/refresh', {}); selected = []; await load(); message(`Загружено карточек: ${d.cards}. ${d.errors?.join('; ') || ''}`); }
    catch (e) { message(e.message); }
    finally { busy = false; table(); }
  }
  async function importExcel(file) {
    if (busy || !file) return;
    if (!/\.xlsx$/i.test(file.name)) { message('Выберите файл Excel .xlsx.'); return; }
    invalidatePreview();
    busy = true; table(); message('Проверяю группы Excel по актуальному кэшу. Изменения в WB ещё не отправляются…');
    try {
      const body = new FormData(); body.append('file', file);
      preview = await api('/api/nomenclature/wb-merges/preview', body);
      message('Проверьте группы и цели ниже. Для отправки в WB нажмите отдельную кнопку подтверждения.');
    } catch (e) { message(e.message); }
    finally { busy = false; table(); }
  }
  async function submitBatch() {
    if (busy || !preview?.preview_id || !preview.valid_count) return;
    const previewId = preview.preview_id;
    // Одно подтверждение не отправляется повторно, даже если ответ сервера потерян.
    preview.preview_id = null;
    busy = true; table(); message('Проверяю свежие карточки WB и последовательно отправляю группы. Дождитесь итогов…');
    try {
      batchResult = await api('/api/nomenclature/wb-merges/batch', {confirmed: true, preview_id: previewId});
      // Persisted successful outcomes can be shown immediately, even if list reload fails.
      // Unknown/error joins never acquire a label; confirmed individual exits clear theirs.
      for (const group of batchResult.groups || []) {
        const detached = new Set((group.operations || []).filter(op => op.operation === 'detach' && op.status === 'success').flatMap(op => op.nm_ids));
        for (const card of cards.filter(c => c.cabinet_id === group.cabinet_id)) {
          if (detached.has(card.nm)) card.merge_label = '';
          if (group.status === 'success' && group.nm_ids.includes(card.nm)) card.merge_label = group.number || '';
        }
      }
      options(); table();
      selected = [];
      const summary = batchResult.summary || {};
      let text = `Обработка завершена. Принято WB: ${summary.success || 0}, ошибок: ${summary.error || 0}, неизвестный статус: ${summary.unknown || 0}.`;
      if (summary.unknown) text += ' Проверьте результат в WB перед новой операцией с этими карточками.';
      try { await load(); } catch (e) { text += ` Список карточек не обновлён: ${e.message}`; }
      message(text);
    } catch (e) { message(`${e.message} Обновите карточки и проверьте результат в WB перед повторной загрузкой файла.`); }
    finally { busy = false; table(); }
  }
  function render(s) {
    root = s;
    root.className = 'panel rv-page pr-page wm-page';
    root.innerHTML = `<div class="wm-head"><div><h2>Склейки WB</h2><p class="muted">Каждая строка — отдельная карточка WB. Склейка объединяет карточки одного предмета внутри одного кабинета.</p></div>
      <button type="button" class="rv-dark" data-merge-refresh>Обновить карточки WB</button></div>
      <div class="wm-filters"><label class="rv-search"><span class="sr">Поиск по nmID, артикулу, названию или imtID</span><input type="search" name="q" placeholder="Поиск карточек…" aria-label="Поиск по nmID, артикулу, названию или imtID"><span aria-hidden="true">⌕</span></label>
      <label class="rv-select"><span class="sr">Бренд</span><select name="brand" aria-label="Бренд"></select></label>
      <label class="rv-select"><span class="sr">Предмет</span><select name="subject" aria-label="Предмет"></select></label>
      <label class="rv-select"><span class="sr">Кошки/Собаки</span><select name="pet_kind" aria-label="Кошки/Собаки"></select></label>
      <label class="rv-select"><span class="sr">Номер склейки</span><select name="merge_label" aria-label="Номер склейки"></select></label>
      <label class="rv-select"><span class="sr">Кабинет</span><select name="cabinet_id" aria-label="Кабинет"></select></label>
      <label class="rv-select"><span class="sr">Статус WB</span><select name="state" aria-label="Статус WB"><option value="">Все</option><option value="group">В склейке</option><option value="single">Одиночные</option></select></label></div>
      <p class="muted" data-merge-updated></p><p data-merge-target></p><div class="wm-actions"><button type="button" class="rv-dark" data-merge-submit disabled>Склеить выбранные</button><button type="button" class="rv-dark wm-secondary" data-merge-clear>Снять выбор</button></div>
      <div class="wm-actions"><a class="rv-dark" href="/api/nomenclature/wb-merges/export">Скачать Excel</a><label class="rv-dark wm-excel-import" role="button" tabindex="0">Загрузить Excel<input type="file" accept=".xlsx" data-merge-import hidden></label></div>
      <p class="muted">В Excel одинаковый «Номер склейки» задаёт точный состав 2–30 карточек. Первая строка — цель. Лишние карточки её WB-группы выйдут по отдельности. Очистка ранее сохранённого номера делает товар одиночным; остальные пустые строки не меняют WB. Проверьте состав и подтвердите отправку.</p>
      <p role="status" aria-live="polite" data-merge-message></p><p class="muted" data-merge-count></p>
      <section class="wm-preview" data-merge-preview hidden></section>
      <div class="table-wrap"><table class="wm-card-table"><colgroup>${columns.map(() => '<col>').join('')}</colgroup><thead><tr>${columns.map(([name], i) => `<th>${name}<span class="wm-resize" data-merge-resize="${i}" role="separator" aria-orientation="vertical" tabindex="0" aria-label="Ширина столбца ${name}" title="Потяните для изменения ширины"></span></th>`).join('')}</tr></thead><tbody></tbody></table></div>`;
    resizeColumns();
    root.querySelector('[name=q]').value = filters.q;
    root.querySelector('[name=state]').value = filters.state;
    root.oninput = ev => { if (ev.target.name === 'q') { filters.q = ev.target.value; table(); } };
    root.onchange = ev => {
      const t = ev.target;
      if (t.matches('[data-merge-import]')) {
        const file = t.files?.[0]; t.value = ''; importExcel(file); return;
      } else if (t.dataset.mergePick) {
        if (busy) return;
        if (t.checked && selected.length >= 30) { t.checked = false; message('Можно выбрать максимум 30 карточек.'); return; }
        selected = t.checked ? [...selected, t.dataset.mergePick] : selected.filter(k => k !== t.dataset.mergePick);
      } else if (Object.hasOwn(filters, t.name)) {
        filters[t.name] = t.value;
        t.closest('.rv-select')?.classList.toggle('set', !!t.value);
      }
      table();
    };
    root.querySelector('[data-merge-submit]').onclick = submit;
    root.querySelector('[data-merge-refresh]').onclick = refresh;
    root.querySelector('[data-merge-clear]').onclick = () => { if (!busy) { selected = []; table(); } };
    root.querySelector('.wm-excel-import').onkeydown = ev => {
      if ((ev.key === 'Enter' || ev.key === ' ') && !busy) { ev.preventDefault(); root.querySelector('[data-merge-import]').click(); }
    };
    previewPanel();
    if (loaded) { options(); table(); }
    else load().catch(e => message(e.message));
  }
  const style = document.createElement('style');
  style.textContent = `.wb-member .pt-tabs a[data-nav="wb-merges"]{display:none}
#wbMergesPage .wm-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:16px}
#wbMergesPage .wm-head h2{margin:0 0 6px}#wbMergesPage .wm-head p{margin:0}
#wbMergesPage .wm-filters{display:grid;grid-template-columns:minmax(220px,2fr) repeat(3,minmax(0,1fr));gap:10px;margin-bottom:14px}
#wbMergesPage .wm-filters>*{min-width:0}#wbMergesPage .wm-filters .rv-search{grid-column:auto;display:block}
#wbMergesPage .wm-filters input,#wbMergesPage .wm-filters select{min-width:0;width:100%}
#wbMergesPage .wm-actions{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
#wbMergesPage .wm-secondary{background:#eef0f4;color:#1f2328}#wbMergesPage .wm-secondary:hover{background:#dfe2e8}
#wbMergesPage .wm-card-table{table-layout:fixed;border-collapse:collapse}#wbMergesPage .wm-card-table th,#wbMergesPage .wm-card-table td{padding:10px;text-align:left;border-bottom:1px solid #e5e7eb;overflow-wrap:anywhere;box-sizing:border-box}#wbMergesPage .table-wrap{overflow:auto;max-width:100%;width:100%}
#wbMergesPage .wm-card-table th{position:relative;padding-right:18px}#wbMergesPage .wm-resize{position:absolute;right:0;top:0;width:10px;height:100%;cursor:col-resize;touch-action:none;user-select:none}#wbMergesPage .wm-resize:hover,#wbMergesPage .wm-resize:focus-visible,#wbMergesPage .wm-resizing{background:#c4b5fd;outline:0}
#wbMergesPage .wm-actions a,#wbMergesPage .wm-excel-import{display:inline-flex;align-items:center;text-decoration:none;cursor:pointer}#wbMergesPage .wm-disabled{opacity:.55;cursor:wait}
#wbMergesPage .wm-preview{padding:14px;border:1px solid #c4b5fd;border-radius:10px;margin:14px 0}#wbMergesPage .wm-preview h3{margin:0 0 8px}#wbMergesPage .wm-preview p{margin:6px 0;overflow-wrap:anywhere}#wbMergesPage .wm-preview-groups{display:grid;gap:8px;margin:12px 0;max-height:440px;overflow:auto}#wbMergesPage .wm-preview-group{padding:10px;background:#f5f3ff;border-radius:6px}#wbMergesPage .wm-preview-error{background:#fff1f2;color:#881337}
@media(max-width:900px){#wbMergesPage .wm-filters{grid-template-columns:repeat(3,minmax(0,1fr))}#wbMergesPage .wm-filters .rv-search{grid-column:1/-1}}
@media(max-width:600px){#wbMergesPage .wm-head{flex-direction:column}#wbMergesPage .wm-filters{grid-template-columns:1fr}#wbMergesPage .wm-filters .rv-search{grid-column:auto}}`;
  document.head.append(style);
  return {render};
})();
