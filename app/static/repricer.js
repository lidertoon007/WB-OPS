/* WB OPS: «Репрайсер» — ручное управление ценами Wildberries с прогнозом маржи до отправки и лимитами безопасности. */
window.WBOPSRepricer = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const num = (v, d = 0) => v == null || v === '' || isNaN(v) ? '—' : Number(v).toLocaleString('ru-RU', {minimumFractionDigits: d, maximumFractionDigits: d});
  const pct = v => v == null ? '—' : num(v, 1) + '%';
  const dt = s => s ? new Date(s).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}) : '—';
  const st = {data: null, tab: 'prices', f: {cabinet: '', brand: '', category: '', q: '', inStock: true, onlyAdded: true}, sel: new Set(), sending: new Set(),
    edit: null, open: null, openHist: null, histDays: 7, modal: null, ops: [], hf: {days: 30, author: '', status: ''}, lastRefresh: 0, tick: null, setQ: '', setOnly: false};
  let root = null;
  try { Object.assign(st.f, JSON.parse(localStorage.getItem('wbops.repricer.f') || '{}')); } catch (e) { /* без хранилища */ }
  const remember = () => { try { localStorage.setItem('wbops.repricer.f', JSON.stringify(st.f)); } catch (e) { /* ignore */ } };
  const api = async (url, opt = {}) => {
    const isForm = opt.body instanceof FormData;
    const r = await fetch(url, {method: opt.method || 'GET', headers: opt.body && !isForm ? {'Content-Type': 'application/json'} : {},
      body: isForm ? opt.body : opt.body ? JSON.stringify(opt.body) : undefined});
    if (opt.raw) { if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || 'Ошибка'); return r; }
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || 'Ошибка запроса');
    return d;
  };

  // ---------------------------------------------------------------- каркас
  async function render(section) {
    root = section;
    root.className = 'panel rv-page rp-page';
    root.innerHTML = `<div class="db-head"><h2>Репрайсер Wildberries</h2><span class="muted db-fresh">Цена уходит на WB только после вашего подтверждения</span></div>
      <div class="rnp-tabs rp-tabs" role="tablist">${[['prices', 'Управление ценами'], ['history', 'История действий'], ['settings', 'Настройки']].map(([k, t]) =>
        `<button type="button" role="tab" data-tab="${k}">${t}</button>`).join('')}</div>
      <div data-body><p class="muted">Загружаю цены с WB…</p></div><div data-modal></div>`;
    root.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => { st.tab = b.dataset.tab; draw(); });
    await load();
    const sel = new URLSearchParams(location.search).get('sel');            // переход из РНП: выбранные артикулы
    if (sel && st.data) {
      const keys = new Set(sel.split(',').map(s => s.trim().toUpperCase()).filter(Boolean));
      st.data.rows.filter(r => keys.has(r.key)).forEach(r => st.sel.add(r.id));
      st.f.onlyAdded = false; st.f.inStock = false; st.f.q = '';
      history.replaceState(null, '', location.pathname);
      draw();
      if (st.sel.size) openMass();
    }
    clearInterval(st.tick);
    st.tick = setInterval(() => { if (!root?.isConnected) return clearInterval(st.tick); const c = root.querySelector('[data-clock]'); if (c) drawClock(c); }, 15000);
  }

  async function load() {
    try { st.data = await api('/api/repricer'); }
    catch (e) { root.querySelector('[data-body]').innerHTML = `<p class="rv-error">${esc(e.message)}</p>`; return; }
    draw();
  }

  function draw() {
    root.querySelectorAll('[data-tab]').forEach(b => { b.classList.toggle('active', b.dataset.tab === st.tab); b.setAttribute('aria-selected', String(b.dataset.tab === st.tab)); });
    if (!st.data) return;
    if (!st.data.cabinets.length) {
      root.querySelector('[data-body]').innerHTML = '<div class="rv-empty"><b>Нет кабинета Wildberries с ключом API</b><p class="muted">Добавьте ключ в «Кабинеты маркетплейсов». Нужна категория «Цены и скидки» с доступом на чтение и запись.</p></div>';
      return;
    }
    ({prices: drawPrices, history: drawHistory, settings: drawSettings}[st.tab])();
  }

  // ---------------------------------------------------------------- управление ценами
  // Колонки таблицы: ключ сортировки/фильтра, заголовок, тип фильтра
  const COLS = [
    ['vendor', 'Товар', 'text', r => `${r.vendor} ${r.nm_id} ${r.name}`],
    ['stock', 'Остаток', 'num', r => r.in_stock ? r.stock : 0],
    ['price', 'Цена до скидки', 'num', r => r.price],
    ['discount', 'Скидка, %', 'num', r => r.discount],
    ['price_before_spp', 'Цена до СПП', 'num', r => r.price_before_spp],
    ['spp', 'СПП', 'num', r => r.in_stock ? r.spp : null],
    ['client_wallet', 'Цена для клиента', 'num', r => r.in_stock ? r.client_wallet : null],
    ['margin', 'Маржа', 'num', r => r.margin],
    ['profit', 'Прибыль, ₽/шт', 'num', r => r.profit],
    ['last_change', 'Изменена', 'changed', r => r.last_change],
  ];
  st.f.cf = st.f.cf || {}; st.f.sort = st.f.sort || ''; st.f.desc = !!st.f.desc;
  // «5000-6000», «>5000», «<10», «>=5», «5000» (точно), «-» или «пусто» — нет значения
  function numMatch(v, expr) {
    const e = String(expr).replace(/\s+/g, '').replace(',', '.');
    if (!e) return true;
    if (e === '-' || e === 'пусто') return v == null;
    if (v == null) return false;
    let m = e.match(/^(>=|<=|>|<|=)?(-?\d+(?:\.\d+)?)$/);
    if (m) { const n = +m[2]; return {'>': v > n, '<': v < n, '>=': v >= n, '<=': v <= n}[m[1]] ?? Math.abs(v - n) < 0.5; }
    m = e.match(/^(-?\d+(?:\.\d+)?)?[-–…](-?\d+(?:\.\d+)?)?$/);
    if (m) return (m[1] == null || v >= +m[1]) && (m[2] == null || v <= +m[2]);
    return true;
  }
  const colOk = r => COLS.every(([k, , type, get]) => {
    const f = st.f.cf[k];
    if (f == null || f === '') return true;
    if (type === 'text') return get(r).toLowerCase().includes(String(f).toLowerCase());
    if (type === 'changed') return f === 'yes' ? !!r.last_change : f === 'no' ? !r.last_change : f === 'day' ? !!r.last_change && Date.now() - new Date(r.last_change) < 864e5 : true;
    return numMatch(get(r), f);
  });
  const filtered = () => {
    const rows = st.data.rows.filter(r => (!st.f.cabinet || r.cabinet_id === st.f.cabinet) && (!st.f.brand || r.brand === st.f.brand)
      && (!st.f.category || r.category === st.f.category) && (!st.f.inStock || r.in_stock) && (!st.f.onlyAdded || r.included || st.sel.has(r.id))
      && (!st.f.q || `${r.vendor} ${r.nm_id} ${r.name}`.toLowerCase().includes(st.f.q.toLowerCase())) && colOk(r));
    const col = COLS.find(c => c[0] === st.f.sort);
    if (col) {
      const sign = st.f.desc ? -1 : 1, get = col[3];
      rows.sort((a, b) => {
        let x = get(a), y = get(b);
        if (col[2] === 'text') return sign * String(a.vendor).localeCompare(String(b.vendor), 'ru', {numeric: true});
        if (col[2] === 'changed') { x = x ? +new Date(x) : null; y = y ? +new Date(y) : null; }
        if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
        return sign * (x - y);
      });
    }
    return rows;
  };

  function drawClock(el) {
    const ages = Object.values(st.data.state || {}).map(s => s.at ? Date.now() / 1000 - s.at : 1e9);
    const age = ages.length ? Math.max(...ages) : 1e9, min = age / 60;
    const cls = min < 5 ? 'g' : min < 7 ? 'y' : 'r';
    el.className = 'rp-clock ' + cls;
    el.title = age > 1e8 ? 'данные ещё не загружены' : `данные обновлены ${Math.round(min)} мин назад`;
    el.setAttribute('aria-label', el.title);
    const btn = root.querySelector('[data-refresh]');
    if (btn) btn.disabled = Date.now() - st.lastRefresh < 60000;
  }

  function drawPrices() {
    const rows = filtered(), d = st.data, added = d.rows.filter(r => r.included).length;
    const opt = (v, t, cur) => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(t)}</option>`;
    const brands = [...new Set(d.rows.map(r => r.brand).filter(Boolean))].sort(), cats = [...new Set(d.rows.map(r => r.category).filter(Boolean))].sort();
    const errs = Object.entries(d.state || {}).filter(([, s]) => s.error).map(([cid, s]) => `${(d.cabinets.find(c => c.id === cid) || {}).name}: ${s.error}`);
    root.querySelector('[data-body]').innerHTML = `
      <div class="db-filters rp-filters">
        ${d.cabinets.length > 1 ? `<select data-f="cabinet" aria-label="Магазин">${opt('', 'Все магазины', st.f.cabinet)}${d.cabinets.map(c => opt(c.id, c.name, st.f.cabinet)).join('')}</select>` : ''}
        <select data-f="brand" aria-label="Бренд">${opt('', 'Все бренды', st.f.brand)}${brands.map(b => opt(b, b, st.f.brand)).join('')}</select>
        <select data-f="category" aria-label="Категория">${opt('', 'Все категории', st.f.category)}${cats.map(b => opt(b, b, st.f.category)).join('')}</select>
        <input type="search" data-f="q" placeholder="Артикул или название" value="${esc(st.f.q)}" aria-label="Поиск">
        <label class="rp-check"><input type="checkbox" data-f="inStock" ${st.f.inStock ? 'checked' : ''}> В продаже</label>
        <label class="rp-check"><input type="checkbox" data-f="onlyAdded" ${st.f.onlyAdded ? 'checked' : ''}> Только добавленные</label>
        <span class="rp-tools"><span data-clock></span><button type="button" class="db-ghost" data-refresh title="Обновить цены с WB (раз в минуту)">↻ Обновить</button>
          <button type="button" class="db-ghost" data-export>⇩ Выгрузить в файл</button>
          <label class="db-ghost rp-upload">⇧ Загрузить файл<input type="file" accept=".xlsx,.csv" data-import hidden></label></span>
      </div>
      ${errs.map(e => `<div class="db-note">${esc(e)}</div>`).join('')}
      ${!added && st.f.onlyAdded ? `<div class="db-note">В репрайсер ещё не добавлены артикулы. Отметьте нужные в «Настройки → Артикулы» или снимите галочку «Только добавленные». <button type="button" class="rv-link" data-add-all>Добавить все артикулы</button></div>` : ''}
      <div class="rnp-scroll rp-scroll"><table class="rnp-table rp-table"><thead><tr>
        <th class="rnp-sticky rp-c"><input type="checkbox" data-all aria-label="Выбрать все на экране" ${rows.length && rows.every(r => st.sel.has(r.id)) ? 'checked' : ''}></th>
        ${COLS.filter(c => c[0] !== 'discount').map(([k, t]) => `<th class="${k === 'vendor' ? 'rp-name' : ''}" aria-sort="${st.f.sort === k ? (st.f.desc ? 'descending' : 'ascending') : 'none'}">
          <button type="button" class="rp-sort ${st.f.sort === k ? 'on' : ''}" data-sort="${k}">${esc(t)}<span>${st.f.sort === k ? (st.f.desc ? '↓' : '↑') : '↕'}</span></button></th>`).join('')}</tr>
        <tr class="rp-fr"><th class="rnp-sticky rp-c">${Object.values(st.f.cf).some(v => v) ? '<button type="button" class="rp-clear" data-cf-clear title="Сбросить фильтры столбцов" aria-label="Сбросить фильтры столбцов">×</button>' : ''}</th>
        ${COLS.filter(c => c[0] !== 'discount').map(([k, t, type]) => `<th class="${k === 'vendor' ? 'rp-name' : ''}">${type === 'changed'
          ? `<select data-cf="${k}" aria-label="Фильтр: ${t}"><option value="">все</option><option value="day" ${st.f.cf[k] === 'day' ? 'selected' : ''}>за сутки</option><option value="yes" ${st.f.cf[k] === 'yes' ? 'selected' : ''}>менялась</option><option value="no" ${st.f.cf[k] === 'no' ? 'selected' : ''}>не менялась</option></select>`
          : `<input data-cf="${k}" value="${esc(st.f.cf[k] || '')}" placeholder="${type === 'text' ? 'поиск' : 'от–до'}" aria-label="Фильтр: ${t}"
              ${type === 'num' ? 'title="Например: 5000-6000, >5000, <10, 5000; «-» — нет значения" inputmode="text"' : ''}>`}</th>`).join('')}</tr></thead>
      <tbody>${rows.slice(0, 500).map(rowHtml).join('') || '<tr><td colspan="10" class="muted">Ничего не найдено.</td></tr>'}</tbody></table></div>
      ${rows.length > 500 ? `<p class="muted">Показаны первые 500 из ${rows.length}. Уточните фильтр.</p>` : ''}
      <p class="muted rp-note">Маржа — прогноз на проданную штуку: цена до СПП минус комиссия, эквайринг, логистика, реклама и налог (по отчёту WB за 60 дней), себестоимость и подготовка. Цена для клиента — с учётом СПП и скидки WB Кошелька из настроек.</p>
      ${st.sel.size ? `<div class="rp-bar"><b>Выбрано: ${st.sel.size}</b><button type="button" class="rv-link" data-sel-filter>Выбрать все по фильтрам (${rows.length})</button>
        <button type="button" class="rv-link" data-sel-clear>Снять выбор</button><button type="button" class="btn" data-mass>Изменить цену</button></div>` : ''}`;
    bindPrices(rows);
    drawClock(root.querySelector('[data-clock]'));
  }

  function rowHtml(r) {
    const editing = st.edit && st.edit.id === r.id, sending = st.sending.has(r.id);
    const cell = (kind, value) => editing && st.edit.kind === kind
      ? `<form class="rp-edit" data-edit-form><input name="v" type="number" min="1" step="1" value="${Math.round(value || 0)}" aria-label="Новая цена" autofocus><button class="btn" type="submit">ОК</button><button type="button" class="rv-link" data-edit-cancel>×</button></form>`
      : `${num(value)} ₽ <button type="button" class="rp-pen" data-edit="${esc(r.id)}" data-kind="${kind}" aria-label="Изменить ${kind === 'before' ? 'цену до СПП' : 'цену для клиента'}" ${kind === 'client' && r.client_wallet == null ? 'disabled title="Нет данных о цене для клиента — задайте цену до СПП"' : ''}>✎</button>`;
    return `<tr class="${sending ? 'rp-sending' : ''}" data-row="${esc(r.id)}">
      <td class="rnp-sticky rp-c"><input type="checkbox" data-sel="${esc(r.id)}" ${st.sel.has(r.id) ? 'checked' : ''} aria-label="Выбрать ${esc(r.vendor)}"></td>
      <td class="rp-name"><button type="button" class="rp-link" data-hist="${esc(r.id)}">${esc(r.vendor)}</button>${r.included ? '' : ' <span class="rp-tag">не в контуре</span>'}
        <small>${esc(r.name || '')}${r.name ? ' · ' : ''}${r.nm_id}${st.data.cabinets.length > 1 ? ' · ' + esc(r.cabinet) : ''}</small></td>
      <td>${r.in_stock ? num(r.stock) : '<span class="rp-out" title="Остаток на складах WB — 0">Нет в наличии</span>'}</td>
      <td>${num(r.price)} ₽<small>скидка ${r.discount}%</small></td>
      <td>${cell('before', r.price_before_spp)}</td>
      <td>${r.in_stock ? pct(r.spp) : '—'}${r.spp_source === 'заказы' ? '<small title="Витрина WB недоступна — средняя СПП по заказам за 14 дней">по заказам</small>' : ''}</td>
      <td>${r.in_stock ? cell('client', r.client_wallet) : '—'}${r.wallet ? `<small>с Кошельком ${num(r.wallet, 1)}%</small>` : ''}</td>
      <td class="${r.margin == null ? '' : r.margin < 0 ? 'neg' : 'pos'}">${r.margin == null ? (r.cost == null ? '<small>нет себестоимости</small>' : '—') : pct(r.margin)}</td>
      <td class="${r.profit == null ? '' : r.profit < 0 ? 'neg' : ''}">${num(r.profit)}</td>
      <td>${sending ? '<span class="rp-clock b" title="Отправляется на WB"></span>' : dt(r.last_change)}</td></tr>
      ${st.open === r.id ? `<tr class="rp-hist-row"><td colspan="10">${itemHistHtml(r)}</td></tr>` : ''}`;
  }

  function bindPrices(rows) {
    const body = root.querySelector('[data-body]');
    body.querySelectorAll('[data-f]').forEach(el => {
      const h = () => { st.f[el.dataset.f] = el.type === 'checkbox' ? el.checked : el.value; remember();
        if (el.dataset.f === 'q') { const pos = el.selectionStart; drawPrices(); const n = root.querySelector('[data-f=q]'); n.focus(); n.setSelectionRange(pos, pos); } else drawPrices(); };
      el.type === 'search' ? (el.oninput = h) : (el.onchange = h);
    });
    body.querySelectorAll('[data-sort]').forEach(b => b.onclick = () => {
      const k = b.dataset.sort;
      if (st.f.sort !== k) { st.f.sort = k; st.f.desc = k !== 'vendor'; } else if (st.f.desc === (k !== 'vendor')) st.f.desc = !st.f.desc; else st.f.sort = '';
      remember(); drawPrices();
    });
    let cfTimer;
    body.querySelectorAll('[data-cf]').forEach(el => {
      const h = () => { st.f.cf[el.dataset.cf] = el.value; remember();
        clearTimeout(cfTimer); cfTimer = setTimeout(() => {
          const pos = el.selectionStart; drawPrices();
          const n = root.querySelector(`[data-cf="${el.dataset.cf}"]`); if (n && n.tagName === 'INPUT') { n.focus(); n.setSelectionRange(pos, pos); }
        }, el.tagName === 'SELECT' ? 0 : 250); };
      el.tagName === 'SELECT' ? (el.onchange = h) : (el.oninput = h);
    });
    body.querySelector('[data-cf-clear]')?.addEventListener('click', () => { st.f.cf = {}; remember(); drawPrices(); });
    body.querySelector('[data-refresh]').onclick = async e => {
      e.currentTarget.disabled = true; st.lastRefresh = Date.now();
      try { await api('/api/repricer/refresh', {method: 'POST'}); await load(); } catch (err) { alert(err.message); }
    };
    body.querySelector('[data-export]').onclick = async () => {
      try {
        const r = await api('/api/repricer/export', {method: 'POST', body: {ids: rows.map(x => x.id)}, raw: true});
        const a = document.createElement('a'); a.href = URL.createObjectURL(await r.blob()); a.download = `ceny_wb_${new Date().toISOString().slice(0, 10)}.xlsx`; a.click();
      } catch (e) { alert(e.message); }
    };
    body.querySelector('[data-import]').onchange = async e => {
      const fd = new FormData(); fd.append('file', e.target.files[0]); e.target.value = '';
      try { const res = await api('/api/repricer/import', {method: 'POST', body: fd}); openModal({items: res.items, title: 'Цены из файла', note: res.skipped ? `Строк без новой цены пропущено: ${res.skipped}` : ''}); }
      catch (err) { alert(err.message); }
    };
    body.querySelector('[data-add-all]')?.addEventListener('click', async () => { await api('/api/repricer/items', {method: 'PUT', body: {ids: st.data.rows.map(r => r.id), included: true}}); await load(); });
    body.querySelector('[data-all]').onchange = e => { rows.slice(0, 500).forEach(r => e.target.checked ? st.sel.add(r.id) : st.sel.delete(r.id)); drawPrices(); };
    body.querySelectorAll('[data-sel]').forEach(cb => cb.onchange = () => { cb.checked ? st.sel.add(cb.dataset.sel) : st.sel.delete(cb.dataset.sel); drawPrices(); });
    body.querySelector('[data-sel-filter]')?.addEventListener('click', () => { rows.forEach(r => st.sel.add(r.id)); drawPrices(); });
    body.querySelector('[data-sel-clear]')?.addEventListener('click', () => { st.sel.clear(); drawPrices(); });
    body.querySelector('[data-mass]')?.addEventListener('click', openMass);
    body.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => { st.edit = {id: b.dataset.edit, kind: b.dataset.kind}; drawPrices(); root.querySelector('[data-edit-form] input')?.select(); });
    const ef = body.querySelector('[data-edit-form]');
    if (ef) {
      ef.onsubmit = async e => {
        e.preventDefault();
        const v = Number(ef.elements.v.value), id = st.edit.id, kind = st.edit.kind;
        st.edit = null; drawPrices();
        await preview([{id, [kind === 'before' ? 'new_price' : 'new_client']: v}], 'Изменение цены');
      };
      ef.querySelector('[data-edit-cancel]').onclick = () => { st.edit = null; drawPrices(); };
    }
    body.querySelectorAll('[data-hist]').forEach(b => b.onclick = async () => {
      if (st.open === b.dataset.hist) { st.open = null; return drawPrices(); }
      st.open = b.dataset.hist; st.openHist = null; drawPrices();
      await loadItemHist();
    });
    body.querySelectorAll('[data-hdays]').forEach(b => b.onclick = async () => { st.histDays = Number(b.dataset.hdays); await loadItemHist(); });
  }

  // ---------------------------------------------------------------- история артикула (график)
  async function loadItemHist() {
    try { st.openHist = await api(`/api/repricer/items/${encodeURIComponent(st.open)}/history?days=${st.histDays}`); } catch (e) { st.openHist = []; }
    drawPrices();
  }
  function itemHistHtml(r) {
    const h = st.openHist;
    const switcher = `<div class="rp-hist-head"><b>История цен ${esc(r.vendor)}</b>${[[1, 'День'], [7, 'Неделя'], [30, 'Месяц']].map(([d, t]) => `<button type="button" class="${st.histDays === d ? 'on' : ''}" data-hdays="${d}">${t}</button>`).join('')}</div>`;
    if (h == null) return switcher + '<p class="muted">Загрузка…</p>';
    const ok = h.filter(x => x.status === 'ok');
    if (!ok.length) return switcher + '<p class="muted">За период цену через репрайсер не меняли.</p>';
    const pts = [{at: ok[0].at, b: ok[0].old_price, c: ok[0].old_client}, ...ok.map(x => ({at: x.at, b: x.new_price, c: x.new_client})), {at: new Date().toISOString(), b: r.price_before_spp, c: r.client_wallet}];
    const W = 640, H = 160, P = 34, xs = pts.map(p => +new Date(p.at)), ys = pts.flatMap(p => [p.b, p.c]).filter(v => v != null);
    const x0 = Math.min(...xs), x1 = Math.max(...xs) || x0 + 1, y0 = Math.min(...ys) * 0.97, y1 = Math.max(...ys) * 1.03;
    const X = t => P + (t - x0) / Math.max(1, x1 - x0) * (W - 2 * P), Y = v => H - 22 - (v - y0) / Math.max(1, y1 - y0) * (H - 40);
    const step = key => pts.filter(p => p[key] != null).map((p, i, a) => `${i ? `L${X(+new Date(p.at))},${Y(a[i - 1][key])} ` : 'M'}${X(+new Date(p.at))},${Y(p[key])}`).join(' ');
    const dots = key => pts.filter(p => p[key] != null).map(p => `<circle cx="${X(+new Date(p.at))}" cy="${Y(p[key])}" r="4"><title>${dt(p.at)} · ${num(p[key])} ₽</title></circle>`).join('');
    return `${switcher}<svg class="rp-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="График цены до СПП и цены для клиента">
        <text x="${P}" y="12" class="rp-ax">${num(y1)} ₽</text><text x="${P}" y="${H - 6}" class="rp-ax">${num(y0)} ₽</text>
        <path d="${step('b')}" class="rp-l1"/><g class="rp-d1">${dots('b')}</g><path d="${step('c')}" class="rp-l2"/><g class="rp-d2">${dots('c')}</g></svg>
      <div class="rp-legend"><span><i class="rp-k1"></i>Цена до СПП</span><span><i class="rp-k2"></i>Цена для клиента</span></div>
      <table class="tr-table"><thead><tr><th>Когда</th><th>Кто</th><th class="r">Было</th><th class="r">Стало</th><th>Статус</th></tr></thead><tbody>
      ${h.slice().reverse().map(x => `<tr><td>${dt(x.at)}</td><td>${esc(x.author)}</td><td class="r">${num(x.old_price)}</td><td class="r">${num(x.new_price)}</td>
        <td>${x.status === 'ok' ? '<span class="tr-chip ok">Выполнено</span>' : `<span class="tr-chip bad" title="${esc(x.error)}">Ошибка</span> ${esc(x.error)}`}</td></tr>`).join('')}</tbody></table>`;
  }

  // ---------------------------------------------------------------- окно расчёта и подтверждения
  function openMass() {
    openModal({items: null, title: `Изменить цену · ${st.sel.size} шт.`, mass: {kind: 'before', action: 'up', unit: 'pct', value: ''}});
  }
  async function preview(changes, title) {
    try { const res = await api('/api/repricer/preview', {method: 'POST', body: {changes}}); openModal({items: res.items, title}); }
    catch (e) { alert(e.message); }
  }
  function openModal(m) { st.modal = {...m, confirmLarge: false, sending: false}; drawModal(); }
  function closeModal() { st.modal = null; root.querySelector('[data-modal]').innerHTML = ''; }

  function drawModal() {
    const m = st.modal, host = root.querySelector('[data-modal]');
    if (!m) { host.innerHTML = ''; return; }
    const items = m.items || [], valid = items.filter(i => !(i.errors || []).length), bad = items.length - valid.length, large = valid.some(i => i.confirm);
    host.innerHTML = `<div class="rp-overlay" data-close-bg><div class="rp-modal" role="dialog" aria-modal="true" aria-label="${esc(m.title)}">
      <header><h3>${esc(m.title)}</h3><button type="button" class="rnp-del" data-close aria-label="Закрыть">×</button></header>
      ${m.mass ? `<form class="rp-mass" data-mass-form>
        <label>Цена<select name="kind"><option value="before" ${m.mass.kind === 'before' ? 'selected' : ''}>до СПП</option><option value="client" ${m.mass.kind === 'client' ? 'selected' : ''}>для клиента</option></select></label>
        <label>Действие<select name="action"><option value="set" ${m.mass.action === 'set' ? 'selected' : ''}>задать</option><option value="up" ${m.mass.action === 'up' ? 'selected' : ''}>увеличить на</option><option value="down" ${m.mass.action === 'down' ? 'selected' : ''}>уменьшить на</option></select></label>
        <label>Значение<input name="value" type="number" step="0.1" min="0" required value="${esc(m.mass.value)}"></label>
        <label>В чём<select name="unit"><option value="pct" ${m.mass.unit === 'pct' ? 'selected' : ''}>%</option><option value="rub" ${m.mass.unit === 'rub' ? 'selected' : ''}>₽</option></select></label>
        <button class="btn secondary" type="submit">Рассчитать для всех</button></form>` : ''}
      ${m.note ? `<p class="muted">${esc(m.note)}</p>` : ''}
      ${items.length ? `<div class="rp-modal-table"><table class="tr-table"><thead><tr><th>Товар</th><th class="r">Было до СПП</th><th class="r">Станет до СПП</th><th class="r">Для клиента</th><th class="r">Маржа</th><th class="r">Прибыль, ₽/шт</th><th>Проверка</th><th></th></tr></thead><tbody>
        ${items.map((i, n) => `<tr class="${(i.errors || []).length ? 'rp-bad' : i.confirm ? 'rp-warn' : ''}">
          <td><b>${esc(i.vendor || i.nm_id)}</b><small>${esc(i.name || '')}</small></td>
          <td class="r">${num(i.old_price)}</td>
          <td class="r">${i.new_price != null ? `<input type="number" min="1" step="1" value="${Math.round(i.new_price)}" data-fix="${n}" aria-label="Новая цена ${esc(i.vendor)}">` : '—'}${i.change_pct != null ? `<small>${i.change_pct > 0 ? '+' : ''}${num(i.change_pct, 1)}%</small>` : ''}</td>
          <td class="r">${num(i.old_client)} → <b>${num(i.new_client)}</b></td>
          <td class="r">${pct(i.old_margin)} → <b class="${i.new_margin == null ? '' : i.new_margin < 0 ? 'neg' : 'pos'}">${pct(i.new_margin)}</b></td>
          <td class="r">${num(i.new_profit)}</td>
          <td class="rp-msgs">${(i.errors || []).map(e => `<span class="neg">✗ ${esc(e)}</span>`).join('')}${(i.warnings || []).map(w => `<span>! ${esc(w)}</span>`).join('')}${!(i.errors || []).length && !(i.warnings || []).length ? '<span class="pos">✓</span>' : ''}</td>
          <td><button type="button" class="rnp-del" data-drop="${n}" aria-label="Убрать из списка">×</button></td></tr>`).join('')}</tbody></table></div>` : (m.mass ? '<p class="muted">Укажите изменение и нажмите «Рассчитать для всех».</p>' : '')}
      ${m.result ? `<div class="db-note">${esc(m.result)}</div>` : ''}
      <footer>${large ? `<label class="rp-check"><input type="checkbox" data-large ${m.confirmLarge ? 'checked' : ''}> Подтверждаю изменения больше лимита за операцию</label>` : ''}
        <span class="muted">${valid.length ? `К отправке: ${valid.length}` : ''}${bad ? ` · с ошибками: ${bad} (не отправятся — уберите их)` : ''}</span>
        <button type="button" class="btn secondary" data-close>Отмена</button>
        <button type="button" class="btn" data-confirm ${!valid.length || bad || (large && !m.confirmLarge) || m.sending ? 'disabled' : ''}>${m.sending ? 'Отправляю…' : 'Подтвердить и отправить на WB'}</button></footer>
    </div></div>`;
    host.querySelectorAll('[data-close]').forEach(b => b.onclick = closeModal);
    host.querySelector('[data-close-bg]').onclick = e => { if (e.target === e.currentTarget) closeModal(); };
    const mf = host.querySelector('[data-mass-form]');
    if (mf) mf.onsubmit = async e => {
      e.preventDefault();
      m.mass = {kind: mf.elements.kind.value, action: mf.elements.action.value, unit: mf.elements.unit.value, value: mf.elements.value.value};
      try { m.items = (await api('/api/repricer/preview', {method: 'POST', body: {mass: m.mass, ids: [...st.sel]}})).items; } catch (err) { alert(err.message); }
      drawModal();
    };
    host.querySelectorAll('[data-fix]').forEach(inp => inp.onchange = async () => {
      const it = m.items[Number(inp.dataset.fix)];
      try { const res = await api('/api/repricer/preview', {method: 'POST', body: {changes: [{id: it.id, new_price: Number(inp.value)}]}}); m.items[Number(inp.dataset.fix)] = res.items[0]; }
      catch (err) { alert(err.message); }
      drawModal();
    });
    host.querySelectorAll('[data-drop]').forEach(b => b.onclick = () => { m.items.splice(Number(b.dataset.drop), 1); drawModal(); });
    host.querySelector('[data-large]')?.addEventListener('change', e => { m.confirmLarge = e.target.checked; drawModal(); });
    host.querySelector('[data-confirm]').onclick = () => send(valid);
  }

  async function send(items) {
    const m = st.modal;
    m.sending = true; drawModal();
    let op;
    try {
      op = await api('/api/repricer/apply', {method: 'POST', body: {changes: items.map(i => ({id: i.id, new_price: i.new_price})), confirm: m.confirmLarge,
        action: m.mass ? 'Массовое изменение цены' : m.title === 'Цены из файла' ? 'Загрузка из файла' : 'Изменение цены'}});
    } catch (e) { m.sending = false; m.result = e.message; drawModal(); return; }
    closeModal();
    items.forEach(i => st.sending.add(i.id));
    st.sel.clear();
    if (st.tab === 'prices') drawPrices();
    const poll = async () => {
      const o = await api('/api/repricer/ops/' + op.id).catch(() => null);
      if (o && o.status === 'running') return setTimeout(poll, 2500);
      st.sending.clear();
      await load();
      if (o) {
        const errs = Object.entries(o.items || {}).filter(([, v]) => v !== 'ok' && v !== 'sending');
        if (errs.length) alert(`Отправлено: ${o.ok} из ${o.count}. Ошибки WB:\n` + errs.map(([k, v]) => `${k.split(':')[1]}: ${v}`).join('\n'));
      }
    };
    setTimeout(poll, 2500);
  }

  // ---------------------------------------------------------------- история действий
  async function drawHistory() {
    const body = root.querySelector('[data-body]');
    const since = new Date(Date.now() - st.hf.days * 864e5).toISOString();
    const q = new URLSearchParams({since, author: st.hf.author, status: st.hf.status});
    try { st.ops = await api('/api/repricer/history?' + q); } catch (e) { body.innerHTML = `<p class="rv-error">${esc(e.message)}</p>`; return; }
    if (st.tab !== 'history') return;
    const authors = [...new Set(st.ops.map(o => o.author).filter(Boolean))];
    body.innerHTML = `<div class="db-filters">
        <select data-hf="days" aria-label="Период">${[[1, 'Сегодня'], [7, '7 дней'], [30, '30 дней'], [90, '90 дней'], [365, 'Год']].map(([d, t]) => `<option value="${d}" ${st.hf.days === d ? 'selected' : ''}>${t}</option>`).join('')}</select>
        <select data-hf="author" aria-label="Сотрудник"><option value="">Все сотрудники</option>${[...new Set([...authors, st.hf.author].filter(Boolean))].map(a => `<option ${a === st.hf.author ? 'selected' : ''}>${esc(a)}</option>`).join('')}</select>
        <select data-hf="status" aria-label="Статус"><option value="">Все статусы</option>${['Выполнено', 'Выполнено частично', 'Ошибка'].map(s => `<option ${s === st.hf.status ? 'selected' : ''}>${s}</option>`).join('')}</select></div>
      <div class="tr-table-wrap"><table class="tr-table"><thead><tr><th>Дата и время</th><th>Сотрудник</th><th>Действие</th><th class="r">Артикулов</th><th class="r">Успешно</th><th class="r">Ошибок</th><th>Статус</th></tr></thead><tbody>
      ${st.ops.map(o => `<tr><td>${dt(o.created_at)}</td><td>${esc(o.author)}</td><td>${esc(o.action)}</td><td class="r">${o.count}</td><td class="r">${o.ok}</td><td class="r">${o.errors}</td>
        <td><span class="tr-chip ${o.status === 'Выполнено' ? 'ok' : o.status === 'Ошибка' || o.status === 'Прервано' ? 'bad' : 'warn'}">${esc(o.status === 'running' ? 'Отправляется' : o.status)}</span></td></tr>`).join('')
        || '<tr><td colspan="7" class="muted">За период изменений не было.</td></tr>'}</tbody></table></div>
      <p class="muted rp-note">История цен конкретного товара — в «Управлении ценами»: нажмите на артикул.</p>`;
    body.querySelectorAll('[data-hf]').forEach(el => el.onchange = () => { st.hf[el.dataset.hf] = el.dataset.hf === 'days' ? Number(el.value) : el.value; drawHistory(); });
  }

  // ---------------------------------------------------------------- настройки
  function drawSettings() {
    const d = st.data, s = d.settings, body = root.querySelector('[data-body]');
    const q = st.setQ.toLowerCase();
    const rows = d.rows.filter(r => (!st.setOnly || r.included) && (!q || `${r.vendor} ${r.nm_id} ${r.name} ${r.brand}`.toLowerCase().includes(q)));
    body.innerHTML = `<section class="tr-box"><h3>Безопасность</h3>
      <form class="tr-set-grid" data-safety>
        <label>Общий диапазон: мин. цена до СПП, ₽<input name="min_price" type="number" min="0" step="1" value="${s.min_price ?? ''}" placeholder="не задана"></label>
        <label>Общий диапазон: макс. цена до СПП, ₽<input name="max_price" type="number" min="0" step="1" value="${s.max_price ?? ''}" placeholder="не задана"></label>
        <label>Макс. изменение за операцию, %<input name="max_change_pct" type="number" min="0" step="0.5" value="${s.max_change_pct ?? ''}"></label>
        <label>Дневной лимит, %<input name="daily_limit_pct" type="number" min="0" step="0.5" value="${s.daily_limit_pct ?? ''}"></label>
        <label>Менять цену артикула не чаще, ч<input name="min_interval_hours" type="number" min="0" step="0.5" value="${s.min_interval_hours ?? ''}"></label>
        <div class="wide tr-actions"><button class="btn" type="submit">Сохранить</button><span class="muted" data-state></span></div></form>
      <p class="muted rp-note">Индивидуальный диапазон артикула (ниже) главнее общего. Изменение больше лимита за операцию требует отдельного подтверждения; дневной лимит и частота блокируют отправку.</p></section>
      <section class="tr-box"><h3>WB Кошелёк</h3><p class="muted rp-note">WB не отдаёт скидку Кошелька по API — укажите её для каждого магазина, чтобы цена для клиента считалась верно.</p>
      <form class="tr-set-grid" data-wallet>${d.cabinets.map(c => `<label>${esc(c.name)}, %<input name="${esc(c.id)}" type="number" min="0" max="49" step="0.1" value="${s.wallet[c.id] ?? 0}"></label>`).join('')}
        <div class="wide tr-actions"><button class="btn" type="submit">Сохранить</button><span class="muted" data-state></span></div></form></section>
      <section class="tr-box"><h3>Артикулы</h3>
      <div class="db-filters"><input type="search" data-set-q placeholder="Поиск" value="${esc(st.setQ)}" aria-label="Поиск артикула">
        <label class="rp-check"><input type="checkbox" data-set-only ${st.setOnly ? 'checked' : ''}> Только добавленные</label>
        <button type="button" class="db-ghost" data-add-all>Добавить все артикулы</button><button type="button" class="db-ghost" data-remove-all>Убрать все</button></div>
      <div class="rnp-scroll"><table class="rnp-table rp-table"><thead><tr><th class="rnp-sticky">Артикул</th><th>Цена до СПП</th><th>В репрайсере</th><th>Мин. цена, ₽</th><th>Макс. цена, ₽</th></tr></thead><tbody>
      ${rows.slice(0, 500).map(r => `<tr><td class="rnp-sticky rp-name"><b>${esc(r.vendor)}</b><small>${esc(r.name || '')} · ${r.nm_id}</small></td><td>${num(r.price_before_spp)}</td>
        <td><button type="button" class="st-switch ${r.included ? 'on' : ''}" role="switch" aria-checked="${r.included}" data-inc="${esc(r.id)}" aria-label="В репрайсере ${esc(r.vendor)}"><i></i></button></td>
        <td><input class="rp-lim" type="number" min="0" step="1" data-lim="min_price" data-id="${esc(r.id)}" value="${r.min_price ?? ''}" placeholder="общий"></td>
        <td><input class="rp-lim" type="number" min="0" step="1" data-lim="max_price" data-id="${esc(r.id)}" value="${r.max_price ?? ''}" placeholder="общий"></td></tr>`).join('')}</tbody></table></div></section>`;
    const sf = body.querySelector('[data-safety]');
    sf.onsubmit = async e => {
      e.preventDefault();
      const b = {}; ['min_price', 'max_price', 'max_change_pct', 'daily_limit_pct', 'min_interval_hours'].forEach(k => { b[k] = sf.elements[k].value === '' ? null : Number(sf.elements[k].value); });
      try { st.data.settings = await api('/api/repricer/settings', {method: 'PUT', body: b}); sf.querySelector('[data-state]').textContent = 'Сохранено'; } catch (err) { sf.querySelector('[data-state]').textContent = err.message; }
    };
    const wf = body.querySelector('[data-wallet]');
    wf.onsubmit = async e => {
      e.preventDefault();
      const wallet = {}; d.cabinets.forEach(c => { wallet[c.id] = Number(wf.elements[c.id].value || 0); });
      try { await api('/api/repricer/settings', {method: 'PUT', body: {wallet}}); wf.querySelector('[data-state]').textContent = 'Сохранено'; await load(); } catch (err) { wf.querySelector('[data-state]').textContent = err.message; }
    };
    const sq = body.querySelector('[data-set-q]');
    sq.oninput = () => { st.setQ = sq.value; const pos = sq.selectionStart; drawSettings(); const n = root.querySelector('[data-set-q]'); n.focus(); n.setSelectionRange(pos, pos); };
    body.querySelector('[data-set-only]').onchange = e => { st.setOnly = e.target.checked; drawSettings(); };
    body.querySelector('[data-add-all]').onclick = async () => { await api('/api/repricer/items', {method: 'PUT', body: {ids: rows.map(r => r.id), included: true}}); await load(); };
    body.querySelector('[data-remove-all]').onclick = async () => { if (confirm('Убрать все артикулы из репрайсера?')) { await api('/api/repricer/items', {method: 'PUT', body: {ids: rows.map(r => r.id), included: false}}); await load(); } };
    body.querySelectorAll('[data-inc]').forEach(b => b.onclick = async () => {
      const on = b.getAttribute('aria-checked') !== 'true';
      await api('/api/repricer/items', {method: 'PUT', body: {ids: [b.dataset.inc], included: on}});
      const r = st.data.rows.find(x => x.id === b.dataset.inc); if (r) r.included = on;
      b.classList.toggle('on', on); b.setAttribute('aria-checked', String(on));
    });
    body.querySelectorAll('[data-lim]').forEach(inp => inp.onchange = async () => {
      try { await api('/api/repricer/items', {method: 'PUT', body: {ids: [inp.dataset.id], [inp.dataset.lim]: inp.value === '' ? null : Number(inp.value)}}); inp.classList.add('saved');
        const r = st.data.rows.find(x => x.id === inp.dataset.id); if (r) r[inp.dataset.lim] = inp.value === '' ? null : Number(inp.value); }
      catch (err) { alert(err.message); }
    });
  }

  const style = document.createElement('style');
  style.textContent = `
