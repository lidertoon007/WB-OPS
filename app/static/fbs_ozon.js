/* WB OPS: «Аналитика FBS» для Ozon — сводка, лента отправлений, склады, артикулы, сроки отгрузки (shipment_date) и штрафы Ozon.
   Стили .fb-* — из fbs.js; переключатель маркетплейса — WBOPSFbs.mpSelect / bindMp. */
window.WBOPSFbsOzon = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const num = (v, d = 0) => v == null || v === '' || isNaN(v) ? '—' : Number(v).toLocaleString('ru-RU', {minimumFractionDigits: d, maximumFractionDigits: d});
  const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const parse = s => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const ru = s => s ? s.split('-').reverse().join('.') : '';
  const dt = s => s ? new Date(s).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}) : '—';
  const WD = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
  // Статусы в фиксированном порядке (цвет закреплён за статусом, как в WB-версии)
  const ST = [['sold', 'Доставлены', '#2f9e44'], ['pvz', 'В ПВЗ', '#12b886'], ['transit', 'В доставке', '#4c6ef5'], ['ready', 'Ждут отгрузки', '#15aabf'],
    ['assembly', 'На сборке', '#7950f2'], ['new', 'Новые', '#adb5bd'], ['arbitration', 'Спор', '#862e9c'], ['refused', 'Невыкуп / возврат', '#f08c00'],
    ['client_cancel', 'Отменены покупателем', '#e8b100'], ['seller_cancel', 'Отменены продавцом', '#e03131'], ['ozon_cancel', 'Отменены Ozon', '#c2255c']];
  const STC = Object.fromEntries(ST.map(([k, , c]) => [k, c])), STT = Object.fromEntries(ST.map(([k, t]) => [k, t]));
  const BANDS = [['lt12', 'до 12 ч', '#2f9e44'], ['12_24', '12–24 ч', '#8ac926'], ['24_48', '24–48 ч', '#f59f00'], ['gt48', 'больше 48 ч', '#e03131']];
  const LBANDS = [['early', 'В срок', '#2f9e44'], ['l2', 'Опоздание до 2 ч', '#f59f00'], ['l24', 'Опоздание 2–24 ч', '#f76707'], ['gt24', 'Опоздание больше суток', '#e03131']];
  const st = {tab: 'summary', f: {preset: 'days:14', from: '', to: '', cabinet: '', warehouse: '', brand: '', category: '', q: ''},
    data: null, fs: {status: '', sort: 'created_at', desc: true, offset: 0}, ws: {sort: 'orders', desc: true}, as: {sort: 'orders', desc: true, q: ''}, poll: null};
  let root = null;
  try { Object.assign(st.f, JSON.parse(localStorage.getItem('wbops.fbs.ozon.f') || '{}')); } catch (e) { /* без хранилища */ }
  const remember = () => { try { localStorage.setItem('wbops.fbs.ozon.f', JSON.stringify(st.f)); } catch (e) { /* ignore */ } };
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
    root.innerHTML = `<div class="db-head"><h2>Аналитика FBS · Ozon</h2><span class="muted db-fresh" data-fresh></span></div>
      <div class="db-filters" data-filters></div><div data-notes></div>
      <div class="rnp-tabs fb-tabs" role="tablist">${[['summary', 'Сводка'], ['feed', 'Лента отправлений'], ['warehouses', 'Склады'], ['articles', 'Артикулы'], ['deadlines', 'Сроки отгрузки'], ['fines', 'Штрафы']]
        .map(([k, t]) => `<button type="button" role="tab" data-tab="${k}">${t}</button>`).join('')}</div>
      <div data-body><p class="muted">Загрузка…</p></div>`;
    root.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => { st.tab = b.dataset.tab; drawBody(); });
    await load();
  }
  async function load() {
    const [from, to] = period(), days = (parse(to) - parse(from)) / 864e5 + 1;
    const q = query({prev_from: iso(addDays(parse(from), -days)), prev_to: iso(addDays(parse(from), -1))});
    let data;
    try { data = await api('/api/fbs-analytics/ozon?' + q); }
    catch (e) { if (root.dataset.mp === 'ozon') root.querySelector('[data-body]').innerHTML = `<p class="rv-error">${esc(e.message)}</p>`; return; }
    if (root.dataset.mp !== 'ozon') return;                  // пока грузилось, переключили на WB
    st.data = data;
    drawFilters(); drawNotes(); drawBody();
    clearTimeout(st.poll);
    if (st.data.state?.status === 'running') st.poll = setTimeout(waitSync, 5000);
  }
  async function waitSync() {
    if (!root?.isConnected || root.dataset.mp !== 'ozon') return;
    const s = await api('/api/fbs-analytics/ozon/status').catch(() => ({}));
    if (s.status === 'running') { const n = root.querySelector('[data-sync-msg]'); if (n) n.textContent = s.message || 'Загружаю…'; st.poll = setTimeout(waitSync, 5000); return; }
    load();
  }

  function drawFilters() {
    const o = st.data.options, f = st.f, [from, to] = period();
    const opt = (v, t, cur) => `<option value="${esc(v)}" ${String(v) === String(cur) ? 'selected' : ''}>${esc(t)}</option>`;
    const whs = o.warehouses.filter(w => !f.cabinet || w.cabinet_id === f.cabinet);
    const running = st.data.state?.status === 'running';
    root.querySelector('[data-filters]').innerHTML = `${window.WBOPSFbs?.mpSelect ? window.WBOPSFbs.mpSelect('ozon') : ''}
      <select data-f="preset" aria-label="Период" class="db-period">${presets().map(([k, t]) => opt(k, t, f.preset)).join('')}</select>
      ${f.preset === 'custom' ? `<label class="db-date">с <input type="date" data-f="from" value="${from}"></label><label class="db-date">по <input type="date" data-f="to" value="${to}"></label>` : ''}
      ${o.cabinets.length > 1 ? `<select data-f="cabinet" aria-label="Кабинет">${opt('', 'Все кабинеты', f.cabinet)}${o.cabinets.map(c => opt(c.id, c.name, f.cabinet)).join('')}</select>` : ''}
      ${whs.length > 1 ? `<select data-f="warehouse" aria-label="Склад">${opt('', 'Все склады', f.warehouse)}${whs.map(w => opt(w.id, w.name, f.warehouse)).join('')}</select>` : ''}
      <select data-f="brand" aria-label="Бренд">${opt('', 'Все бренды', f.brand)}${o.brands.map(b => opt(b, b, f.brand)).join('')}</select>
      <select data-f="category" aria-label="Категория">${opt('', 'Все категории', f.category)}${o.categories.map(b => opt(b, b, f.category)).join('')}</select>
      <input type="search" data-f="q" placeholder="Артикул, отправление, SKU" value="${esc(f.q)}" aria-label="Поиск">
      <span class="rp-tools"><button type="button" class="db-ghost" data-sync ${running ? 'disabled' : ''}>↻ ${running ? 'Загружаю…' : 'Обновить из Ozon'}</button></span>`;
    window.WBOPSFbs?.bindMp?.(root);
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
      try { await fetch('/api/fbs-analytics/ozon/sync', {method: 'POST'}).then(r => r.ok ? r : r.json().then(d => { throw new Error(d.detail); })); st.data.state = {status: 'running'}; drawNotes(); st.poll = setTimeout(waitSync, 4000); }
      catch (err) { alert(err.message); e.currentTarget.disabled = false; }
    };
    const s = st.data.synced_at;
    root.querySelector('[data-fresh]').textContent = s ? `Данные Ozon на ${dt(s)} · обновляются каждые 30 минут` : 'Данные ещё не загружены';
  }
  function drawNotes() {
    const s = st.data.state || {}, notes = [];
    if (s.status === 'running') notes.push(`<span data-sync-msg>${esc(s.message || 'Загружаю отправления FBS из Ozon…')}</span> Первая загрузка — за 90 дней, это несколько минут.`);
    (s.errors || []).forEach(e => notes.push('Загрузка: ' + esc(e)));
    if (s.status === 'error') notes.push('Не удалось загрузить: ' + esc(s.message));
    if (!st.data.options.cabinets.length) notes.push('Нет кабинета Ozon с Client ID и API-ключом — добавьте его в разделе «Кабинеты маркетплейсов».');
    else if (!st.data.synced_at && s.status !== 'running') notes.push('Нажмите «Обновить из Ozon». Нужен API-ключ с доступом к отправлениям FBS и финансам.');
    root.querySelector('[data-notes]').innerHTML = notes.map(n => `<div class="db-note">${n}</div>`).join('');
  }
  function drawBody() {
    root.querySelectorAll('[data-tab]').forEach(b => { b.classList.toggle('active', b.dataset.tab === st.tab); b.setAttribute('aria-selected', String(b.dataset.tab === st.tab)); });
    ({summary: drawSummary, feed: drawFeed, warehouses: drawWarehouses, articles: drawArticles, deadlines: drawDeadlines, fines: drawFines}[st.tab] || drawSummary)();
  }
  const goFeed = status => { st.fs.status = status; st.fs.offset = 0; st.tab = 'feed'; drawBody(); };

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
  const signed = v => v == null ? '—' : `${v < 0 ? '−' : v > 0 ? '+' : ''}${num(Math.abs(v))}<sup>₽</sup>`;
  function drawSummary() {
    const s = st.data.summary, p = st.data.previous || {}, has = !!st.data.previous;
    const pv = k => has ? p[k] : null;
    const body = root.querySelector('[data-body]');
    if (!s.orders) { body.innerHTML = '<div class="rv-empty"><b>Нет отправлений FBS Ozon за период</b><p class="muted">Выберите другой период или нажмите «Обновить из Ozon».</p></div>'; return; }
    body.innerHTML = `<div class="db-grid fb-grid">
        ${tile('Отправления FBS', `${num(s.orders)}<sup>шт</sup><span class="db-sep">/</span>${num(s.orders_sum)}<sup>₽</sup>`, s.orders, pv('orders'), {good: 'up', hint: 'Все отправления FBS за период по дате создания (in_process_at)'})}
        ${tile('Доставлено', `${num(s.sold)}<sup>шт</sup><span class="db-sep">/</span>${num(s.sold_sum)}<sup>₽</sup>`, s.sold, pv('sold'), {good: 'up'})}
        ${tile('Процент выкупа', `${num(s.buyout_pct, 1)}<sup>%</sup>`, s.buyout_pct, pv('buyout_pct'), {good: 'up', pp: true, d: 1, hint: 'Доставлено / (доставлено + невыкуп или возврат после отгрузки). Отправления в пути не учитываются'})}
        ${tile('Отмены', `${num(s.cancel_pct, 1)}<sup>%</sup>`, s.cancel_pct, pv('cancel_pct'), {good: 'down', pp: true, d: 1, hint: 'Отмены покупателем, продавцом и Ozon до отгрузки к числу отправлений'})}
        ${tile('В работе', `${num(s.in_progress)}<sup>шт</sup>`, null, null, {fixed: true, hint: 'Отправления периода, которые сейчас новые, на сборке, ждут отгрузки, в доставке, в ПВЗ или в споре'})}
        ${tile('Среднее время до передачи', `${num(s.avg_hours_to_ship, 1)}<sup>ч</sup>`, s.avg_hours_to_ship, pv('avg_hours_to_ship'), {good: 'down', d: 1, unit: 'ч', hint: 'От создания отправления до фактической передачи в доставку (fact_delivery_date из /v3/posting/fbs/get)'})}
        ${tile('Отгружено в срок', `${num(s.on_time_pct, 1)}<sup>%</sup>`, s.on_time_pct, pv('on_time_pct'), {good: 'up', pp: true, d: 1, hint: 'Передано в доставку не позже срока отгрузки (shipment_date)'})}
        ${tile('Опоздания', `${num(s.late)}<sup>шт</sup>${s.avg_late_hours != null ? `<span class="db-sep">/</span>${num(s.avg_late_hours, 1)}<sup>ч</sup>` : ''}`, s.late, pv('late'), {good: 'down', hint: 'Отправления, переданные в доставку позже срока отгрузки, и среднее опоздание'})}
        ${tile('Просрочено сейчас', `${num(s.overdue)}<sup>шт</sup>`, null, null, {fixed: true, hint: 'Ещё не переданы в доставку, а срок отгрузки уже прошёл'})}
        ${tile('Скидка за скорость / штрафы', `${signed(s.effect_rub)}<span class="db-sep">/</span>${s.fines_rub ? '+' + num(s.fines_rub) : '0'}<sup>₽</sup>`, s.fines_rub, pv('fines_rub'), {good: 'down', unit: '₽', hint: 'Слева — скидка (минус) или наценка Ozon за скорость отгрузки по tariffication_fact. Справа — штрафы за сроки и ошибки отгрузки из начислений по отправлениям периода'})}
      </div>
      <section class="tr-box"><h3>Отправления по статусам</h3><div class="fb-status">${ST.filter(([k]) => s.statuses[k]).map(([k, t, c]) =>
        `<button type="button" class="fb-st" data-goto="${k}"><i style="background:${c}"></i>${t} <b>${num(s.statuses[k])}</b><span>${num(s.statuses[k] / s.orders * 100, 1)}%</span></button>`).join('')}
        ${s.late ? `<button type="button" class="fb-st late" data-goto="late"><i></i>Отгружены с опозданием <b>${num(s.late)}</b></button>` : ''}
        ${s.overdue ? `<button type="button" class="fb-st late" data-goto="overdue"><i></i>Просрочены сейчас <b>${num(s.overdue)}</b></button>` : ''}</div>
        ${barsByDay()}</section>
      <div class="fb-two">
        <section class="tr-box"><h3>Скорость отгрузки</h3><p class="muted fb-sub">От создания до передачи в доставку · ${num(s.shipped)} отправлений</p>${bandsBar(s.bands, s.shipped, BANDS)}</section>
        <section class="tr-box"><h3>Соблюдение срока отгрузки</h3><p class="muted fb-sub">Передача в доставку относительно shipment_date</p>${bandsBar(s.late_bands, s.shipped, LBANDS)}</section>
      </div>
      <div class="fb-two">
        <section class="tr-box"><h3>По дням недели</h3><p class="muted fb-sub">День недели — по дате создания отправления</p>${weekdays()}</section>
        <section class="tr-box"><h3>Процент выкупа по дням</h3>${lineChart(st.data.by_day, 'buyout_pct', '%', '#2f9e44')}</section>
      </div>`;
    body.querySelectorAll('[data-goto]').forEach(b => b.onclick = () => goFeed(b.dataset.goto));
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
      return segs + label + (d.orders ? `<rect x="${P + i * bw}" y="${P}" width="${bw}" height="${H - P - 22}" fill="transparent"><title>${ru(d.day)}: ${d.orders} отправлений${d.late ? `, с опозданием ${d.late}` : ''}</title></rect>` : '');
    }).join('');
    return `<div class="fb-chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Отправления по дням и статусам"><text x="${P}" y="14" class="fb-ax">${num(max)} шт</text>
      <line x1="${P}" x2="${W - P}" y1="${H - 22}" y2="${H - 22}" stroke="#e5e7eb"/>${bars}</svg></div>`;
  }
  function bandsBar(values, total, bands) {
    values = values || {};
    if (!total) return '<p class="muted">Нет переданных в доставку отправлений за период.</p>';
    return `<div class="fb-bands" role="img" aria-label="Распределение">${bands.map(([k, t, c]) => {
      const v = values[k] || 0; return v ? `<span style="width:${v / total * 100}%;background:${c}" title="${t}: ${v} (${num(v / total * 100, 1)}%)"></span>` : ''; }).join('')}</div>
      <table class="fb-mini">${bands.map(([k, t, c]) => `<tr><td><i style="background:${c}"></i>${t}</td><td class="r"><b>${num(values[k] || 0)}</b></td>
        <td class="r muted">${num((values[k] || 0) / total * 100, 1)}%</td></tr>`).join('')}</table>`;
  }
  function weekdays() {
    const w = st.data.weekdays, max = Math.max(1, ...w.map(x => x.avg_hours_to_ship || 0));
    return `<table class="fb-mini fb-wd"><tr><th></th><th class="r">Отправлений</th><th>Время до передачи</th><th class="r">Опоздания</th><th class="r">Выкуп</th></tr>${w.map(x => `<tr><td><b>${WD[x.weekday]}</b></td><td class="r">${num(x.orders)}</td>
      <td><span class="fb-hbar"><i style="width:${(x.avg_hours_to_ship || 0) / max * 100}%;background:${(BANDS.find(b => b[0] === bandOf(x.avg_hours_to_ship)) || [])[2] || '#adb5bd'}"></i></span> ${num(x.avg_hours_to_ship, 1)} ч</td>
      <td class="r ${x.late_pct ? 'neg' : ''}">${x.late_pct == null ? '—' : num(x.late_pct, 1) + '%'}</td><td class="r">${x.buyout_pct == null ? '—' : num(x.buyout_pct, 1) + '%'}</td></tr>`).join('')}</table>`;
  }
  const bandOf = h => h == null ? null : h < 12 ? 'lt12' : h < 24 ? '12_24' : h < 48 ? '24_48' : 'gt48';
  function lineChart(days, key, unit, color) {
    const pts = days.map((d, i) => [i, d[key]]).filter(p => p[1] != null);
    if (pts.length < 2) return '<p class="muted">Недостаточно данных.</p>';
    const W = 560, H = 170, P = 30, vals = pts.map(p => p[1]);
    const lo = Math.min(...vals) * 0.95, hi = Math.max(...vals) * 1.05 || 1;
    const X = i => P + i / Math.max(1, days.length - 1) * (W - 2 * P), Y = v => H - 22 - (v - lo) / Math.max(1e-9, hi - lo) * (H - 40);
    const path = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(' ');
    return `<svg class="fb-line" viewBox="0 0 ${W} ${H}" role="img" aria-label="График"><text x="${P}" y="12" class="fb-ax">${num(hi, 0)} ${unit}</text><text x="${P}" y="${H - 6}" class="fb-ax">${num(lo, 0)} ${unit}</text>
      <path d="${path}" fill="none" stroke="${color}" stroke-width="2"/>${pts.map(p => `<circle cx="${X(p[0])}" cy="${Y(p[1])}" r="3.5" fill="${color}" stroke="#fff" stroke-width="1.5"><title>${ru(days[p[0]].day)}: ${num(p[1], 1)} ${unit}</title></circle>`).join('')}</svg>`;
  }

  // ---------------------------------------------------------------- лента отправлений
  async function drawFeed() {
    const body = root.querySelector('[data-body]'), fs = st.fs;
    body.innerHTML = '<p class="muted">Загрузка…</p>';
    let d;
    try { d = await api('/api/fbs-analytics/ozon/orders?' + query({status: fs.status, sort: fs.sort, desc: fs.desc, offset: fs.offset, limit: 100})); }
    catch (e) { body.innerHTML = `<p class="rv-error">${esc(e.message)}</p>`; return; }
    if (st.tab !== 'feed') return;
    const total = Object.values(d.counts).reduce((a, b) => a + b, 0), x = d.extra || {};
    const th = (k, t, cls = '') => `<th class="${cls}"><button type="button" class="rp-sort ${fs.sort === k ? 'on' : ''}" data-fsort="${k}">${t}<span>${fs.sort === k ? (fs.desc ? '↓' : '↑') : '↕'}</span></button></th>`;
    const multi = st.data.options.cabinets.length > 1;
    body.innerHTML = `<div class="sk-chips">
        <button type="button" class="sk-chip ${!fs.status ? 'on' : ''}" data-fst="">Все <b>${num(total)}</b></button>
        ${ST.filter(([k]) => d.counts[k]).map(([k, t, c]) => `<button type="button" class="sk-chip ${fs.status === k ? 'on' : ''}" data-fst="${k}"><i class="fb-dot" style="background:${c}"></i>${t} <b>${num(d.counts[k])}</b></button>`).join('')}
        <button type="button" class="sk-chip ${fs.status === 'late' ? 'on' : ''}" data-fst="late">С опозданием <b>${num(x.late)}</b></button>
        <button type="button" class="sk-chip ${fs.status === 'overdue' ? 'on' : ''}" data-fst="overdue">Просрочены <b>${num(x.overdue)}</b></button>
        <button type="button" class="sk-chip ${fs.status === 'fined' ? 'on' : ''}" data-fst="fined">Со штрафом <b>${num(x.fined)}</b></button>
        <a class="db-ghost fb-csv" href="/api/fbs-analytics/ozon/orders.csv?${query({status: fs.status})}" download>⇩ CSV</a></div>
      <div class="rnp-scroll"><table class="rnp-table fb-feed"><thead><tr>${th('created_at', 'Создано', 'l')}<th class="l">Отправление</th>${th('article', 'Артикул', 'l')}${th('warehouse', 'Склад', 'l')}
        ${th('price', 'Сумма')}<th class="l">Статус</th>${th('shipment_date', 'Срок отгрузки')}${th('hours_to_ship', 'До передачи')}${th('late_hours', 'Опоздание')}${th('effect_rub', 'Скорость')}${th('fine_rub', 'Штраф')}</tr></thead><tbody>
      ${d.items.map(r => `<tr><td class="l">${dt(r.created_at)}</td><td class="l">${esc(r.posting)}${r.cancel_reason ? `<small>${esc(r.cancel_reason)}</small>` : ''}</td>
        <td class="l"><b>${esc(r.article)}</b><small>${r.sku || ''}${r.qty > 1 ? ` · ${r.qty} шт` : ''}${r.brand ? ' · ' + esc(r.brand) : ''}</small></td><td class="l">${esc(r.warehouse)}${multi ? `<small>${esc(r.cabinet)}</small>` : ''}</td>
        <td>${num(r.price)} ₽</td><td class="l"><span class="fb-chip" style="--c:${STC[r.status]}">${STT[r.status] || r.status}</span></td>
        <td>${dt(r.shipment_date)}</td>
        <td>${r.hours_to_ship == null ? '—' : `<span class="fb-h" style="color:${(BANDS.find(b => b[0] === r.band) || [])[2]}">${num(r.hours_to_ship, 1)} ч</span>`}</td>
        <td class="${r.late || r.overdue ? 'neg' : ''}">${r.overdue ? `просрочено ${num(r.overdue_hours, 1)} ч` : r.late_hours == null ? '—' : r.late ? `+${num(r.late_hours, 1)} ч` : 'в срок'}</td>
        <td class="${r.effect_pct == null ? '' : r.effect_pct < 0 ? 'pos' : r.effect_pct > 0 ? 'neg' : ''}">${r.effect_pct == null ? '—' : `${r.effect_pct > 0 ? '+' : ''}${num(r.effect_pct, 1)}%<small>${r.effect_rub > 0 ? '+' : ''}${num(r.effect_rub)} ₽</small>`}</td>
        <td class="${r.fine_rub ? 'neg' : ''}">${r.fine_rub ? num(r.fine_rub) + ' ₽' : '—'}</td></tr>`).join('')
        || '<tr><td colspan="11" class="muted l">Нет отправлений.</td></tr>'}</tbody></table></div>
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
    const heat = v => v == null ? '' : v >= 48 ? 'fb-hot' : v >= 24 ? 'fb-mid' : v < 12 ? 'fb-cool' : '';
    const html = `<div class="rnp-scroll"><table class="rnp-table fb-groups"><thead><tr>${cols.map(([k, t, cls]) => th(k, t, cls)).join('')}
        ${th('orders', 'Отправлений')}${th('orders_sum', 'Сумма, ₽')}${th('buyout_pct', '% выкупа')}${th('cancel_pct', 'Отмены')}${th('avg_hours_to_ship', 'До передачи, ч')}<th class="l">Скорость отгрузки</th>
        ${th('on_time_pct', 'В срок, %')}${th('late', 'Опоздания')}${th('overdue', 'Просрочено')}${th('fines_rub', 'Штрафы, ₽')}</tr></thead><tbody>
      ${sorted.slice(0, 300).map(r => `<tr>${cols.map(([k, , cls, fmt]) => `<td class="${cls || ''}">${fmt ? fmt(r) : esc(r[k])}</td>`).join('')}
        <td>${num(r.orders)}</td><td>${num(r.orders_sum)}</td><td>${r.buyout_pct == null ? '—' : num(r.buyout_pct, 1) + '%'}</td><td>${r.cancel_pct == null ? '—' : num(r.cancel_pct, 1) + '%'}</td>
        <td class="${heat(r.avg_hours_to_ship)}">${num(r.avg_hours_to_ship, 1)}</td>
        <td class="l"><div class="fb-bands sm">${BANDS.map(([k, t, c]) => r.bands[k] ? `<span style="width:${r.bands[k] / r.shipped * 100}%;background:${c}" title="${t}: ${r.bands[k]}"></span>` : '').join('')}</div></td>
        <td>${r.on_time_pct == null ? '—' : num(r.on_time_pct, 1)}</td><td class="${r.late ? 'neg' : ''}">${num(r.late)}</td><td class="${r.overdue ? 'neg' : ''}">${num(r.overdue)}</td>
        <td class="${r.fines_rub ? 'neg' : ''}">${r.fines_rub ? num(r.fines_rub) : '—'}</td></tr>`).join('')
        || `<tr><td colspan="${cols.length + 10}" class="muted l">Нет данных.</td></tr>`}</tbody></table></div>
      <p class="muted rp-note">Цвет «До передачи»: зелёный — до 12 ч, серый — 24–48 ч, красный — 48 ч и больше. Опоздание — передача в доставку позже срока отгрузки Ozon (shipment_date).</p>`;
    return {html, bind: body => body.querySelectorAll('[data-gsort]').forEach(b => b.onclick = () => { if (state.sort === b.dataset.gsort) state.desc = !state.desc; else { state.sort = b.dataset.gsort; state.desc = true; } redraw(); })};
  }
  function drawWarehouses() {
    const body = root.querySelector('[data-body]');
    const t = groupTable(st.data.warehouses, [['warehouse', 'Склад', 'l', r => `<b>${esc(r.warehouse)}</b>${st.data.options.cabinets.length > 1 ? `<small>${esc(r.cabinet)}</small>` : ''}`]], st.ws, drawWarehouses);
    body.innerHTML = t.html; t.bind(body);
  }
  function drawArticles() {
    const body = root.querySelector('[data-body]'), q = st.as.q.toLowerCase();
    const rows = st.data.articles.filter(r => !q || `${r.article} ${r.sku} ${r.title}`.toLowerCase().includes(q));
    const t = groupTable(rows, [['article', 'Артикул', 'l', r => `<b>${esc(r.article)}</b><small>${esc(r.title || '')}${r.title ? ' · ' : ''}${r.sku || ''}</small>`]], st.as, drawArticles);
    body.innerHTML = `<div class="rnp-tools"><input type="search" data-aq placeholder="Найти артикул" value="${esc(st.as.q)}"><span class="muted">${rows.length} артикулов</span></div>` + t.html;
    t.bind(body);
    const s = body.querySelector('[data-aq]');
    s.oninput = () => { st.as.q = s.value; const pos = s.selectionStart; drawArticles(); const n = root.querySelector('[data-aq]'); n.focus(); n.setSelectionRange(pos, pos); };
  }

  // ---------------------------------------------------------------- сроки отгрузки
  function drawDeadlines() {
    const s = st.data.summary, p = st.data.previous || {}, has = !!st.data.previous, body = root.querySelector('[data-body]');
    const pv = k => has ? p[k] : null;
    if (!s.orders) { body.innerHTML = '<div class="rv-empty"><b>Нет отправлений FBS Ozon за период</b></div>'; return; }
    body.innerHTML = `<div class="db-grid fb-grid">
        ${tile('Отгружено в срок', `${num(s.on_time_pct, 1)}<sup>%</sup>`, s.on_time_pct, pv('on_time_pct'), {good: 'up', pp: true, d: 1, hint: 'Доля переданных в доставку до срока shipment_date'})}
        ${tile('С опозданием', `${num(s.late)}<sup>шт</sup>`, s.late, pv('late'), {good: 'down', hint: 'Переданы в доставку позже срока отгрузки'})}
        ${tile('Среднее опоздание', `${num(s.avg_late_hours, 1)}<sup>ч</sup>`, s.avg_late_hours, pv('avg_late_hours'), {good: 'down', d: 1, unit: 'ч', hint: 'Сколько в среднем часов после срока отгрузки отправление передавали в доставку'})}
        ${tile('Просрочено сейчас', `${num(s.overdue)}<sup>шт</sup>`, null, null, {fixed: true, hint: 'Ещё на сборке или ждут отгрузки, а срок shipment_date прошёл. Ozon может отменить их и начислить штраф'})}
        ${tile('Со скидкой за скорость', `${num(s.discount_pct, 1)}<sup>%</sup>`, s.discount_pct, pv('discount_pct'), {good: 'up', pp: true, d: 1, hint: `Доля отправлений, где Ozon применил скидку за быструю отгрузку (tariffication_fact), из ${s.tariffed} с известным тарифом`})}
      </div>
      ${s.overdue ? `<div class="db-note">Просрочено ${num(s.overdue)} отправлений — <button type="button" class="db-ghost" data-goto="overdue">открыть список</button></div>` : ''}
      <div class="fb-two">
        <section class="tr-box"><h3>Соблюдение срока</h3><p class="muted fb-sub">Передача в доставку относительно shipment_date · ${num(s.shipped)} отправлений</p>${bandsBar(s.late_bands, s.shipped, LBANDS)}</section>
        <section class="tr-box"><h3>Доля опозданий по дням, %</h3>${lineChart(st.data.by_day, 'late_pct', '%', '#e03131')}</section>
      </div>
      <section class="tr-box"><h3>Где опаздываем</h3>${lateTable()}</section>
      <p class="muted rp-note">Срок отгрузки — поле shipment_date отправления. Момент передачи — fact_delivery_date из /v3/posting/fbs/get, если ещё не загружен — delivering_date из списка.
        Скидка за скорость — фактический тариф Ozon (tariffication_fact): «discount» — скидка, «no_discount» — без скидки.</p>`;
    body.querySelectorAll('[data-goto]').forEach(b => b.onclick = () => goFeed(b.dataset.goto));
  }
  function lateTable() {
    const rows = st.data.warehouses.filter(r => r.shipped || r.overdue).sort((a, b) => (b.late + b.overdue) - (a.late + a.overdue));
    return `<div class="rnp-scroll"><table class="rnp-table fb-groups"><thead><tr><th class="l">Склад</th><th>Передано</th><th>В срок, %</th><th>С опозданием</th><th>Среднее опоздание, ч</th><th>Просрочено сейчас</th><th>Штрафы, ₽</th></tr></thead><tbody>
      ${rows.map(r => `<tr><td class="l"><b>${esc(r.warehouse)}</b></td><td>${num(r.shipped)}</td><td>${num(r.on_time_pct, 1)}</td><td class="${r.late ? 'neg' : ''}">${num(r.late)}</td>
        <td>${num(r.avg_late_hours, 1)}</td><td class="${r.overdue ? 'neg' : ''}">${num(r.overdue)}</td><td>${r.fines_rub ? num(r.fines_rub) : '—'}</td></tr>`).join('')
        || '<tr><td colspan="7" class="muted l">Нет данных.</td></tr>'}</tbody></table></div>`;
  }

  // ---------------------------------------------------------------- штрафы
  function drawFines() {
    const fz = st.data.fines, body = root.querySelector('[data-body]'), s = st.data.summary;
    if (!fz.count) { body.innerHTML = `<div class="rv-empty"><b>Штрафов за сроки за период нет</b><p class="muted">Ищутся начисления Ozon «Обработка операционных ошибок продавца», «Превышение индекса ошибок» и «Отгрузка в нерекомендованный слот». Начисления обновляются раз в 3 часа.</p></div>`; return; }
    const max = Math.max(1, ...fz.by_type.map(t => t.sum));
    body.innerHTML = `<div class="fb-lost-hero"><div><span>Штрафы Ozon за сроки и ошибки отгрузки</span><b>${num(fz.total)} ₽</b>
        <p>${num(fz.count)} начислений за период (по дате начисления). Из них по отправлениям FBS из ленты — ${num(fz.fbs_total)} ₽, по отправлениям этого периода — ${num(s.fines_rub)} ₽ (${num(s.fined)} шт).</p></div></div>
      <div class="fb-two">
        <section class="tr-box"><h3>По видам</h3><table class="fb-mini fb-lb"><tr><th>Вид начисления</th><th class="r">Кол-во</th><th>Сумма</th></tr>
          ${fz.by_type.map(t => `<tr><td title="${esc(t.name)}">${esc(t.label)}</td><td class="r">${num(t.count)}</td>
            <td><span class="fb-hbar"><i style="width:${t.sum / max * 100}%;background:#e03131"></i></span> <b>${num(t.sum)} ₽</b></td></tr>`).join('')}</table></section>
        <section class="tr-box"><h3>Штрафы по дням, ₽</h3>${lineChart(st.data.by_day.map(d => ({...d, fines: d.fines || null})), 'fines', '₽', '#e03131')}</section>
      </div>
      <section class="tr-box"><h3>Начисления</h3><div class="rnp-scroll"><table class="rnp-table fb-feed"><thead><tr><th class="l">Дата</th><th class="l">Отправление</th><th class="l">Вид</th><th class="l">Артикул</th><th class="l">Склад</th><th>Сумма</th></tr></thead><tbody>
        ${fz.items.map(x => `<tr><td class="l">${ru(x.day)}</td><td class="l">${esc(x.posting)}${x.fbs ? '' : '<small>нет в ленте FBS</small>'}</td><td class="l">${esc(x.type)}</td>
          <td class="l">${esc(x.article) || '—'}</td><td class="l">${esc(x.warehouse) || '—'}</td><td class="neg">${num(x.amount)} ₽</td></tr>`).join('')}</tbody></table></div>
        ${fz.count > fz.items.length ? `<p class="muted rp-note">Показаны ${fz.items.length} из ${num(fz.count)} начислений.</p>` : ''}</section>
      <p class="muted rp-note">Источник — начисления /v1/finance/accrual/by-day (за закрытые дни — кэш ОПиУ Ozon). «Нет в ленте FBS» — отправление FBO или созданное до начала загрузки.</p>`;
  }

  return {render};
})();
