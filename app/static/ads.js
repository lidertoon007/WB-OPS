/* WB OPS: «Аналитика рекламы» — расходы, ДРР, кампании, артикулы, поисковые запросы, площадки и списания WB. */
window.WBOPSAds = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const num = (v, d = 0) => v == null || v === '' || isNaN(v) ? '—' : Number(v).toLocaleString('ru-RU', {minimumFractionDigits: d, maximumFractionDigits: d});
  const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const parse = s => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const ru = s => s ? s.slice(0, 10).split('-').reverse().join('.') : '';
  const dt = s => s ? new Date(s).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}) : '—';
  const pct = (v, d = 1) => v == null ? '—' : num(v, d) + '%';
  const rub = v => v == null ? '—' : num(v) + ' ₽';
  const STC = {9: '#2f9e44', 11: '#f59f00', 7: '#adb5bd', 4: '#4c6ef5', 8: '#e03131', '-1': '#adb5bd'};
  const PAY = {cpm: 'CPM · за показы', cpc: 'CPC · за клики'}, BID = {manual: 'ручная ставка', unified: 'единая ставка'};
  const PLC = {search: 'поиск', recommendations: 'рекомендации', combined: 'поиск + рекомендации'};
  const st = {tab: 'summary', insightLevel: 'all', f: {preset: 'days:14', from: '', to: '', cabinet: '', campaign: '', status: '', payment: '', brand: '', category: '', q: ''},
    data: null, cs: {sort: 'spend', desc: true, st: 'live'}, as: {sort: 'spend', desc: true, q: ''}, qs: {sort: 'spend', desc: true, q: '', mode: ''}, poll: null};
  let root = null;
  try { Object.assign(st.f, JSON.parse(localStorage.getItem('wbops.ads.f') || '{}')); } catch (e) { /* без хранилища */ }
  const remember = () => { try { localStorage.setItem('wbops.ads.f', JSON.stringify(st.f)); } catch (e) { /* ignore */ } };
  const api = async (url, opt) => { const r = await fetch(url, opt); const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.detail || 'Ошибка запроса'); return d; };

  function presets() {
    const t = new Date(), y = addDays(t, -1), m0 = new Date(t.getFullYear(), t.getMonth(), 1);
    return [['today', 'Сегодня', t, t], ['yesterday', 'Вчера', y, y], ['days:7', 'Последние 7 дней', addDays(t, -6), t], ['days:14', 'Последние 14 дней', addDays(t, -13), t],
      ['days:30', 'Последние 30 дней', addDays(t, -29), t], ['days:90', 'Последние 90 дней', addDays(t, -89), t], ['month:0', 'Текущий месяц', m0, t],
      ['month:1', 'Прошлый месяц', new Date(t.getFullYear(), t.getMonth() - 1, 1), new Date(t.getFullYear(), t.getMonth(), 0)], ['custom', 'Свой период…', null, null]];
  }
  function period() {
    if (st.f.preset === 'custom' && st.f.from && st.f.to) return [st.f.from, st.f.to];
    const p = presets().find(x => x[0] === st.f.preset) || presets()[3];
    return [iso(p[2]), iso(p[3])];
  }
  function query(extra = {}) {
    const [from, to] = period(), q = new URLSearchParams({from, to});
    ['cabinet', 'campaign', 'status', 'payment', 'brand', 'category', 'q'].forEach(k => st.f[k] && q.set(k, st.f[k]));
    Object.entries(extra).forEach(([k, v]) => v !== '' && v != null && q.set(k, v));
    return q;
  }

  // ---------------------------------------------------------------- каркас
  async function render(section) {
    root = section;
    root.className = 'panel rv-page fb-page ad-page';
    root.innerHTML = `<div class="db-head"><h2>Аналитика рекламы</h2><span class="muted db-fresh" data-fresh></span></div>
      <div class="db-filters" data-filters></div><div data-notes></div>
      <div class="rnp-tabs fb-tabs" role="tablist">${[['summary', 'Сводка'], ['campaigns', 'Кампании'], ['articles', 'Артикулы'], ['queries', 'Поисковые запросы'], ['spending', 'Списания и баланс']]
        .map(([k, t]) => `<button type="button" role="tab" data-tab="${k}">${t}</button>`).join('')}</div>
      <div data-body><p class="muted">Загрузка…</p></div>`;
    root.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => { st.tab = b.dataset.tab; drawBody(); });
    await load();
  }
  async function load() {
    const [from, to] = period(), days = (parse(to) - parse(from)) / 864e5 + 1;
    try { st.data = await api('/api/ads?' + query({prev_from: iso(addDays(parse(from), -days)), prev_to: iso(addDays(parse(from), -1))})); }
    catch (e) { root.querySelector('[data-body]').innerHTML = `<p class="rv-error">${esc(e.message)}</p>`; return; }
    drawFilters(); drawNotes(); drawBody();
    clearTimeout(st.poll);
    if (st.data.state?.status === 'running') st.poll = setTimeout(waitSync, 6000);
  }
  async function waitSync() {
    if (!root?.isConnected) return;
    const s = await api('/api/ads/status').catch(() => ({}));
    if (s.status === 'running') { const n = root.querySelector('[data-sync-msg]'); if (n) n.textContent = s.message || 'Загружаю…'; st.poll = setTimeout(waitSync, 6000); return; }
    load();
  }
  function drawFilters() {
    const o = st.data.options, f = st.f, [from, to] = period();
    const opt = (v, t, cur) => `<option value="${esc(v)}" ${String(v) === String(cur) ? 'selected' : ''}>${esc(t)}</option>`;
    const busy = st.data.state?.status === 'running';
    root.querySelector('[data-filters]').innerHTML = `
      <select data-f="preset" aria-label="Период" class="db-period">${presets().map(([k, t]) => opt(k, t, f.preset)).join('')}</select>
      ${f.preset === 'custom' ? `<label class="db-date">с <input type="date" data-f="from" value="${from}"></label><label class="db-date">по <input type="date" data-f="to" value="${to}"></label>` : ''}
      ${o.cabinets.length > 1 ? `<select data-f="cabinet" aria-label="Кабинет">${opt('', 'Все кабинеты', f.cabinet)}${o.cabinets.map(c => opt(c, c, f.cabinet)).join('')}</select>` : ''}
      <select data-f="campaign" aria-label="Кампания" class="ad-camp-sel">${opt('', 'Все кампании', f.campaign)}${o.campaigns.map(c => opt(c.id, `${c.name} · ${c.id}`, f.campaign)).join('')}</select>
      <select data-f="payment" aria-label="Оплата">${opt('', 'CPM и CPC', f.payment)}${opt('cpm', 'CPM — за показы', f.payment)}${opt('cpc', 'CPC — за клики', f.payment)}</select>
      <select data-f="brand" aria-label="Бренд">${opt('', 'Все бренды', f.brand)}${o.brands.map(b => opt(b, b, f.brand)).join('')}</select>
      <select data-f="category" aria-label="Предмет">${opt('', 'Все предметы', f.category)}${o.categories.map(b => opt(b, b, f.category)).join('')}</select>
      <input type="search" data-f="q" placeholder="Артикул, кампания, запрос" value="${esc(f.q)}" aria-label="Поиск">
      <span class="rp-tools"><button type="button" class="db-ghost" data-sync ${busy ? 'disabled' : ''}>↻ ${busy ? 'Загружаю…' : 'Обновить из WB'}</button></span>`;
    let t;
    root.querySelectorAll('[data-f]').forEach(el => {
      const h = () => {
        const k = el.dataset.f;
        if (k === 'preset' && el.value === 'custom') { st.f.from = from; st.f.to = to; }
        st.f[k] = el.value; remember();
        clearTimeout(t); t = setTimeout(load, k === 'q' ? 400 : 0);
      };
      el.type === 'search' ? (el.oninput = h) : (el.onchange = h);
    });
    root.querySelector('[data-sync]').onclick = async e => {
      e.currentTarget.disabled = true;
      try { await api('/api/ads/sync', {method: 'POST'}); st.data.state = {status: 'running'}; drawNotes(); st.poll = setTimeout(waitSync, 5000); }
      catch (err) { alert(err.message); e.currentTarget.disabled = false; }
    };
    const s = st.data.synced_at;
    root.querySelector('[data-fresh]').textContent = s ? `Данные WB на ${dt(s)} · обновляются каждый час` : 'Данные ещё не загружены';
  }
  function drawNotes() {
    const s = st.data.state || {}, notes = [];
    if (s.status === 'running') notes.push(`<span data-sync-msg>${esc(s.message || 'Загружаю рекламу из WB…')}</span> Первая загрузка — за 90 дней, WB отдаёт статистику медленно (3 запроса в минуту), это 5–15 минут.`);
    (s.errors || []).forEach(e => notes.push('Загрузка: ' + esc(e)));
    if (s.status === 'error') notes.push('Не удалось загрузить: ' + esc(s.message));
    if (!st.data.synced_at && s.status !== 'running') notes.push('Нажмите «Обновить из WB». Нужен ключ API с категорией «Продвижение» (поле «Ключ API рекламы» в кабинете).');
    root.querySelector('[data-notes]').innerHTML = notes.map(n => `<div class="db-note">${n}</div>`).join('');
  }
  function drawBody() {
    root.querySelectorAll('[data-tab]').forEach(b => { b.classList.toggle('active', b.dataset.tab === st.tab); b.setAttribute('aria-selected', String(b.dataset.tab === st.tab)); });
    ({summary: drawSummary, campaigns: drawCampaigns, articles: drawArticles, queries: drawQueries, spending: drawSpending}[st.tab])();
  }

  // ---------------------------------------------------------------- сводка
  function tile(title, value, cur, prev, opts = {}) {
    let cls = 'neutral', delta = '';
    if (cur != null && prev != null && !(cur === 0 && prev === 0)) {
      const diff = cur - prev, good = opts.good === 'down' ? diff < 0 : diff > 0;
      cls = Math.abs(diff) < 1e-9 || !opts.good ? 'neutral' : good ? 'good' : 'bad';
      const rel = prev && !opts.pp ? ` <span>(${num(diff / Math.abs(prev) * 100, 1)}%)</span>` : '';
      delta = `<span class="db-delta">${diff > 0 ? '+' : ''}${num(diff, opts.d ?? 0)}${opts.pp ? ' п.п.' : opts.unit ? ' ' + opts.unit : ''}${rel}</span><span class="db-arrow">${diff > 0 ? '↗' : diff < 0 ? '↘' : '→'}</span>`;
    } else if (st.data.previous) delta = '<span class="db-delta muted">нет данных</span>';
    return `<article class="db-tile ${cls}"><div class="db-title">${esc(title)}${opts.hint ? ` <span class="db-i" tabindex="0" title="${esc(opts.hint)}" aria-label="${esc(opts.hint)}">i</span>` : ''}</div>
      <div class="db-value">${value}</div><div class="db-change">${delta}</div></article>`;
  }
  function drawSummary() {
    const d = st.data, s = d.summary, p = d.previous || {}, has = !!d.previous, body = root.querySelector('[data-body]');
    const pv = k => has ? p[k] : null;
    if (!d.synced_at) { body.innerHTML = '<div class="rv-empty"><b>Нет данных</b><p class="muted">Нажмите «Обновить из WB».</p></div>'; return; }
    const net = Object.values(d.spending.balances || {}).reduce((a, b) => a + (Number(b.net) || 0), 0);
    body.innerHTML = `<div class="fb-lost-hero ad-hero">
        <div><span>Расход на рекламу за период</span><b>${num(s.spend)} ₽</b>
          <p>${s.spend ? `${num(s.orders)} заказов с рекламы на ${num(s.sum_price)} ₽ · заказ из рекламы стоит ${rub(s.cpo)} · ДРР рекламных заказов ${pct(s.drr)}${s.drr_total != null ? `, от всех заказов ${pct(s.drr_total)}` : ''}. Работает ${num(s.active)} кампаний${s.paused ? `, на паузе ${num(s.paused)}` : ''}.` : 'За период расходов нет.'}</p></div>
        <div class="fb-lost-side"><span>≈ в месяц при таком темпе</span><b>${num(s.spend / d.days * 30)} ₽</b><span>${net ? `на балансе ${num(net)} ₽` : ''}${s.budget_left ? ` · в бюджетах кампаний ${num(s.budget_left)} ₽` : ''}</span></div>
      </div>
      <div class="db-grid fb-grid">
        ${tile('Расход', `${num(s.spend)}<sup>₽</sup>`, s.spend, pv('spend'), {unit: '₽'})}
        ${tile('Заказы с рекламы', `${num(s.orders)}<sup>шт</sup>`, s.orders, pv('orders'), {good: 'up', hint: 'Заказы, которые WB засчитал рекламе (по данным статистики кампаний)'})}
        ${tile('Цена заказа', `${num(s.cpo)}<sup>₽</sup>`, s.cpo, pv('cpo'), {good: 'down', unit: '₽', hint: 'Расход / заказы с рекламы'})}
        ${tile('ДРР рекламных заказов', `${num(s.drr, 1)}<sup>%</sup>`, s.drr, pv('drr'), {good: 'down', pp: true, d: 1, hint: 'Расход / сумма заказов, пришедших с рекламы'})}
        ${tile('ДРР от всех заказов', `${num(s.drr_total, 1)}<sup>%</sup>`, s.drr_total, pv('drr_total'), {good: 'down', pp: true, d: 1, hint: 'Расход / сумма всех заказов этих артикулов (без отмен). Заказы — из РНП'})}
        ${tile('Показы', `${num(s.views)}`, s.views, pv('views'), {good: 'up'})}
        ${tile('CTR', `${num(s.ctr, 2)}<sup>%</sup>`, s.ctr, pv('ctr'), {good: 'up', pp: true, d: 2, hint: 'Клики / показы'})}
        ${tile('Цена клика (CPC)', `${num(s.cpc, 2)}<sup>₽</sup>`, s.cpc, pv('cpc'), {good: 'down', unit: '₽', d: 2})}
        ${tile('Конверсия клик → заказ', `${num(s.cr, 2)}<sup>%</sup>`, s.cr, pv('cr'), {good: 'up', pp: true, d: 2})}
        ${tile('Доля заказов с рекламы', `${num(s.ad_share, 1)}<sup>%</sup>`, s.ad_share, pv('ad_share'), {pp: true, d: 1, hint: 'Заказы с рекламы / все заказы этих артикулов'})}
      </div>
      ${insights()}
      <section class="tr-box"><h3>Расход по дням</h3><p class="muted fb-sub">Наведите на столбец — заказы, цена заказа и ДРР за день</p>${spendChart()}</section>
      <div class="fb-two">
        <section class="tr-box"><h3>ДРР от всех заказов по дням, %</h3>${line('drr_total', '%', '#e8590c')}</section>
        <section class="tr-box"><h3>Цена заказа с рекламы по дням, ₽</h3>${line('cpo', '₽', '#7c3aed')}</section>
      </div>
      <div class="fb-two">
        <section class="tr-box"><h3>Воронка рекламы</h3><p class="muted fb-sub">От показа до заказа</p>${funnel(s)}</section>
        <section class="tr-box"><h3>Площадки</h3><p class="muted fb-sub">Где показывалась реклама</p>${platforms()}</section>
      </div>
      <section class="tr-box"><h3>Кампании с наибольшим расходом</h3>${campTable(d.campaigns.slice(0, 6), true)}</section>`;
    body.querySelectorAll('[data-insight-filter]').forEach(b => b.onclick = () => { st.insightLevel = b.dataset.insightFilter; drawSummary(); });
    bindCamp(body);
    body.querySelectorAll('[data-goins]').forEach(b => b.onclick = () => {
      const k = b.dataset.goins, id = b.dataset.id;
      if (k === 'campaign') { st.f.campaign = id; remember(); load(); }
      else if (k === 'article') { st.f.q = id; remember(); st.tab = 'articles'; load(); }
      else { st.qs.mode = 'waste'; st.tab = 'queries'; drawBody(); }
    });
  }
  function insights() {
    const ins = st.data.insights;
    if (!ins.length) return '<div class="pn-check ok">✓ Явных проблем в рекламе за период не найдено.</div>';
    const priority = {bad: 0, warn: 1};
    const ordered = ins.map((item, index) => ({item, index})).sort((a, b) => (priority[a.item.level] ?? 2) - (priority[b.item.level] ?? 2) || a.index - b.index).map(({item}) => item);
    const counts = {all: ordered.length, bad: ordered.filter(i => i.level === 'bad').length, warn: ordered.filter(i => i.level === 'warn').length};
    const visible = st.insightLevel === 'all' ? ordered : ordered.filter(i => i.level === st.insightLevel);
    const empty = '<div class="ad-ins-empty muted">Для выбранного фильтра проблем не найдено.</div>';
    return `<section class="tr-box ad-ins"><h3>Что улучшить</h3><div class="ad-ins-legend"><span class="ad-ins-legend-item bad"><i></i><b>Критично</b> — подтверждена потеря эффективности или денег</span><span class="ad-ins-legend-item warn"><i></i><b>Требует внимания</b> — сигнал для проверки</span></div><div class="ad-ins-filters" role="group" aria-label="Фильтр проблем"><button type="button" class="ad-ins-filter ${st.insightLevel === 'all' ? 'active' : ''}" data-insight-filter="all">Все <b>${counts.all}</b></button><button type="button" class="ad-ins-filter bad ${st.insightLevel === 'bad' ? 'active' : ''}" data-insight-filter="bad">Критичные <b>${counts.bad}</b></button><button type="button" class="ad-ins-filter warn ${st.insightLevel === 'warn' ? 'active' : ''}" data-insight-filter="warn">Требуют внимания <b>${counts.warn}</b></button></div><div class="ad-ins-list">${visible.length ? visible.map(i => `<button type="button" class="ad-ins-item ${i.level}" data-goins="${i.kind}" data-id="${esc(i.id ?? '')}">
      <b>${i.level === 'bad' ? '⚠' : '•'} ${esc(i.title)}</b><span>${esc(i.text)}</span></button>`).join('') : empty}</div></section>`;
  }
  function spendChart() {
    const t = st.data.timeline, max = Math.max(1, ...t.map(x => x.spend || 0));
    if (!t.some(x => x.spend)) return '<p class="muted">Расходов нет.</p>';
    const W = Math.max(1000, t.length * 22), H = 220, P = 34, bw = (W - P * 2) / t.length, base = H - 24;
    const bars = t.map((x, i) => {
      const h = (x.spend || 0) / max * (H - 50), y = base - h, lbl = t.length <= 18 || i % Math.ceil(t.length / 18) === 0;
      return (h ? `<rect x="${P + i * bw + 2}" y="${y}" width="${Math.max(2, bw - 4)}" height="${h}" rx="3" fill="${x.orders ? '#7c3aed' : '#c4b5fd'}"/>` : '') +
        (lbl ? `<text x="${P + i * bw + bw / 2}" y="${H - 8}" class="fb-ax" text-anchor="middle">${ru(x.day).slice(0, 5)}</text>` : '') +
        `<rect x="${P + i * bw}" y="${P}" width="${bw}" height="${base - P}" fill="transparent"><title>${ru(x.day)}: расход ${num(x.spend)} ₽, заказов с рекламы ${num(x.orders)}, цена заказа ${rub(x.cpo)}, ДРР рекл. ${pct(x.drr)}, ДРР общий ${pct(x.drr_total)}, CTR ${pct(x.ctr, 2)}</title></rect>`;
    }).join('');
    return `<div class="fb-chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Расход по дням"><text x="${P}" y="16" class="fb-ax">${num(max)} ₽</text>
      <line x1="${P}" x2="${W - P}" y1="${base}" y2="${base}" stroke="#e5e7eb"/>${bars}</svg></div><p class="muted fb-sub">Светлые столбцы — дни без заказов с рекламы.</p>`;
  }
  function line(key, unit, color) {
    const days = st.data.timeline, pts = days.map((d, i) => [i, d[key]]).filter(p => p[1] != null);
    if (pts.length < 2) return '<p class="muted">Недостаточно данных.</p>';
    const W = 560, H = 170, P = 34, vals = pts.map(p => p[1]), lo = Math.min(...vals) * 0.9, hi = Math.max(...vals) * 1.08 || 1;
    const X = i => P + i / Math.max(1, days.length - 1) * (W - 2 * P), Y = v => H - 22 - (v - lo) / Math.max(1e-9, hi - lo) * (H - 40);
    const path = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(' ');
    return `<svg class="fb-line" viewBox="0 0 ${W} ${H}" role="img" aria-label="График"><text x="${P}" y="12" class="fb-ax">${num(hi)} ${unit}</text><text x="${P}" y="${H - 6}" class="fb-ax">${num(lo)} ${unit}</text>
      <path d="${path}" fill="none" stroke="${color}" stroke-width="2"/>${pts.map(p => `<circle cx="${X(p[0])}" cy="${Y(p[1])}" r="3.5" fill="${color}" stroke="#fff" stroke-width="1.5"><title>${ru(days[p[0]].day)}: ${num(p[1], 1)} ${unit}</title></circle>`).join('')}</svg>`;
  }
  function funnel(s) {
    const steps = [['Показы', s.views, null], ['Клики', s.clicks, s.ctr], ['Корзины', s.atbs, s.cart_cr], ['Заказы', s.orders, s.clicks ? s.orders / s.clicks * 100 : null]];
    const max = Math.max(1, s.views || 0);
    return `<div class="ad-funnel">${steps.map(([t, v, c], i) => `<div class="ad-fstep"><span>${t}</span><div class="ad-fbar"><i style="width:${Math.max(1.5, Math.sqrt((v || 0) / max) * 100)}%"></i></div>
      <b>${num(v)}</b><small class="muted">${i ? `${pct(c, 2)} ${i === 1 ? 'от показов' : 'от кликов'}` : ''}</small></div>`).join('')}</div>
      <p class="muted fb-sub">Ширина полос — в квадратном корне, чтобы были видны малые значения. Корзина → заказ: ${s.atbs ? pct(s.orders / s.atbs * 100) : '—'}.</p>`;
  }
  function platforms() {
    const pl = st.data.platforms, tot = pl.reduce((a, b) => a + (b.spend || 0), 0) || 1, colors = {1: '#4c6ef5', 32: '#2f9e44', 64: '#868e96'};
    if (!pl.length) return '<p class="muted">Нет данных.</p>';
    return `<div class="fb-bands">${pl.map(p => `<span style="width:${(p.spend || 0) / tot * 100}%;background:${colors[p.app] || '#adb5bd'}" title="${esc(p.title)}: ${num(p.spend)} ₽"></span>`).join('')}</div>
      <table class="fb-mini"><tr><th>Площадка</th><th class="r">Расход</th><th class="r">CTR</th><th class="r">CPC</th><th class="r">Заказы</th><th class="r">Цена заказа</th></tr>
      ${pl.map(p => `<tr><td><i style="background:${colors[p.app] || '#adb5bd'}"></i>${esc(p.title)}</td><td class="r"><b>${num(p.spend)} ₽</b> <small class="muted">${num((p.spend || 0) / tot * 100, 0)}%</small></td>
        <td class="r">${pct(p.ctr, 2)}</td><td class="r">${rub(p.cpc)}</td><td class="r">${num(p.orders)}</td><td class="r">${rub(p.cpo)}</td></tr>`).join('')}</table>`;
  }

  // ---------------------------------------------------------------- кампании
  const sorter = S => (a, b) => { const x = a[S.sort], y = b[S.sort];
    if (typeof x === 'string' || typeof y === 'string') return (S.desc ? -1 : 1) * String(x ?? '').localeCompare(String(y ?? ''), 'ru', {numeric: true});
    if (x == null) return 1; if (y == null) return -1; return (S.desc ? -1 : 1) * (x - y); };
  const thS = (S, attr) => (k, t, cls = '') => `<th class="${cls}"><button type="button" class="rp-sort ${S.sort === k ? 'on' : ''}" ${attr}="${k}">${t}<span>${S.sort === k ? (S.desc ? '↓' : '↑') : '↕'}</span></button></th>`;
  const drrCls = v => v == null ? '' : v >= 25 ? 'fb-hot' : v >= 15 ? 'fb-mid' : 'fb-cool';
  function delta(cur, prev) {
    if (prev == null) return '';
    if (!prev) return cur ? '<small class="muted">новая</small>' : '';
    const d = (cur - prev) / prev * 100;
    return `<small class="${d > 0 ? 'neg' : 'pos'}">${d > 0 ? '+' : ''}${num(d, 0)}%</small>`;
  }
  function campTable(rows, compact = false) {
    const S = st.cs, th = thS(S, 'data-csort'), has = !!st.data.previous;
    rows = compact ? rows : rows.slice().sort(sorter(S));
    return `<div class="rnp-scroll"><table class="rnp-table fb-groups ad-table"><thead><tr>${th('name', 'Кампания', 'l')}${compact ? '' : th('budget', 'Бюджет, ₽')}${th('spend', 'Расход, ₽')}
      ${th('views', 'Показы')}${th('clicks', 'Клики')}${th('ctr', 'CTR')}${th('cpc', 'CPC, ₽')}${compact ? '' : th('cpm', 'CPM, ₽')}${compact ? '' : th('atbs', 'Корзины')}${th('orders', 'Заказы')}
      ${th('cr', 'CR')}${th('cpo', 'Цена заказа')}${compact ? '' : th('sum_price', 'Сумма заказов, ₽')}${th('drr', 'ДРР')}</tr></thead><tbody>
      ${rows.map(c => `<tr class="pn-click" data-camp="${c.id}" title="Показать только эту кампанию"><td class="l ad-name"><b>${esc(c.name)}</b>
        <small><span class="ad-st" style="--c:${STC[c.status] || '#adb5bd'}">${esc(c.status_title)}</span> ${esc(PAY[c.payment_type] || c.payment_type)}${c.bid_type ? ' · ' + esc(BID[c.bid_type] || c.bid_type) : ''}${c.placements.length ? ' · ' + c.placements.map(p => esc(PLC[p] || p)).join(', ') : ''} · ${c.id}${st.data.options.cabinets.length > 1 ? ' · ' + esc(c.account) : ''}</small>
        <small class="muted">${c.articles.length ? esc(c.articles.join(', ')) + (c.nms_total > c.articles.length ? ` и др. (${c.nms_total})` : '') : `${c.nms_total} артикулов`}</small></td>
        ${compact ? '' : `<td class="${c.status === 9 && c.budget != null && c.budget < 500 ? 'neg' : ''}">${c.budget == null ? '—' : num(c.budget)}</td>`}
        <td><b>${num(c.spend)}</b>${has ? ' ' + delta(c.spend || 0, c.prev_spend) : ''}</td><td>${num(c.views)}</td><td>${num(c.clicks)}</td><td>${pct(c.ctr, 2)}</td><td>${num(c.cpc, 2)}</td>
        ${compact ? '' : `<td>${num(c.cpm)}</td><td>${num(c.atbs)}</td>`}<td class="${(c.spend || 0) >= 1000 && !c.orders ? 'fb-hot' : ''}">${num(c.orders)}</td><td>${pct(c.cr, 2)}</td><td>${rub(c.cpo)}</td>
        ${compact ? '' : `<td>${num(c.sum_price)}</td>`}<td class="${drrCls(c.drr)}">${pct(c.drr)}</td></tr>`).join('') || '<tr><td colspan="15" class="muted l">Нет кампаний.</td></tr>'}</tbody></table></div>`;
  }
  function bindCamp(body, redraw) {
    body.querySelectorAll('[data-camp]').forEach(r => r.onclick = () => { st.f.campaign = r.dataset.camp; remember(); st.tab = 'summary'; load(); });
    body.querySelectorAll('[data-csort]').forEach(b => b.onclick = e => { e.stopPropagation(); const k = b.dataset.csort; if (st.cs.sort === k) st.cs.desc = !st.cs.desc; else { st.cs.sort = k; st.cs.desc = true; } (redraw || drawSummary)(); });
  }
  function drawCampaigns() {
    const body = root.querySelector('[data-body]'), all = st.data.campaigns, S = st.cs;
    const groups = [['live', 'Активные и на паузе', c => c.status === 9 || c.status === 11], ['9', 'Активные', c => c.status === 9], ['11', 'На паузе', c => c.status === 11],
      ['spent', 'С расходом', c => c.spend > 0], ['waste', 'Тратят без заказов', c => (c.spend || 0) >= 1000 && !c.orders], ['all', 'Все', () => true]];
    const g = groups.find(x => x[0] === S.st) || groups[0], rows = all.filter(g[2]);
    body.innerHTML = `<div class="sk-chips">${groups.map(([k, t, fn]) => `<button type="button" class="sk-chip ${S.st === k ? 'on' : ''}" data-cst="${k}">${t} <b>${all.filter(fn).length}</b></button>`).join('')}
        <a class="db-ghost fb-csv" href="/api/ads/export.csv?${query({kind: 'campaigns'})}" download>⇩ CSV</a></div>
      ${campTable(rows)}
      <p class="muted rp-note">Нажмите на кампанию — вся страница покажет только её. ДРР: зелёный — до 15%, серый — 15–25%, красный — от 25%. Бюджет красным — осталось меньше 500 ₽.</p>`;
    body.querySelectorAll('[data-cst]').forEach(b => b.onclick = () => { S.st = b.dataset.cst; drawCampaigns(); });
    bindCamp(body, drawCampaigns);
  }

  // ---------------------------------------------------------------- артикулы
  function drawArticles() {
    const body = root.querySelector('[data-body]'), S = st.as, q = S.q.toLowerCase(), has = !!st.data.previous, th = thS(S, 'data-asort');
    const rows = st.data.articles.filter(r => !q || `${r.article} ${r.nm_id} ${r.title} ${r.brand}`.toLowerCase().includes(q)).sort(sorter(S));
    body.innerHTML = `<div class="rnp-tools"><input type="search" data-aq placeholder="Найти артикул" value="${esc(S.q)}"><span class="muted">${rows.length} артикулов в рекламе</span>
        <a class="db-ghost fb-csv" href="/api/ads/export.csv?${query({kind: 'articles'})}" download>⇩ CSV</a></div>
      <div class="rnp-scroll"><table class="rnp-table fb-groups ad-table"><thead><tr>${th('article', 'Артикул', 'l')}${th('campaigns', 'Кампаний')}${th('spend', 'Расход, ₽')}
        ${th('views', 'Показы')}${th('ctr', 'CTR')}${th('cpc', 'CPC, ₽')}${th('orders', 'Заказы с рекл.')}${th('cpo', 'Цена заказа')}${th('all_orders', 'Все заказы')}${th('ad_share', 'Доля рекл.')}
        ${th('all_orders_sum', 'Сумма всех, ₽')}${th('drr', 'ДРР рекл.')}${th('drr_total', 'ДРР общий')}</tr></thead><tbody>
      ${rows.slice(0, 400).map(a => `<tr><td class="l"><b>${esc(a.article || a.nm_id)}</b><small>${a.nm_id}${a.subject ? ' · ' + esc(a.subject) : ''}${a.title && !a.article ? ' · ' + esc(a.title) : ''}</small></td>
        <td>${num(a.campaigns)}</td><td><b>${num(a.spend)}</b>${has ? ' ' + delta(a.spend || 0, a.prev_spend) : ''}</td><td>${num(a.views)}</td><td>${pct(a.ctr, 2)}</td><td>${num(a.cpc, 2)}</td>
        <td class="${(a.spend || 0) >= 1000 && !a.orders ? 'fb-hot' : ''}">${num(a.orders)}</td><td>${rub(a.cpo)}</td><td>${num(a.all_orders)}</td><td>${pct(a.ad_share)}</td>
        <td>${num(a.all_orders_sum)}</td><td class="${drrCls(a.drr)}">${pct(a.drr)}</td><td class="${drrCls(a.drr_total)}">${pct(a.drr_total)}</td></tr>`).join('')
        || '<tr><td colspan="13" class="muted l">Нет данных.</td></tr>'}</tbody></table></div>
      <p class="muted rp-note">«Все заказы» — все заказы артикула за период без отмен (из РНП), в том числе органические. ДРР общий = расход / сумма всех заказов — показывает, сколько от оборота артикула уходит на рекламу.</p>`;
    body.querySelectorAll('[data-asort]').forEach(b => b.onclick = () => { const k = b.dataset.asort; if (S.sort === k) S.desc = !S.desc; else { S.sort = k; S.desc = true; } drawArticles(); });
    const s = body.querySelector('[data-aq]');
    s.oninput = () => { S.q = s.value; const pos = s.selectionStart; drawArticles(); const n = root.querySelector('[data-aq]'); n.focus(); n.setSelectionRange(pos, pos); };
  }

  // ---------------------------------------------------------------- поисковые запросы
  function drawQueries() {
    const body = root.querySelector('[data-body]'), S = st.qs, q = S.q.toLowerCase(), th = thS(S, 'data-qsort'), all = st.data.clusters, s = st.data.summary;
    const modes = [['', 'Все', () => true], ['waste', 'Тратят без заказов', c => (c.spend || 0) >= 100 && !c.orders],
      ['best', 'Дешёвый заказ', c => c.orders && s.cpo && c.cpo <= s.cpo * 0.7], ['low', 'Низкая позиция (>20)', c => c.avg_pos > 20]];
    const m = modes.find(x => x[0] === S.mode) || modes[0];
    const rows = all.filter(m[2]).filter(c => !q || c.query.toLowerCase().includes(q)).sort(sorter(S));
    const waste = all.filter(modes[1][2]).reduce((a, c) => a + (c.spend || 0), 0);
    body.innerHTML = `<div class="sk-chips">${modes.map(([k, t, fn]) => `<button type="button" class="sk-chip ${S.mode === k ? 'on' : ''}" data-qm="${k}">${t} <b>${all.filter(fn).length}</b></button>`).join('')}
        <input type="search" data-qq placeholder="Найти запрос" value="${esc(S.q)}" class="ad-qsearch"><a class="db-ghost fb-csv" href="/api/ads/export.csv?${query({kind: 'clusters'})}" download>⇩ CSV</a></div>
      ${waste ? `<div class="pn-check warn">! Запросы без заказов съели ${num(waste)} ₽. Добавьте нерелевантные в минус-фразы кампании в кабинете WB.</div>` : ''}
      <div class="rnp-scroll"><table class="rnp-table fb-groups ad-table"><thead><tr>${th('query', 'Поисковый запрос', 'l')}${th('views', 'Показы')}${th('clicks', 'Клики')}${th('ctr', 'CTR')}
        ${th('cpc', 'CPC, ₽')}${th('avg_pos', 'Ср. позиция')}${th('atbs', 'Корзины')}${th('orders', 'Заказы')}${th('cr', 'CR')}${th('spend', 'Расход, ₽')}${th('cpo', 'Цена заказа')}<th class="l">Артикулы</th></tr></thead><tbody>
      ${rows.slice(0, 500).map(c => `<tr><td class="l"><b>${esc(c.query)}</b>${c.campaigns > 1 ? `<small>${c.campaigns} кампании</small>` : ''}</td><td>${num(c.views)}</td><td>${num(c.clicks)}</td><td>${pct(c.ctr, 2)}</td>
        <td>${num(c.cpc, 2)}</td><td class="${c.avg_pos > 20 ? 'fb-mid' : c.avg_pos && c.avg_pos <= 5 ? 'fb-cool' : ''}">${num(c.avg_pos, 1)}</td><td>${num(c.atbs)}</td>
        <td class="${(c.spend || 0) >= 100 && !c.orders ? 'fb-hot' : ''}">${num(c.orders)}</td><td>${pct(c.cr, 2)}</td><td><b>${num(c.spend)}</b></td>
        <td class="${c.cpo && s.cpo && c.cpo <= s.cpo * 0.7 ? 'fb-cool' : ''}">${rub(c.cpo)}</td><td class="l"><small>${esc(c.articles.join(', '))}</small></td></tr>`).join('')
        || '<tr><td colspan="12" class="muted l">Нет данных по запросам. WB отдаёт их для активных и приостановленных кампаний; запрос попадает в статистику после 100 показов.</td></tr>'}</tbody></table></div>
      <p class="muted rp-note">Поисковые кластеры — сгруппированные WB запросы покупателей. Загружаются за последние 30 дней${st.data.clusters_from ? ` (есть с ${ru(st.data.clusters_from)})` : ''}. Всего запросов: ${num(st.data.clusters_total)}.</p>`;
    body.querySelectorAll('[data-qm]').forEach(b => b.onclick = () => { S.mode = b.dataset.qm; drawQueries(); });
    body.querySelectorAll('[data-qsort]').forEach(b => b.onclick = () => { const k = b.dataset.qsort; if (S.sort === k) S.desc = !S.desc; else { S.sort = k; S.desc = true; } drawQueries(); });
    const sq = body.querySelector('[data-qq]');
    sq.oninput = () => { S.q = sq.value; const pos = sq.selectionStart; drawQueries(); const n = root.querySelector('[data-qq]'); n.focus(); n.setSelectionRange(pos, pos); };
  }

  // ---------------------------------------------------------------- списания
  function drawSpending() {
    const body = root.querySelector('[data-body]'), sp = st.data.spending, s = st.data.summary;
    const diff = sp.total - (s.spend || 0);
    body.innerHTML = `<div class="db-grid fb-grid ad-bal">${Object.entries(sp.balances).map(([acc, b]) => `<article class="db-tile neutral"><div class="db-title">${esc(acc)}</div>
        <div class="db-value">${num(b.net)}<sup>₽</sup></div><div class="db-change"><span class="db-fixed">баланс · счёт ${num(b.balance)} ₽ · бонусы ${num(b.bonus)} ₽</span></div></article>`).join('')}
        <article class="db-tile neutral"><div class="db-title">В бюджетах кампаний</div><div class="db-value">${num(s.budget_left)}<sup>₽</sup></div><div class="db-change"><span class="db-fixed">активные и на паузе</span></div></article></div>
      <div class="fb-two"><section class="tr-box"><h3>Списано WB за период</h3><div class="pn-big">${num(sp.total)} ₽</div>
          <p class="muted">${num(sp.count)} списаний. По статистике кампаний расход ${num(s.spend)} ₽${Math.abs(diff) >= 1 ? ` — разница ${diff > 0 ? '+' : ''}${num(diff)} ₽: WB списывает деньги пополнением бюджета и с задержкой, поэтому суммы по дням могут не совпадать.` : ' — совпадает.'}</p></section>
        <section class="tr-box"><h3>Откуда списано</h3><table class="fb-mini">${Object.entries(sp.by_source).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="r"><b>${num(v)} ₽</b></td></tr>`).join('') || '<tr><td class="muted">Нет списаний.</td></tr>'}</table></section></div>
      <section class="tr-box"><h3>История списаний</h3><div class="rnp-scroll"><table class="rnp-table fb-feed"><thead><tr><th class="l">Когда</th><th class="l">Кампания</th><th class="l">Источник</th><th>Сумма</th></tr></thead><tbody>
        ${sp.items.map(u => `<tr><td class="l">${dt(u.time)}</td><td class="l">${esc(u.campaign)}<small>${u.advert_id}${st.data.options.cabinets.length > 1 ? ' · ' + esc(u.account) : ''}</small></td><td class="l">${esc(u.source)}</td><td><b>${num(u.amount, 2)} ₽</b></td></tr>`).join('')
          || '<tr><td colspan="4" class="muted l">Списаний нет.</td></tr>'}</tbody></table></div></section>`;
  }

  const style = document.createElement('style');
  style.textContent = `
