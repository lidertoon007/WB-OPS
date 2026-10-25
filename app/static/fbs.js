/* WB OPS: «Аналитика FBS» — сводка, лента сборочных заданий, склады, артикулы, скорость отгрузки и скидки/штрафы WB за сроки. */
window.WBOPSFbs = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const num = (v, d = 0) => v == null || v === '' || isNaN(v) ? '—' : Number(v).toLocaleString('ru-RU', {minimumFractionDigits: d, maximumFractionDigits: d});
  const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const parse = s => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const ru = s => s ? s.split('-').reverse().join('.') : '';
  const dt = s => s ? new Date(s).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}) : '—';
  const WD = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
  // Статусы в фиксированном порядке (цвет закреплён за статусом)
  const ST = [['sold', 'Выкуплены', '#2f9e44'], ['pvz', 'В ПВЗ', '#12b886'], ['transit', 'В пути', '#4c6ef5'], ['assembly', 'На сборке', '#7950f2'],
    ['new', 'Новые', '#adb5bd'], ['refused', 'Отказ при получении', '#f08c00'], ['client_cancel', 'Отменены покупателем', '#e8b100'],
    ['seller_cancel', 'Отменены продавцом', '#e03131'], ['defect', 'Брак', '#862e9c']];
  const STC = Object.fromEntries(ST.map(([k, , c]) => [k, c])), STT = Object.fromEntries(ST.map(([k, t]) => [k, t]));
  const BANDS = [['lt13', 'до 13 ч', '−5%', '#2f9e44'], ['13_42', '13–42 ч', '−3,5%', '#8ac926'], ['42_48', '42–48 ч', '0%', '#adb5bd'],
    ['48_54', '48–54 ч', '+0,3%/ч', '#f59f00'], ['54_60', '54–60 ч', '+0,35%/ч', '#f76707'], ['gt60', 'больше 60 ч', '+0,45%/ч', '#e03131']];
  const st = {tab: 'summary', f: {preset: 'days:14', from: '', to: '', cabinet: '', warehouse: '', brand: '', category: '', q: ''},
    data: null, feed: null, fs: {status: '', sort: 'created_at', desc: true, offset: 0}, ws: {sort: 'orders', desc: true}, as: {sort: 'orders', desc: true, q: ''},
    calc: {price: 3000, hours: 30}, lost: {by: 'warehouses', sort: 'lost_rub', desc: true}, poll: null};
  let root = null;
  try { Object.assign(st.f, JSON.parse(localStorage.getItem('wbops.fbs.f') || '{}')); } catch (e) { /* без хранилища */ }
  const remember = () => { try { localStorage.setItem('wbops.fbs.f', JSON.stringify(st.f)); } catch (e) { /* ignore */ } };
  const api = async url => { const r = await fetch(url); const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.detail || 'Ошибка запроса'); return d; };

  function presets() {
    const y = addDays(new Date(), -1), t = new Date(), m0 = new Date(t.getFullYear(), t.getMonth(), 1);
    return [['days:7', 'Последние 7 дней', addDays(t, -6), t], ['days:14', 'Последние 14 дней', addDays(t, -13), t], ['days:30', 'Последние 30 дней', addDays(t, -29), t],
      ['days:90', 'Последние 90 дней', addDays(t, -89), t], ['month:0', 'Текущий месяц', m0, t],
      ['month:1', 'Прошлый месяц', new Date(t.getFullYear(), t.getMonth() - 1, 1), new Date(t.getFullYear(), t.getMonth(), 0)], ['yesterday', 'Вчера', y, y], ['custom', 'Свой период…', null, null]];
  }
  function period() {
    if (st.f.preset === 'custom' && st.f.from && st.f.to) return [st.f.from, st.f.to];
    const p = presets().find(x => x[0] === st.f.preset) || presets()[1];
    return [iso(p[2]), iso(p[3])];
  }
  function query(extra = {}) {
    const [from, to] = period(), q = new URLSearchParams({from, to});
    ['cabinet', 'warehouse', 'brand', 'category', 'q'].forEach(k => st.f[k] && q.set(k, st.f[k]));
    Object.entries(extra).forEach(([k, v]) => v !== '' && v != null && q.set(k, v));
    return q;
  }

  // ---------------------------------------------------------------- каркас
  async function render(section) {
    root = section;
    root.className = 'panel rv-page fb-page';
    root.innerHTML = `<div class="db-head"><h2>Аналитика FBS</h2><span class="muted db-fresh" data-fresh></span></div>
      <div class="db-filters" data-filters></div><div data-notes></div>
      <div class="rnp-tabs fb-tabs" role="tablist">${[['summary', 'Сводка'], ['feed', 'Лента заказов'], ['warehouses', 'Склады'], ['articles', 'Артикулы'], ['calc', 'Штраф за отгрузку'], ['lost', 'Упущенная выгода']]
        .map(([k, t]) => `<button type="button" role="tab" data-tab="${k}">${t}</button>`).join('')}</div>
      <div data-body><p class="muted">Загрузка…</p></div>`;
    root.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => { st.tab = b.dataset.tab; drawBody(); });
    await load();
  }
  async function load() {
    const [from, to] = period(), days = (parse(to) - parse(from)) / 864e5 + 1;
    const q = query({prev_from: iso(addDays(parse(from), -days)), prev_to: iso(addDays(parse(from), -1))});
    try { st.data = await api('/api/fbs-analytics?' + q); }
    catch (e) { root.querySelector('[data-body]').innerHTML = `<p class="rv-error">${esc(e.message)}</p>`; return; }
    st.feed = null;
    drawFilters(); drawNotes(); drawBody();
    clearTimeout(st.poll);
    if (st.data.state?.status === 'running') st.poll = setTimeout(waitSync, 5000);
  }
  async function waitSync() {
    if (!root?.isConnected) return;
    const s = await api('/api/fbs-analytics/status').catch(() => ({}));
    if (s.status === 'running') { const n = root.querySelector('[data-sync-msg]'); if (n) n.textContent = s.message || 'Загружаю…'; st.poll = setTimeout(waitSync, 5000); return; }
    load();
  }

  function drawFilters() {
    const o = st.data.options, f = st.f, [from, to] = period();
    const opt = (v, t, cur) => `<option value="${esc(v)}" ${String(v) === String(cur) ? 'selected' : ''}>${esc(t)}</option>`;
    const whs = o.warehouses.filter(w => !f.cabinet || w.cabinet_id === f.cabinet);
    root.querySelector('[data-filters]').innerHTML = `
      <select data-f="preset" aria-label="Период" class="db-period">${presets().map(([k, t]) => opt(k, t, f.preset)).join('')}</select>
      ${f.preset === 'custom' ? `<label class="db-date">с <input type="date" data-f="from" value="${from}"></label><label class="db-date">по <input type="date" data-f="to" value="${to}"></label>` : ''}
      ${o.cabinets.length > 1 ? `<select data-f="cabinet" aria-label="Кабинет">${opt('', 'Все кабинеты', f.cabinet)}${o.cabinets.map(c => opt(c.id, c.name, f.cabinet)).join('')}</select>` : ''}
      ${whs.length > 1 ? `<select data-f="warehouse" aria-label="Склад">${opt('', 'Все склады', f.warehouse)}${whs.map(w => opt(w.id, w.name, f.warehouse)).join('')}</select>` : ''}
      <select data-f="brand" aria-label="Бренд">${opt('', 'Все бренды', f.brand)}${o.brands.map(b => opt(b, b, f.brand)).join('')}</select>
      <select data-f="category" aria-label="Категория">${opt('', 'Все категории', f.category)}${o.categories.map(b => opt(b, b, f.category)).join('')}</select>
      <input type="search" data-f="q" placeholder="Артикул, задание, поставка" value="${esc(f.q)}" aria-label="Поиск">
      <span class="rp-tools"><button type="button" class="db-ghost" data-sync ${st.data.state?.status === 'running' ? 'disabled' : ''}>↻ ${st.data.state?.status === 'running' ? 'Загружаю…' : 'Обновить из WB'}</button></span>`;
    let t;
    root.querySelectorAll('[data-f]').forEach(el => {
      const h = () => {
        const k = el.dataset.f;
        if (k === 'preset' && el.value === 'custom') { st.f.from = from; st.f.to = to; }
        st.f[k] = el.value; if (k === 'cabinet') st.f.warehouse = ''; st.fs.offset = 0; remember();
        clearTimeout(t); t = setTimeout(load, k === 'q' ? 400 : 0);
      };
      el.type === 'search' ? (el.oninput = h) : (el.onchange = h);
    });
    root.querySelector('[data-sync]').onclick = async e => {
      e.currentTarget.disabled = true;
      try { await fetch('/api/fbs-analytics/sync', {method: 'POST'}).then(r => r.ok ? r : r.json().then(d => { throw new Error(d.detail); })); st.data.state = {status: 'running'}; drawNotes(); st.poll = setTimeout(waitSync, 4000); }
      catch (err) { alert(err.message); e.currentTarget.disabled = false; }
    };
    const s = st.data.synced_at;
    root.querySelector('[data-fresh]').textContent = s ? `Данные WB на ${dt(s)} · обновляются каждые 30 минут` : 'Данные ещё не загружены';
  }
  function drawNotes() {
    const s = st.data.state || {}, notes = [];
    if (s.status === 'running') notes.push(`<span data-sync-msg>${esc(s.message || 'Загружаю сборочные задания из WB…')}</span> Первая загрузка — за 90 дней, это несколько минут.`);
    (s.errors || []).forEach(e => notes.push('Загрузка: ' + esc(e)));
    if (s.status === 'error') notes.push('Не удалось загрузить: ' + esc(s.message));
    if (!st.data.synced_at && s.status !== 'running') notes.push('Нажмите «Обновить из WB». Нужен ключ API с категориями «Маркетплейс» и «Статистика».');
    root.querySelector('[data-notes]').innerHTML = notes.map(n => `<div class="db-note">${n}</div>`).join('');
  }
  function drawBody() {
    root.querySelectorAll('[data-tab]').forEach(b => { b.classList.toggle('active', b.dataset.tab === st.tab); b.setAttribute('aria-selected', String(b.dataset.tab === st.tab)); });
    ({summary: drawSummary, feed: drawFeed, warehouses: drawWarehouses, articles: drawArticles, calc: drawCalc, lost: drawLost}[st.tab])();
  }

  // ---------------------------------------------------------------- сводка
  function tile(title, value, cur, prev, opts = {}) {
    let cls = 'neutral', delta = '';
    if (cur != null && prev != null && !(cur === 0 && prev === 0)) {
      const diff = cur - prev, good = opts.good === 'down' ? diff < 0 : diff > 0;
      cls = Math.abs(diff) < 1e-9 || !opts.good ? 'neutral' : good ? 'good' : 'bad';
      const rel = prev && !opts.pp ? ` <span>(${num(diff / Math.abs(prev) * 100, 1)}%)</span>` : '';
      delta = `<span class="db-delta">${diff > 0 ? '+' : ''}${num(diff, opts.d ?? 0)}${opts.pp ? ' п.п.' : opts.unit ? ' ' + opts.unit : ''}${rel}</span><span class="db-arrow">${diff > 0 ? '↗' : diff < 0 ? '↘' : '→'}</span>`;
    } else if (prev == null && st.data.previous && !opts.fixed) delta = '<span class="db-delta muted">нет данных</span>';
    const len = String(value).replace(/<[^>]+>/g, '').length;
    return `<article class="db-tile ${cls}"><div class="db-title">${esc(title)}${opts.hint ? ` <span class="db-i" tabindex="0" title="${esc(opts.hint)}" aria-label="${esc(opts.hint)}">i</span>` : ''}</div>
      <div class="db-value ${len > 17 ? 'long' : len > 13 ? 'mid' : ''}">${value}</div><div class="db-change">${opts.fixed ? '<span class="db-fixed">на сейчас</span>' : delta}</div></article>`;
  }
  function drawSummary() {
    const s = st.data.summary, p = st.data.previous || {}, has = !!st.data.previous;
    const pv = k => has ? p[k] : null;
    const eff = s.effect_rub;
    const body = root.querySelector('[data-body]');
    if (!s.orders) { body.innerHTML = '<div class="rv-empty"><b>Нет FBS-заказов за период</b><p class="muted">Выберите другой период или нажмите «Обновить из WB».</p></div>'; return; }
    body.innerHTML = `<div class="db-grid fb-grid">
        ${tile('Заказы FBS', `${num(s.orders)}<sup>шт</sup><span class="db-sep">/</span>${num(s.orders_sum)}<sup>₽</sup>`, s.orders, pv('orders'), {good: 'up', hint: 'Все сборочные задания за период по дате заказа'})}
        ${tile('Выкуплено', `${num(s.sold)}<sup>шт</sup><span class="db-sep">/</span>${num(s.sold_sum)}<sup>₽</sup>`, s.sold, pv('sold'), {good: 'up'})}
        ${tile('Процент выкупа', `${num(s.buyout_pct, 1)}<sup>%</sup>`, s.buyout_pct, pv('buyout_pct'), {good: 'up', pp: true, d: 1, hint: 'Выкуплено / (выкуплено + отказ при получении). Заказы в пути не учитываются'})}
        ${tile('Отмены', `${num(s.cancel_pct, 1)}<sup>%</sup>`, s.cancel_pct, pv('cancel_pct'), {good: 'down', pp: true, d: 1, hint: 'Отмены продавцом и покупателем к числу заказов'})}
        ${tile('В работе', `${num(s.in_progress)}<sup>шт</sup>`, null, null, {fixed: true, hint: 'Заказы периода, которые сейчас новые, на сборке, в пути или в ПВЗ'})}
        ${tile('Среднее время до СЦ', `${num(s.avg_hours_to_sc, 1)}<sup>ч</sup>`, s.avg_hours_to_sc, pv('avg_hours_to_sc'), {good: 'down', d: 1, unit: 'ч', hint: 'От заказа до приёмки поставки на складе WB (скан поставки)'})}
        ${tile('От СЦ до выкупа', `${num(s.avg_days_to_buyout, 1)}<sup>дн</sup>`, s.avg_days_to_buyout, pv('avg_days_to_buyout'), {good: 'down', d: 1, unit: 'дн', hint: 'От приёмки WB до выкупа покупателем (дата продажи из отчёта «Продажи»)'})}
        ${tile('Отгружено до 13 ч', `${num(s.fast_pct, 1)}<sup>%</sup>`, s.fast_pct, pv('fast_pct'), {good: 'up', pp: true, d: 1, hint: 'Доля заданий, принятых WB быстрее 13 часов — скидка 5% к комиссии'})}
        ${tile('Отгружено до 48 ч', `${num(s.on_time_pct, 1)}<sup>%</sup>`, s.on_time_pct, pv('on_time_pct'), {good: 'up', pp: true, d: 1, hint: 'После 48 часов WB начисляет прибавку к комиссии за каждый час'})}
        ${tile('Скидка / штраф к комиссии', eff == null ? '—' : `${eff < 0 ? '−' : '+'}${num(Math.abs(eff))}<sup>₽</sup>`, eff, pv('effect_rub'), {good: 'down', unit: '₽', hint: 'Оценка по правилам WB с 31.08.2026 от суммы заказа: минус — экономия на комиссии, плюс — штраф'})}
      </div>
      <section class="tr-box"><h3>Заказы по статусам</h3><div class="fb-status">${ST.filter(([k]) => s.statuses[k]).map(([k, t, c]) =>
        `<button type="button" class="fb-st" data-goto="${k}"><i style="background:${c}"></i>${t} <b>${num(s.statuses[k])}</b><span>${num(s.statuses[k] / s.orders * 100, 1)}%</span></button>`).join('')}
        ${s.late ? `<button type="button" class="fb-st late" data-goto="late"><i></i>Отгружены позже 48 ч <b>${num(s.late)}</b></button>` : ''}</div>
        ${barsByDay()}</section>
      <div class="fb-two">
        <section class="tr-box"><h3>Скорость отгрузки</h3><p class="muted fb-sub">От заказа до приёмки WB · ${num(s.shipped)} заданий</p>${bandsBar(s)}</section>
        <section class="tr-box"><h3>По дням недели</h3><p class="muted fb-sub">День недели — по дате заказа</p>${weekdays()}</section>
      </div>
      <div class="fb-two">
        <section class="tr-box"><h3>Процент выкупа по дням</h3>${lineChart(st.data.by_day, 'buyout_pct', '%', '#2f9e44')}</section>
        <section class="tr-box"><h3>Среднее время до СЦ по дням, ч</h3>${lineChart(st.data.by_day, 'avg_hours_to_sc', 'ч', '#7c3aed', 48)}</section>
      </div>`;
    body.querySelectorAll('[data-goto]').forEach(b => b.onclick = () => { st.fs.status = b.dataset.goto; st.fs.offset = 0; st.tab = 'feed'; drawBody(); });
  }
  function barsByDay() {
    const days = st.data.by_day, max = Math.max(1, ...days.map(d => d.orders));
    const W = Math.max(1000, days.length * 22), H = 220, P = 26, bw = (W - P * 2) / days.length;
    const bars = days.map((d, i) => {
      let y = H - 22;
      const segs = ST.map(([k, t, c]) => {
        const v = d.statuses[k] || 0; if (!v) return '';
        const h = v / max * (H - 44); y -= h;
        return `<rect x="${P + i * bw + 2}" y="${y + 1}" width="${Math.max(2, bw - 4)}" height="${Math.max(0, h - 1)}" rx="2" fill="${c}"><title>${ru(d.day)} · ${t}: ${v}</title></rect>`;
      }).join('');
      const label = days.length <= 16 || i % Math.ceil(days.length / 16) === 0 ? `<text x="${P + i * bw + bw / 2}" y="${H - 6}" class="fb-ax" text-anchor="middle">${ru(d.day).slice(0, 5)}</text>` : '';
      return segs + label + (d.orders ? `<rect x="${P + i * bw}" y="${P}" width="${bw}" height="${H - P - 22}" fill="transparent"><title>${ru(d.day)}: ${d.orders} заказов${d.buyout_pct != null ? `, выкуп ${num(d.buyout_pct, 1)}%` : ''}</title></rect>` : '');
    }).join('');
    return `<div class="fb-chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Заказы по дням и статусам"><text x="${P}" y="14" class="fb-ax">${num(max)} шт</text>
      <line x1="${P}" x2="${W - P}" y1="${H - 22}" y2="${H - 22}" stroke="#e5e7eb"/>${bars}</svg></div>`;
  }
  function bandsBar(s) {
    const total = s.shipped || 0;
    if (!total) return '<p class="muted">Нет принятых WB поставок за период.</p>';
    return `<div class="fb-bands" role="img" aria-label="Распределение по времени отгрузки">${BANDS.map(([k, t, r, c]) => {
      const v = s.bands[k] || 0; return v ? `<span style="width:${v / total * 100}%;background:${c}" title="${t}: ${v} (${num(v / total * 100, 1)}%) · комиссия ${r}"></span>` : ''; }).join('')}</div>
      <table class="fb-mini">${BANDS.map(([k, t, r, c]) => `<tr><td><i style="background:${c}"></i>${t}</td><td class="r"><b>${num(s.bands[k] || 0)}</b></td>
        <td class="r muted">${num((s.bands[k] || 0) / total * 100, 1)}%</td><td class="r ${r.startsWith('−') ? 'pos' : r.startsWith('+') ? 'neg' : 'muted'}">${r}</td></tr>`).join('')}</table>`;
  }
  function weekdays() {
    const w = st.data.weekdays, max = Math.max(1, ...w.map(x => x.avg_hours_to_sc || 0));
    return `<table class="fb-mini fb-wd"><tr><th></th><th class="r">Заказов</th><th>Время до СЦ</th><th class="r">Выкуп</th></tr>${w.map(x => `<tr><td><b>${WD[x.weekday]}</b></td><td class="r">${num(x.orders)}</td>
      <td><span class="fb-hbar"><i style="width:${(x.avg_hours_to_sc || 0) / max * 100}%;background:${x.avg_hours_to_sc >= 48 ? '#e03131' : x.avg_hours_to_sc >= 42 ? '#adb5bd' : x.avg_hours_to_sc >= 13 ? '#8ac926' : '#2f9e44'}"></i></span> ${num(x.avg_hours_to_sc, 1)} ч</td>
      <td class="r">${x.buyout_pct == null ? '—' : num(x.buyout_pct, 1) + '%'}</td></tr>`).join('')}</table>`;
  }
  function lineChart(days, key, unit, color, mark) {
    const pts = days.map((d, i) => [i, d[key]]).filter(p => p[1] != null);
    if (pts.length < 2) return '<p class="muted">Недостаточно данных.</p>';
    const W = 560, H = 170, P = 30, vals = pts.map(p => p[1]).concat(mark ? [mark] : []);
    const lo = Math.min(...vals) * 0.95, hi = Math.max(...vals) * 1.05 || 1;
    const X = i => P + i / Math.max(1, days.length - 1) * (W - 2 * P), Y = v => H - 22 - (v - lo) / Math.max(1e-9, hi - lo) * (H - 40);
    const path = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(' ');
    return `<svg class="fb-line" viewBox="0 0 ${W} ${H}" role="img" aria-label="График"><text x="${P}" y="12" class="fb-ax">${num(hi, 0)} ${unit}</text><text x="${P}" y="${H - 6}" class="fb-ax">${num(lo, 0)} ${unit}</text>
      ${mark && mark >= lo && mark <= hi ? `<line x1="${P}" x2="${W - P}" y1="${Y(mark)}" y2="${Y(mark)}" stroke="#e03131" stroke-dasharray="4 4"/><text x="${W - P}" y="${Y(mark) - 4}" class="fb-ax" text-anchor="end">${mark} ${unit} — штраф</text>` : ''}
      <path d="${path}" fill="none" stroke="${color}" stroke-width="2"/>${pts.map(p => `<circle cx="${X(p[0])}" cy="${Y(p[1])}" r="3.5" fill="${color}" stroke="#fff" stroke-width="1.5"><title>${ru(days[p[0]].day)}: ${num(p[1], 1)} ${unit}</title></circle>`).join('')}</svg>`;
  }

  // ---------------------------------------------------------------- лента заказов
  async function drawFeed() {
    const body = root.querySelector('[data-body]'), fs = st.fs;
    body.innerHTML = '<p class="muted">Загрузка…</p>';
    let d;
    try { d = await api('/api/fbs-analytics/orders?' + query({status: fs.status, sort: fs.sort, desc: fs.desc, offset: fs.offset, limit: 100})); }
    catch (e) { body.innerHTML = `<p class="rv-error">${esc(e.message)}</p>`; return; }
    if (st.tab !== 'feed') return;
    const total = Object.values(d.counts).reduce((a, b) => a + b, 0);
    const th = (k, t, cls = '') => `<th class="${cls}"><button type="button" class="rp-sort ${fs.sort === k ? 'on' : ''}" data-fsort="${k}">${t}<span>${fs.sort === k ? (fs.desc ? '↓' : '↑') : '↕'}</span></button></th>`;
    body.innerHTML = `<div class="sk-chips">
        <button type="button" class="sk-chip ${!fs.status ? 'on' : ''}" data-fst="">Все <b>${num(total)}</b></button>
        ${ST.filter(([k]) => d.counts[k]).map(([k, t, c]) => `<button type="button" class="sk-chip ${fs.status === k ? 'on' : ''}" data-fst="${k}"><i class="fb-dot" style="background:${c}"></i>${t} <b>${num(d.counts[k])}</b></button>`).join('')}
        <button type="button" class="sk-chip ${fs.status === 'late' ? 'on' : ''}" data-fst="late">Отгружены позже 48 ч</button>
        <a class="db-ghost fb-csv" href="/api/fbs-analytics/orders.csv?${query({status: fs.status})}" download>⇩ CSV</a></div>
      <div class="rnp-scroll"><table class="rnp-table fb-feed"><thead><tr>${th('created_at', 'Дата заказа', 'l')}<th class="l">Задание</th>${th('article', 'Артикул', 'l')}${th('warehouse', 'Склад', 'l')}
        ${th('price', 'Сумма')}<th class="l">Поставка</th><th class="l">Статус</th>${th('hours_to_sc', 'До СЦ')}<th>До выкупа</th>${th('effect_rub', 'Комиссия')}</tr></thead><tbody>
      ${d.items.map(r => `<tr><td class="l">${dt(r.created_at)}</td><td class="l">${r.id}<small>${esc(r.rid).slice(0, 18)}</small></td>
        <td class="l"><b>${esc(r.article)}</b><small>${r.nm_id}${r.brand ? ' · ' + esc(r.brand) : ''}</small></td><td class="l">${esc(r.warehouse)}${st.data.options.cabinets.length > 1 ? `<small>${esc(r.cabinet)}</small>` : ''}</td>
        <td>${num(r.price)} ₽</td><td class="l">${esc(r.supply_id) || '—'}</td>
        <td class="l"><span class="fb-chip" style="--c:${STC[r.status]}">${STT[r.status] || r.status}</span></td>
        <td>${r.hours_to_sc == null ? '—' : `<span class="fb-h" style="color:${(BANDS.find(b => b[0] === r.band) || [])[3]}">${num(r.hours_to_sc, 1)} ч</span>`}</td>
        <td>${r.days_to_buyout == null ? '—' : num(r.days_to_buyout, 1) + ' дн'}</td>
        <td class="${r.effect_pct == null ? '' : r.effect_pct < 0 ? 'pos' : r.effect_pct > 0 ? 'neg' : ''}">${r.effect_pct == null ? '—' : `${r.effect_pct > 0 ? '+' : ''}${num(r.effect_pct, 2)}%<small>${r.effect_rub > 0 ? '+' : ''}${num(r.effect_rub)} ₽</small>`}</td></tr>`).join('')
        || '<tr><td colspan="10" class="muted l">Нет заказов.</td></tr>'}</tbody></table></div>
      <div class="fb-pager"><button type="button" class="db-ghost" data-page="-1" ${fs.offset ? '' : 'disabled'}>← Назад</button>
        <span class="muted">${d.total ? `${fs.offset + 1}–${Math.min(fs.offset + 100, d.total)} из ${num(d.total)}` : ''}</span>
        <button type="button" class="db-ghost" data-page="1" ${fs.offset + 100 < d.total ? '' : 'disabled'}>Вперёд →</button></div>`;
    body.querySelectorAll('[data-fst]').forEach(b => b.onclick = () => { fs.status = b.dataset.fst; fs.offset = 0; drawFeed(); });
    body.querySelectorAll('[data-fsort]').forEach(b => b.onclick = () => { if (fs.sort === b.dataset.fsort) fs.desc = !fs.desc; else { fs.sort = b.dataset.fsort; fs.desc = true; } fs.offset = 0; drawFeed(); });
    body.querySelectorAll('[data-page]').forEach(b => b.onclick = () => { fs.offset = Math.max(0, fs.offset + Number(b.dataset.page) * 100); drawFeed(); });
  }

  // ---------------------------------------------------------------- склады и артикулы
  function groupTable(rows, cols, state, redraw) {
    const sorted = rows.slice().sort((a, b) => { const x = a[state.sort], y = b[state.sort];
      if (typeof x === 'string') return (state.desc ? -1 : 1) * x.localeCompare(y, 'ru', {numeric: true});
      if (x == null) return 1; if (y == null) return -1; return (state.desc ? -1 : 1) * (x - y); });
    const th = (k, t, cls = '') => `<th class="${cls}"><button type="button" class="rp-sort ${state.sort === k ? 'on' : ''}" data-gsort="${k}">${t}<span>${state.sort === k ? (state.desc ? '↓' : '↑') : '↕'}</span></button></th>`;
    const heat = v => v == null ? '' : v >= 48 ? 'fb-hot' : v >= 42 ? 'fb-mid' : v < 13 ? 'fb-cool' : '';
    const html = `<div class="rnp-scroll"><table class="rnp-table fb-groups"><thead><tr>${cols.map(([k, t, cls]) => th(k, t, cls)).join('')}
        ${th('orders', 'Заказов')}${th('orders_sum', 'Сумма, ₽')}${th('buyout_pct', '% выкупа')}${th('cancel_pct', 'Отмены')}${th('avg_hours_to_sc', 'До СЦ, ч')}<th class="l">Скорость отгрузки</th>
        ${th('avg_days_to_buyout', 'До выкупа, дн')}${th('late', '> 48 ч')}${th('effect_rub', 'Комиссия, ₽')}</tr></thead><tbody>
      ${sorted.slice(0, 300).map(r => `<tr>${cols.map(([k, , cls, fmt]) => `<td class="${cls || ''}">${fmt ? fmt(r) : esc(r[k])}</td>`).join('')}
        <td>${num(r.orders)}</td><td>${num(r.orders_sum)}</td><td>${r.buyout_pct == null ? '—' : num(r.buyout_pct, 1) + '%'}</td><td>${r.cancel_pct == null ? '—' : num(r.cancel_pct, 1) + '%'}</td>
        <td class="${heat(r.avg_hours_to_sc)}">${num(r.avg_hours_to_sc, 1)}</td>
        <td class="l"><div class="fb-bands sm">${BANDS.map(([k, t, , c]) => r.bands[k] ? `<span style="width:${r.bands[k] / r.shipped * 100}%;background:${c}" title="${t}: ${r.bands[k]}"></span>` : '').join('')}</div></td>
        <td>${num(r.avg_days_to_buyout, 1)}</td><td class="${r.late ? 'neg' : ''}">${num(r.late)}</td>
        <td class="${r.effect_rub == null ? '' : r.effect_rub < 0 ? 'pos' : r.effect_rub > 0 ? 'neg' : ''}">${r.effect_rub == null ? '—' : (r.effect_rub > 0 ? '+' : '') + num(r.effect_rub)}</td></tr>`).join('')
        || `<tr><td colspan="${cols.length + 9}" class="muted l">Нет данных.</td></tr>`}</tbody></table></div>
      <p class="muted rp-note">Цвет «До СЦ»: зелёный — до 13 ч (скидка 5%), серый — 42–48 ч (без скидки), красный — 48 ч и больше (штраф). Комиссия: минус — экономия, плюс — штраф (оценка).</p>`;
    return {html, bind: body => body.querySelectorAll('[data-gsort]').forEach(b => b.onclick = () => { if (state.sort === b.dataset.gsort) state.desc = !state.desc; else { state.sort = b.dataset.gsort; state.desc = true; } redraw(); })};
  }
  function drawWarehouses() {
    const body = root.querySelector('[data-body]');
    const t = groupTable(st.data.warehouses, [['warehouse', 'Склад', 'l', r => `<b>${esc(r.warehouse)}</b>${st.data.options.cabinets.length > 1 ? `<small>${esc(r.cabinet)}</small>` : ''}`]], st.ws, drawWarehouses);
    body.innerHTML = t.html; t.bind(body);
  }
  function drawArticles() {
    const body = root.querySelector('[data-body]'), q = st.as.q.toLowerCase();
    const rows = st.data.articles.filter(r => !q || `${r.article} ${r.nm_id} ${r.title}`.toLowerCase().includes(q));
    const t = groupTable(rows, [['article', 'Артикул', 'l', r => `<b>${esc(r.article)}</b><small>${esc(r.title || '')}${r.title ? ' · ' : ''}${r.nm_id}</small>`]], st.as, drawArticles);
    body.innerHTML = `<div class="rnp-tools"><input type="search" data-aq placeholder="Найти артикул" value="${esc(st.as.q)}"><span class="muted">${rows.length} артикулов</span></div>` + t.html;
    t.bind(body);
    const s = body.querySelector('[data-aq]');
    s.oninput = () => { st.as.q = s.value; const pos = s.selectionStart; drawArticles(); const n = root.querySelector('[data-aq]'); n.focus(); n.setSelectionRange(pos, pos); };
  }

  // ---------------------------------------------------------------- калькулятор
  function effect(h) {
    if (h < 13) return -5; if (h < 42) return -3.5; if (h < 48) return 0;
    return (h < 54 ? 0.3 : h < 60 ? 0.35 : 0.45) * (h - 48);
  }
  function drawCalc() {
    const c = st.calc, body = root.querySelector('[data-body]');
    const e = effect(Number(c.hours) || 0), rub = (Number(c.price) || 0) * e / 100;
    body.innerHTML = `<div class="fb-two"><section class="tr-box"><h3>Будет ли штраф за долгую отгрузку</h3>
        <form class="fb-calc" data-calc><label>Цена товара, ₽<input name="price" type="number" min="0" step="1" value="${esc(c.price)}"></label>
          <label>Время от заказа до отгрузки, ч<input name="hours" type="number" min="0" step="0.5" value="${esc(c.hours)}"></label></form>
        <div class="fb-calc-res ${e < 0 ? 'good' : e > 0 ? 'bad' : ''}"><span>${e < 0 ? 'Скидка к комиссии' : e > 0 ? 'Штраф к комиссии' : 'Комиссия без изменений'}</span>
          <b>${e > 0 ? '+' : e < 0 ? '−' : ''}${num(Math.abs(e), 2)}%</b><span>${e === 0 ? '0 ₽' : `${e > 0 ? '+' : '−'}${num(Math.abs(rub))} ₽ ${e > 0 ? 'дополнительно' : 'экономии'}`}</span></div></section>
      <section class="tr-box"><h3>Правила Wildberries с 31.08.2026</h3><table class="fb-mini">${BANDS.map(([k, t, r, col]) => `<tr><td><i style="background:${col}"></i>${t}</td><td class="r ${r.startsWith('−') ? 'pos' : r.startsWith('+') ? 'neg' : 'muted'}"><b>${r}</b></td></tr>`).join('')}</table>
        <p class="muted rp-note">После 48 часов прибавка начисляется за каждый час, начиная с 48-го. Время отгрузки — от заказа до приёмки поставки WB.</p></section></div>`;
    const f = body.querySelector('[data-calc]');
    f.oninput = e2 => { st.calc[e2.target.name] = e2.target.value; const n = e2.target.name, pos = e2.target.selectionStart; drawCalc();
      const el = root.querySelector(`[data-calc] [name=${n}]`); el.focus(); try { el.setSelectionRange(pos, pos); } catch (er) { /* number */ } };
  }

  // ---------------------------------------------------------------- упущенная выгода
  const rub = v => v == null ? '—' : `${num(Math.abs(v))}<sup>₽</sup>`;
  function drawLost() {
    const s = st.data.summary, p = st.data.previous || {}, has = !!st.data.previous, body = root.querySelector('[data-body]');
    const pv = k => has ? p[k] : null;
    if (!s.shipped) { body.innerHTML = '<div class="rv-empty"><b>Нет принятых WB поставок за период</b><p class="muted">Упущенная выгода считается по заданиям, которые WB уже принял на складе.</p></div>'; return; }
    if (!('lost_rub' in s)) { body.innerHTML = '<div class="rv-empty"><b>Сервер панели работает на старой версии</b><p class="muted">Закройте и снова запустите панель WB OPS — после перезапуска здесь появится расчёт упущенной выгоды.</p></div>'; return; }
    const days = st.data.days.length, month = s.lost_rub / days * 30;
    const share = s.lost_rub && s.orders_sum ? s.lost_rub / s.orders_sum * 100 : 0;
    body.innerHTML = `<div class="fb-lost-hero ${s.lost_rub > 0 ? '' : 'ok'}">
        <div><span>Упущенная выгода за период</span><b>${s.lost_rub > 0 ? num(s.lost_rub) + ' ₽' : '0 ₽'}</b>
          <p>${s.lost_rub > 0 ? `Столько вы бы сэкономили на комиссии WB, если бы все ${num(s.shipped)} заданий принимались на складе быстрее 13 часов.` : 'Все задания периода отгружены быстрее 13 часов — максимальная скидка 5% получена.'}</p></div>
        ${s.lost_rub > 0 ? `<div class="fb-lost-side"><span>≈ в месяц при таком темпе</span><b>${num(month)} ₽</b><span>${num(share, 2)}% от суммы заказов</span></div>` : ''}
      </div>
      <div class="db-grid fb-grid">
        ${tile('Упущенная выгода', rub(s.lost_rub), s.lost_rub, pv('lost_rub'), {good: 'down', unit: '₽', hint: 'Разница между фактической скидкой/штрафом и скидкой 5%, если бы все задания были приняты WB до 13 ч'})}
        ${tile('Недополучено скидки', rub(s.lost_discount), s.lost_discount, pv('lost_discount'), {good: 'down', unit: '₽', hint: 'Задания 13–48 ч: получили −3,5% или 0% вместо −5%, плюс 5% у опоздавших после 48 ч'})}
        ${tile('Штрафы за опоздание', rub(s.lost_penalty), s.lost_penalty, pv('lost_penalty'), {good: 'down', unit: '₽', hint: 'Прибавка к комиссии за задания, принятые позже 48 ч'})}
        ${tile('Заданий позже 13 ч', `${num(s.lost_orders)}<sup>шт</sup><span class="db-sep">/</span>${num(s.lost_pct, 1)}<sup>%</sup>`, s.lost_pct, pv('lost_pct'), {good: 'down', pp: true, d: 1, hint: 'Сколько принятых WB заданий отгружено медленнее 13 часов'})}
        ${tile('Среднее время у медленных', `${num(s.avg_hours_slow, 1)}<sup>ч</sup>`, s.avg_hours_slow, pv('avg_hours_slow'), {good: 'down', d: 1, unit: 'ч', hint: 'Среднее время от заказа до приёмки WB у заданий позже 13 ч'})}
      </div>
      <div class="fb-two">
        <section class="tr-box"><h3>Сейчас и если бы до 13 ч</h3><p class="muted fb-sub">Эффект на комиссию WB по ${num(s.shipped)} принятым заданиям</p>
          <table class="fb-mini fb-cmp"><tr><td>Фактически</td><td class="r ${s.effect_rub < 0 ? 'pos' : s.effect_rub > 0 ? 'neg' : ''}"><b>${s.effect_rub > 0 ? '+' : s.effect_rub < 0 ? '−' : ''}${num(Math.abs(s.effect_rub || 0))} ₽</b></td><td class="muted">${s.effect_rub <= 0 ? 'экономия' : 'переплата'}</td></tr>
            <tr><td>Если бы все до 13 ч</td><td class="r pos"><b>−${num(Math.abs(s.potential_rub))} ₽</b></td><td class="muted">экономия 5%</td></tr>
            <tr class="fb-cmp-total"><td>Разница</td><td class="r neg"><b>${num(s.lost_rub)} ₽</b></td><td class="muted">упущено</td></tr></table>
          ${lostSplit(s)}</section>
        <section class="tr-box"><h3>Откуда потери</h3><p class="muted fb-sub">По времени от заказа до приёмки WB</p>${lostBands(s)}</section>
      </div>
      <section class="tr-box"><h3>Упущенная выгода по дням</h3><p class="muted fb-sub">День — по дате заказа · <i class="fb-dot" style="background:#f59f00"></i>недополученная скидка <i class="fb-dot" style="background:#e03131;margin-left:10px"></i>штрафы</p>${lostByDay()}</section>
      <section class="tr-box"><div class="fb-lost-head"><h3>Где теряем больше всего</h3>
        <div class="rnp-tabs fb-mini-tabs">${[['warehouses', 'Склады'], ['articles', 'Артикулы']].map(([k, t]) => `<button type="button" data-lby="${k}" class="${st.lost.by === k ? 'active' : ''}">${t}</button>`).join('')}</div></div>
        ${lostTable()}</section>
      <p class="muted rp-note">Расчёт по правилам WB с 31.08.2026 от цены заказа: до 13 ч — скидка 5% к комиссии, 13–42 ч — 3,5%, 42–48 ч — без изменений, после 48 ч — прибавка за каждый час.
        Упущенная выгода = (фактический эффект − (−5%)) × цена. Задания без приёмки WB (в сборке, отменённые до отгрузки) не учитываются. Это оценка — точные суммы смотрите в отчёте о реализации.</p>`;
    body.querySelectorAll('[data-lby]').forEach(b => b.onclick = () => { st.lost.by = b.dataset.lby; drawLost(); });
    body.querySelectorAll('[data-lsort]').forEach(b => b.onclick = () => { const k = b.dataset.lsort; if (st.lost.sort === k) st.lost.desc = !st.lost.desc; else { st.lost.sort = k; st.lost.desc = true; } drawLost(); });
  }
  function lostSplit(s) {
    if (!s.lost_rub) return '';
    const d = s.lost_discount / s.lost_rub * 100;
    return `<div class="fb-bands fb-split"><span style="width:${d}%;background:#f59f00" title="Недополученная скидка: ${num(s.lost_discount)} ₽"></span><span style="width:${100 - d}%;background:#e03131" title="Штрафы: ${num(s.lost_penalty)} ₽"></span></div>
      <table class="fb-mini"><tr><td><i style="background:#f59f00"></i>Недополученная скидка</td><td class="r"><b>${num(s.lost_discount)} ₽</b></td><td class="r muted">${num(d, 0)}%</td></tr>
      <tr><td><i style="background:#e03131"></i>Штрафы после 48 ч</td><td class="r"><b>${num(s.lost_penalty)} ₽</b></td><td class="r muted">${num(100 - d, 0)}%</td></tr></table>`;
  }
  function lostBands(s) {
    const lb = s.lost_bands || {}, rows = BANDS.filter(([k]) => k !== 'lt13'), max = Math.max(1, ...rows.map(([k]) => (lb[k] || {}).lost || 0));
    return `<table class="fb-mini fb-lb"><tr><th>Время до СЦ</th><th class="r">Заданий</th><th class="r">Сумма заказов</th><th>Упущено</th></tr>
      <tr class="muted"><td><i style="background:#2f9e44"></i>до 13 ч</td><td class="r">${num(s.bands.lt13 || 0)}</td><td class="r">—</td><td>0 ₽ · скидка 5% получена</td></tr>
      ${rows.map(([k, t, r, c]) => { const v = lb[k] || {orders: 0, sum: 0, lost: 0};
        return `<tr><td><i style="background:${c}"></i>${t} <span class="muted">${r}</span></td><td class="r">${num(v.orders)}</td><td class="r">${num(v.sum)} ₽</td>
          <td><span class="fb-hbar"><i style="width:${v.lost / max * 100}%;background:${c}"></i></span> <b>${num(v.lost)} ₽</b></td></tr>`; }).join('')}</table>`;
  }
  function lostByDay() {
    const days = st.data.by_day, tot = d => (d.lost_discount || 0) + (d.lost_penalty || 0), max = Math.max(1, ...days.map(tot));
    if (!days.some(tot)) return '<p class="muted">Потерь нет.</p>';
    const W = Math.max(1000, days.length * 22), H = 200, P = 26, bw = (W - P * 2) / days.length;
    const bars = days.map((d, i) => {
      const h1 = (d.lost_discount || 0) / max * (H - 44), h2 = (d.lost_penalty || 0) / max * (H - 44), x = P + i * bw + 2, w = Math.max(2, bw - 4), base = H - 22;
      const label = days.length <= 16 || i % Math.ceil(days.length / 16) === 0 ? `<text x="${P + i * bw + bw / 2}" y="${H - 6}" class="fb-ax" text-anchor="middle">${ru(d.day).slice(0, 5)}</text>` : '';
      return (h1 ? `<rect x="${x}" y="${base - h1}" width="${w}" height="${h1}" rx="2" fill="#f59f00"/>` : '') +
        (h2 ? `<rect x="${x}" y="${base - h1 - h2}" width="${w}" height="${Math.max(0, h2 - 2)}" rx="2" fill="#e03131"/>` : '') + label +
        (tot(d) ? `<rect x="${P + i * bw}" y="${P}" width="${bw}" height="${H - P - 22}" fill="transparent"><title>${ru(d.day)}: упущено ${num(tot(d))} ₽ (скидка ${num(d.lost_discount)} ₽, штрафы ${num(d.lost_penalty)} ₽)</title></rect>` : '');
    }).join('');
    return `<div class="fb-chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Упущенная выгода по дням"><text x="${P}" y="14" class="fb-ax">${num(max)} ₽</text>
      <line x1="${P}" x2="${W - P}" y1="${H - 22}" y2="${H - 22}" stroke="#e5e7eb"/>${bars}</svg></div>`;
  }
  function lostTable() {
    const isW = st.lost.by === 'warehouses', L = st.lost;
    const rows = st.data[st.lost.by].filter(r => r.shipped).sort((a, b) => { const x = a[L.sort], y = b[L.sort];
      if (x == null) return 1; if (y == null) return -1; return (L.desc ? -1 : 1) * (x - y); });
    const total = st.data.summary.lost_rub || 0;
    const th = (k, t) => `<th><button type="button" class="rp-sort ${L.sort === k ? 'on' : ''}" data-lsort="${k}">${t}<span>${L.sort === k ? (L.desc ? '↓' : '↑') : '↕'}</span></button></th>`;
    return `<div class="rnp-scroll"><table class="rnp-table fb-groups"><thead><tr><th class="l">${isW ? 'Склад' : 'Артикул'}</th>${th('shipped', 'Принято WB')}${th('lost_orders', 'Позже 13 ч')}
      ${th('lost_pct', 'Доля, %')}${th('avg_hours_to_sc', 'До СЦ, ч')}${th('lost_discount', 'Недополучено скидки, ₽')}${th('lost_penalty', 'Штрафы, ₽')}${th('lost_rub', 'Упущено, ₽')}<th class="l">Доля потерь</th></tr></thead><tbody>
      ${rows.slice(0, 200).map(r => `<tr><td class="l">${isW ? `<b>${esc(r.warehouse)}</b>${st.data.options.cabinets.length > 1 ? `<small>${esc(r.cabinet)}</small>` : ''}` : `<b>${esc(r.article)}</b><small>${esc(r.title || '')}${r.title ? ' · ' : ''}${r.nm_id}</small>`}</td>
        <td>${num(r.shipped)}</td><td>${num(r.lost_orders)}</td><td>${num(r.lost_pct, 1)}</td><td class="${r.avg_hours_to_sc >= 48 ? 'fb-hot' : r.avg_hours_to_sc < 13 ? 'fb-cool' : ''}">${num(r.avg_hours_to_sc, 1)}</td>
        <td>${num(r.lost_discount)}</td><td class="${r.lost_penalty ? 'neg' : ''}">${num(r.lost_penalty)}</td><td><b>${num(r.lost_rub)}</b></td>
        <td class="l"><span class="fb-hbar"><i style="width:${total ? r.lost_rub / total * 100 : 0}%;background:#e8590c"></i></span> <span class="muted">${total ? num(r.lost_rub / total * 100, 1) : 0}%</span></td></tr>`).join('')
        || '<tr><td colspan="9" class="muted l">Нет данных.</td></tr>'}</tbody></table></div>`;
  }

  const style = document.createElement('style');
  style.textContent = `
.fb-tabs{margin:4px 0 14px;width:max-content;max-width:100%}
.fb-grid{grid-template-columns:repeat(5,minmax(0,1fr));margin-bottom:14px}
.fb-status{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.fb-st{display:inline-flex;align-items:center;gap:7px;border:1px solid #e5e7eb;background:#fff;border-radius:10px;padding:7px 11px;font:inherit;font-size:13px;cursor:pointer}
.fb-st:hover{border-color:#8b5cf6}.fb-st i{width:10px;height:10px;border-radius:3px;display:inline-block}.fb-st b{font-variant-numeric:tabular-nums}.fb-st span{color:#6b7280;font-size:12px}
.fb-st.late i{background:#e03131}
.fb-chart{overflow-x:auto}.fb-chart svg{width:100%;min-width:560px;height:auto;display:block}.fb-ax{font-size:10px;fill:#6b7280}
.fb-line{width:100%;height:auto;display:block}
.fb-two{display:grid;grid-template-columns:1fr 1fr;gap:14px}.fb-two .tr-box{margin:0 0 14px}.fb-sub{font-size:12px;margin:-4px 0 10px}
.fb-bands{display:flex;height:22px;border-radius:7px;overflow:hidden;background:#f1f2f4;gap:2px;margin-bottom:10px}.fb-bands span{display:block;height:100%}
.fb-bands.sm{height:10px;width:150px;margin:0;border-radius:4px;gap:1px}
.fb-mini{width:100%;border-collapse:collapse;font-size:13px}.fb-mini td,.fb-mini th{padding:5px 6px;border-bottom:1px solid #f1f2f4;text-align:left;font-weight:500}
.fb-mini th{font-size:12px;color:#6b7280;text-transform:none;letter-spacing:normal}.fb-mini .r{text-align:right}.fb-mini i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:7px;vertical-align:-1px}
.fb-mini .pos,.fb-groups .pos,.fb-feed .pos{color:#2f6b2f}.fb-mini .neg,.fb-groups .neg,.fb-feed .neg{color:#b42323}
.fb-hbar{display:inline-block;width:110px;height:8px;background:#f1f2f4;border-radius:4px;vertical-align:middle;overflow:hidden}.fb-hbar i{display:block;height:100%;margin:0;border-radius:4px}
.fb-feed td,.fb-feed th,.fb-groups td,.fb-groups th{text-align:right}.fb-feed .l,.fb-groups .l{text-align:left}
.fb-feed td small,.fb-groups td small{display:block;font-size:11px;color:#6b7280;font-weight:400}
.fb-chip{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;white-space:nowrap}.fb-chip::before{content:'';width:8px;height:8px;border-radius:50%;background:var(--c)}
.fb-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.fb-h{font-weight:700;font-variant-numeric:tabular-nums}
.fb-hot{background:#fdecec!important;color:#b42323;font-weight:700}.fb-mid{background:#f4f5f7!important}.fb-cool{background:#e9f5ea!important;color:#2f6b2f;font-weight:700}
.fb-pager{display:flex;gap:12px;align-items:center;justify-content:center;margin-top:10px}
.fb-csv{margin-left:auto;text-decoration:none;display:inline-flex;align-items:center}
.fb-calc{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}.fb-calc label{display:grid;gap:5px;font-size:13px;font-weight:600;color:#374151}
.fb-calc input{height:42px;border:1px solid #e3e5ea;border-radius:10px;padding:0 12px;font:inherit;font-size:15px}
.fb-calc-res{display:flex;flex-direction:column;gap:4px;border-radius:12px;padding:14px 16px;background:#f4f5f7}.fb-calc-res b{font-size:30px}
.fb-calc-res.good{background:#e3efe3;color:#2f6b2f}.fb-calc-res.bad{background:#f6e0e0;color:#b42323}
.fb-lost-hero{display:flex;gap:18px;align-items:stretch;justify-content:space-between;background:linear-gradient(135deg,#fff4e6,#fdecec);border:1px solid #f6d3c0;border-radius:14px;padding:18px 20px;margin-bottom:14px}
.fb-lost-hero span{font-size:13px;color:#7c4a2d;font-weight:600}.fb-lost-hero b{display:block;font-size:34px;color:#b42323;margin:4px 0;font-variant-numeric:tabular-nums}.fb-lost-hero p{margin:0;color:#5b3a29;font-size:13px;max-width:640px}
.fb-lost-hero.ok{background:#e9f5ea;border-color:#c7e3c9}.fb-lost-hero.ok b{color:#2f6b2f}.fb-lost-hero.ok span,.fb-lost-hero.ok p{color:#2f6b2f}
.fb-lost-side{display:flex;flex-direction:column;justify-content:center;text-align:right;border-left:1px solid #f1cbb5;padding-left:18px;min-width:200px}.fb-lost-side b{font-size:24px}
.fb-cmp td{font-size:14px;padding:8px 6px}.fb-cmp-total td{border-top:2px solid #e5e7eb;border-bottom:0}.fb-split{margin-top:14px}
.fb-lost-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.fb-mini-tabs{margin:0}
.fb-lb .fb-hbar,.fb-groups .fb-hbar{width:80px}
@media(max-width:900px){.fb-lost-hero{flex-direction:column}.fb-lost-side{text-align:left;border-left:0;padding-left:0}}
@media(max-width:1300px){.fb-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:900px){.fb-two{grid-template-columns:1fr}.fb-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.fb-calc{grid-template-columns:1fr}}`;
  document.head.appendChild(style);

  return {render};
})();