.rp-tabs{margin:12px 0 14px;width:max-content;max-width:100%}
.rp-filters{align-items:center}.rp-filters input[type=search]{min-width:220px;background-image:none;padding:0 12px}
.rp-check{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:500;color:#374151;white-space:nowrap}
.db-filters .rp-check input{appearance:auto;width:16px;height:16px;min-width:0;padding:0;margin:0;border:0;background:none}
.rp-hist-row .tr-table th,.rp-hist-row .tr-table td{text-align:left}.rp-hist-row .tr-table .r{text-align:right}
.rp-tools{display:flex;gap:8px;align-items:center;margin-left:auto;flex-wrap:wrap}.rp-upload{display:inline-flex;align-items:center;cursor:pointer}
.rp-clock{display:inline-block;width:14px;height:14px;border-radius:50%;border:2px solid currentColor;position:relative;flex:none}
.rp-clock::after{content:'';position:absolute;left:5px;top:2px;width:2px;height:5px;background:currentColor;border-radius:1px}
.rp-clock.g{color:#2f9e44}.rp-clock.y{color:#d9a400}.rp-clock.r{color:#c92a2a}.rp-clock.b{color:#1c7ed6}
.rp-scroll{max-height:68vh}.rp-table td,.rp-table th{text-align:right}.rp-table .rp-name{text-align:left;min-width:220px;max-width:320px;white-space:normal}
.rp-table td small{display:block;font-size:11px;color:#6b7280;font-weight:400}.rp-c{width:36px;min-width:36px!important;text-align:center!important}
.rp-sort{border:0;background:none;padding:0;font:inherit;font-weight:600;color:inherit;cursor:pointer;display:inline-flex;gap:4px;align-items:center;white-space:nowrap}
.rp-sort span{color:#c4c4cc;font-size:11px}.rp-sort.on{color:#5b21b6}.rp-sort.on span{color:#5b21b6}
.rp-table thead .rp-fr th{top:38px;background:#fff;padding:5px 6px;border-bottom:1px solid #eceef2}
.rp-fr .rp-name input{width:170px}.rp-fr input,.rp-fr select{width:86px;min-width:0;height:28px;border:1px solid #e3e5ea;border-radius:7px;padding:0 6px;font:inherit;font-size:12px;font-weight:400;text-align:left;background:#fff;box-sizing:border-box}
.rp-fr input:focus,.rp-fr select:focus{outline:2px solid #ddd6fe;border-color:#8b5cf6}
.rp-clear{border:0;background:#ede9fe;color:#5b21b6;border-radius:6px;width:22px;height:22px;cursor:pointer;font-weight:700}
.rp-link{border:0;background:none;padding:0;font:inherit;font-weight:700;color:#5b21b6;cursor:pointer;text-align:left}.rp-link:hover{text-decoration:underline}
.rp-tag{font-size:10px;font-weight:700;color:#6b7280;background:#f3f4f6;border-radius:5px;padding:1px 5px}
.rp-pen{border:0;background:none;color:#7c3aed;cursor:pointer;font-size:13px;padding:0 2px}.rp-pen:disabled{color:#c4c4c4;cursor:not-allowed}
.rp-out{font-size:12px;font-weight:700;color:#b42323;white-space:nowrap}
.rp-edit{display:inline-flex;gap:4px;align-items:center}.rp-edit input{width:90px;height:30px;border:1px solid #8b5cf6;border-radius:7px;padding:0 6px;font:inherit;text-align:right}
.rp-edit .btn{height:30px;padding:0 9px;font-size:12px}
.rp-table tr.rp-sending td,.rp-table tr.rp-sending th{background:#fff8db!important}
.rp-hist-row>td{background:#faf9fd!important;text-align:left!important;white-space:normal}
.rp-hist-head{display:flex;gap:6px;align-items:center;margin-bottom:8px}.rp-hist-head b{margin-right:8px}
.rp-hist-head button{border:1px solid #e5e7eb;background:#fff;border-radius:7px;padding:3px 10px;font:inherit;font-size:12px;cursor:pointer}.rp-hist-head button.on{border-color:#7c3aed;color:#5b21b6;font-weight:700}
.rp-chart{width:100%;max-width:680px;height:auto;display:block}.rp-ax{font-size:10px;fill:#6b7280}
.rp-l1{fill:none;stroke:#7c3aed;stroke-width:2}.rp-l2{fill:none;stroke:#0ca678;stroke-width:2}.rp-d1 circle{fill:#7c3aed;stroke:#fff;stroke-width:2}.rp-d2 circle{fill:#0ca678;stroke:#fff;stroke-width:2}
.rp-legend{display:flex;gap:14px;font-size:12px;color:#4b5563;margin:4px 0 10px}.rp-legend i{display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:5px;vertical-align:middle}
.rp-k1{background:#7c3aed}.rp-k2{background:#0ca678}
.rp-note{font-size:12px;margin:8px 0}
.rp-bar{position:sticky;bottom:0;z-index:5;display:flex;gap:14px;align-items:center;flex-wrap:wrap;background:#1f2328;color:#fff;border-radius:12px;padding:10px 14px;margin-top:10px;box-shadow:0 8px 24px #0003}
.rp-bar .rv-link{color:#c4b5fd}.rp-bar .btn{margin-left:auto}
.rp-overlay{position:fixed;inset:0;background:#11182766;z-index:1000;display:grid;place-items:center;padding:16px}
.rp-modal{background:#fff;border-radius:16px;width:min(1100px,100%);max-height:92vh;display:flex;flex-direction:column;box-shadow:0 20px 60px #0004;overflow:hidden}
.rp-modal header,.rp-modal footer{display:flex;align-items:center;gap:12px;padding:14px 18px}.rp-modal header{justify-content:space-between;border-bottom:1px solid #eceef2}.rp-modal h3{margin:0;font-size:17px}
.rp-modal footer{border-top:1px solid #eceef2;justify-content:flex-end;flex-wrap:wrap}.rp-modal footer .muted{margin-right:auto;font-size:13px}
.rp-modal>p,.rp-modal>.db-note{margin:10px 18px 0}
.rp-mass{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;padding:14px 18px 0}.rp-mass label{display:grid;gap:4px;font-size:12px;font-weight:600;color:#374151}
.rp-mass select,.rp-mass input{height:38px;border:1px solid #e3e5ea;border-radius:9px;padding:0 10px;font:inherit;min-width:120px;background:#fff}
.rp-modal-table{overflow:auto;padding:12px 18px;flex:1}.rp-modal-table td small{display:block;font-size:11px;color:#6b7280}
.rp-modal-table input{width:96px;height:30px;border:1px solid #e3e5ea;border-radius:7px;padding:0 6px;font:inherit;text-align:right}
.rp-modal-table .neg{color:#b42323}.rp-modal-table .pos{color:#2f6b2f}
.rp-msgs span{display:block;font-size:12px;color:#92400e;max-width:300px}.rp-msgs span.neg{color:#b42323}.rp-msgs span.pos{color:#2f6b2f}
tr.rp-bad td{background:#fdf2f2}tr.rp-warn td{background:#fffbeb}
.rp-lim{width:110px;height:32px;border:1px solid #e3e5ea;border-radius:7px;padding:0 8px;font:inherit;text-align:right}.rp-lim.saved{border-color:#7fbd80;background:#ecfdf0}
.wb-member [data-nav=repricer]{display:none!important}
@media(max-width:800px){.rp-tools{margin-left:0}.rp-filters input[type=search]{min-width:0;width:100%}.rp-mass label{flex:1 1 40%}}`;
  document.head.appendChild(style);

  return {render};
})();