.ad-hero{background:linear-gradient(135deg,#f3f0ff,#eef2ff);border-color:#d9d2fb}.ad-hero b{color:#5b21b6}.ad-hero span,.ad-hero p{color:#4c3d7a}.ad-hero .fb-lost-side{border-color:#d9d2fb}
.ad-camp-sel{max-width:260px}
.ad-ins-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(340px,100%),1fr));gap:10px}
.ad-ins-item{display:flex;flex-direction:column;gap:4px;text-align:left;border:1px solid #e5e7eb;border-left:4px solid #f59f00;border-radius:12px;padding:10px 12px;background:#fff;font:inherit;cursor:pointer;min-width:0;min-height:112px;height:100%;box-sizing:border-box;overflow:hidden}
.ad-ins-item.bad{border-left-color:#e03131}.ad-ins-item:hover{background:#faf7ff}.ad-ins-item b{font-size:14px}.ad-ins-item span{font-size:13px;color:#4b5563;line-height:1.4}
.ad-ins-legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin:-4px 0 12px;color:#6b7280;font-size:12px;line-height:1.4}.ad-ins-legend-item{display:inline-flex;align-items:center;gap:5px}.ad-ins-legend-item i{display:inline-block;width:8px;height:8px;border-radius:50%;background:#f59f00;flex:none}.ad-ins-legend-item.bad i{background:#e03131}.ad-ins-legend-item b{color:#374151}
.ad-ins-filters{display:flex;flex-wrap:wrap;gap:6px;margin:-4px 0 12px}.ad-ins-filter{border:1px solid #e5e7eb;border-radius:999px;background:#f8f9fb;color:#4b5563;padding:6px 10px;font:inherit;font-size:12px;cursor:pointer}.ad-ins-filter b{margin-left:3px}.ad-ins-filter.active{background:#111827;border-color:#111827;color:#fff}.ad-ins-filter.bad.active{background:#e03131;border-color:#e03131}.ad-ins-filter.warn.active{background:#f59f00;border-color:#f59f00;color:#111827}.ad-ins-empty{grid-column:1/-1;padding:16px;border:1px dashed #d1d5db;border-radius:10px;text-align:center}
.ad-funnel{display:grid;gap:10px}.ad-fstep{display:grid;grid-template-columns:80px 1fr 90px 120px;align-items:center;gap:10px;font-size:13px}
.ad-fbar{height:16px;background:#f1f2f4;border-radius:6px;overflow:hidden}.ad-fbar i{display:block;height:100%;background:#7c3aed;border-radius:6px}.ad-fstep b{text-align:right;font-variant-numeric:tabular-nums}
.ad-name small{display:block;font-size:11px;font-weight:400;margin-top:2px}.ad-name{min-width:280px;max-width:420px;white-space:normal}
.ad-table{min-width:1180px}.ad-table th,.ad-table td{vertical-align:middle}.ad-table th:not(.l),.ad-table td:not(.l){white-space:nowrap}.ad-table .ad-name{width:30%;min-width:280px;max-width:420px;text-align:left}.ad-table .ad-name b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.ad-table .ad-name small{overflow-wrap:anywhere}.ad-table tbody tr{height:48px}
.ad-page .db-filters{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));align-items:stretch;gap:10px}.ad-page .db-filters select,.ad-page .db-filters input{width:100%;min-width:0;max-width:none;box-sizing:border-box}.ad-page .db-filters .db-period{min-width:0}.ad-page .db-filters .db-date{display:grid;grid-template-columns:auto minmax(0,1fr);align-items:center;min-width:0}.ad-page .db-filters .db-date input{width:100%}.ad-page .db-filters .rp-tools{display:flex;align-items:center;min-width:0;margin-left:0}.ad-page .db-filters .rp-tools .db-ghost{width:100%;white-space:nowrap}
.ad-st{display:inline-flex;align-items:center;gap:4px;font-weight:600;color:#374151}.ad-st::before{content:'';width:7px;height:7px;border-radius:50%;background:var(--c)}
.ad-qsearch{height:34px;border:1px solid #e3e5ea;border-radius:9px;padding:0 10px;font:inherit;margin-left:auto}
@media(max-width:1100px){.ad-page .db-filters{grid-template-columns:repeat(3,minmax(0,1fr))}.ad-fstep{grid-template-columns:72px minmax(0,1fr) 80px}.ad-fstep small{display:none}}
@media(max-width:760px){.ad-page .db-filters{grid-template-columns:repeat(2,minmax(0,1fr))}.ad-fstep{grid-template-columns:64px minmax(0,1fr) 70px}.ad-ins-list{grid-template-columns:1fr}.ad-table{min-width:980px}.ad-table .ad-name{min-width:240px}}
@media(max-width:520px){.ad-page .db-filters{grid-template-columns:1fr}.ad-fstep{grid-template-columns:1fr auto;gap:6px}.ad-fstep .ad-fbar{grid-column:1/-1}.ad-fstep b{grid-column:2;grid-row:1}.ad-table{min-width:900px}}
`;
  document.head.appendChild(style);

  return {render};
})();
