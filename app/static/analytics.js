/* WB OPS: раздел «Аналитика и отчёты».
   Вкладки: Дашборд · Детально · Теги · Тренды · По товару · Отчёты. Данные — /api/analytics/*.
   Графики — простые SVG без внешних библиотек; подсказки по наведению; PDF — через печать страницы. */
window.WBOPSAnalytics = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const fmt = n => (n ?? 0).toLocaleString('ru-RU');
  const pct = (v, digits = 0) => v == null ? '—' : (v * 100).toFixed(digits).replace('.', ',') + '%';
  const iso = d => d.toISOString().slice(0, 10);
  const addDays = (s, n) => { const d = new Date(s + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return iso(d); };
  const MONTHS = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
  const keyLabel = k => k.length === 7 ? `${MONTHS[+k.slice(5, 7) - 1]} ${k.slice(2, 4)}` : `${k.slice(8, 10)}.${k.slice(5, 7)}`;
  const keyTitle = (k, grain) => k.length === 7 ? `${MONTHS[+k.slice(5, 7) - 1]} ${k.slice(0, 4)}` : (grain === 'week' ? `неделя с ${keyLabel(k)}.${k.slice(0, 4)}` : `${keyLabel(k)}.${k.slice(0, 4)}`);
  const minutesText = m => {
    if (m == null) return '—';
    if (m < 60) return `${Math.round(m)} мин`;
    if (m < 60 * 24) return `${Math.floor(m / 60)} ч ${Math.round(m % 60)} мин`;
    return `${Math.floor(m / 1440)} д ${Math.round((m % 1440) / 60)} ч`;
  };

  // Цвета: категориальная палитра в фиксированном порядке; цвет закреплён за сущностью.
  const MP_COLORS = {Wildberries: '#6250d6', Ozon: '#2a78d6', 'Яндекс Маркет': '#eda100'};
  const RATING_COLORS = {5: '#2a78d6', 4: '#8fb8ea', 3: '#c4c2bd', 2: '#f0a39a', 1: '#e34948'};
  const SENT = {positive: ['Позитивные', '#2a78d6'], neutral: ['Нейтральные', '#c4c2bd'], negative: ['Негативные', '#e34948']};
  const METHOD_COLORS = {ai: '#6250d6', individual: '#2a78d6', templates: '#eda100', knowledge: '#1baf7a', manual: '#eb6834'};
  const CAT = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948'];
  const KINDS = {review: 'Отзывы', question: 'Вопросы', chat: 'Чаты'};

  const state = {tab: 'dashboard', kind: 'review', marketplace: '', cabinet: '', preset: '30', from: '', to: '', grain: 'day',
                 filters: null, product: null, productData: null, products: null, reportSearch: ''};
  let root = null;

  async function api(url, options) {
    const r = await fetch(url, options);
    let body = null;
    try { body = await r.json(); } catch (e) { /* пустой ответ */ }
    if (!r.ok) throw new Error((body && body.detail) || `Ошибка ${r.status}`);
    return body;
  }
  const query = (extra = {}) => {
    const p = new URLSearchParams({from: state.from, to: state.to, ...extra});
    if (state.marketplace) p.set('marketplace', state.marketplace);
    if (state.cabinet) p.set('cabinet', state.cabinet);
    return p.toString();
  };

  // ---------------------------------------------------------------- графики
  const niceMax = v => {
    if (v <= 5) return 5;
    const p = Math.pow(10, Math.floor(Math.log10(v))), n = v / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
  };
  const legend = items => `<div class="an-legend">${items.map(i =>
    `<span><i style="background:${i.color}"></i>${esc(i.label)}${i.value != null ? ` <b>${esc(i.value)}</b>` : ''}</span>`).join('')}</div>`;

  /** Столбцы с накоплением. series: [{label, color, values: {key: n}}] */
  function stacked(keys, series, {grain = 'day', height = 220, unit = ''} = {}) {
    const W = 760, H = height, L = 38, B = 24, T = 8, R = 6;
    const totals = keys.map(k => series.reduce((s, x) => s + (x.values[k] || 0), 0));
    const max = niceMax(Math.max(1, ...totals));
    const band = (W - L - R) / Math.max(1, keys.length), bw = Math.max(3, Math.min(30, band * 0.68));
    const y = v => T + (H - T - B) * (1 - v / max);
    let svg = '';
    for (let i = 0; i <= 4; i++) {
      const v = max * i / 4, yy = y(v);
      svg += `<line x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}" class="an-grid"/><text x="${L - 6}" y="${yy + 4}" text-anchor="end" class="an-axis">${fmt(Math.round(v))}</text>`;
    }
    const step = Math.ceil(keys.length / 12);
    keys.forEach((k, i) => {
      const x = L + band * i + (band - bw) / 2;
      let acc = 0;
      series.forEach(s => {
        const v = s.values[k] || 0;
        if (!v) return;
        const y1 = y(acc + v), y0 = y(acc);
        const h = Math.max(1, y0 - y1 - (acc ? 1.5 : 0));
        svg += `<rect x="${x}" y="${y1}" width="${bw}" height="${h}" rx="2" fill="${s.color}"/>`;
        acc += v;
      });
      const tip = `<b>${esc(keyTitle(k, grain))}</b>` + series.filter(s => s.values[k]).map(s =>
        `<div><i style="background:${s.color}"></i>${esc(s.label)}: <b>${fmt(s.values[k])}${unit}</b></div>`).join('') +
        (series.length > 1 ? `<div class="an-tip-total">Всего: <b>${fmt(totals[i])}</b></div>` : '');
      svg += `<rect x="${L + band * i}" y="${T}" width="${band}" height="${H - T - B}" fill="transparent" data-tip="${esc(tip)}"/>`;
      if (i % step === 0) svg += `<text x="${L + band * i + band / 2}" y="${H - 6}" text-anchor="middle" class="an-axis">${esc(keyLabel(k))}</text>`;
    });
    return `<svg viewBox="0 0 ${W} ${H}" class="an-svg" role="img">${svg}</svg>`;
  }

  /** Кольцевая диаграмма долей. items: [{label, color, value}] */
  function donut(items, centerLabel = 'всего') {
    const total = items.reduce((s, i) => s + i.value, 0);
    const r = 52, C = 2 * Math.PI * r;
    let off = 0, arcs = '';
    if (total) items.filter(i => i.value).forEach(i => {
      const len = C * i.value / total, gap = items.filter(x => x.value).length > 1 ? 2 : 0;
      arcs += `<circle r="${r}" cx="70" cy="70" fill="none" stroke="${i.color}" stroke-width="18" stroke-dasharray="${Math.max(0.5, len - gap)} ${C}" stroke-dashoffset="${-off}" transform="rotate(-90 70 70)" data-tip="${esc(`<b>${esc(i.label)}</b><div>${fmt(i.value)} · ${pct(i.value / total, 1)}</div>`)}"/>`;
      off += len;
    });
    else arcs = `<circle r="${r}" cx="70" cy="70" fill="none" stroke="#eceef3" stroke-width="18"/>`;
    const rows = items.map(i => `<li><i style="background:${i.color}"></i><span>${esc(i.label)}</span><b>${fmt(i.value)}</b><small>${total ? pct(i.value / total) : '—'}</small></li>`).join('');
    return `<div class="an-donut"><svg viewBox="0 0 140 140" role="img">${arcs}<text x="70" y="68" text-anchor="middle" class="an-donut-v">${fmt(total)}</text><text x="70" y="86" text-anchor="middle" class="an-axis">${esc(centerLabel)}</text></svg><ul>${rows}</ul></div>`;
  }

  /** Горизонтальные полосы. items: [{label, value, color?}] */
  function hbars(items, color = '#2a78d6') {
    if (!items.length) return '<p class="muted">Нет данных за период.</p>';
    const max = Math.max(1, ...items.map(i => i.value));
    return `<div class="an-hbars">${items.map(i => `<div data-tip="${esc(`<b>${esc(i.label)}</b><div>${fmt(i.value)}</div>`)}"><span>${esc(i.label)}</span><div><i style="width:${Math.max(1, i.value / max * 100)}%;background:${i.color || color}"></i></div><b>${fmt(i.value)}</b></div>`).join('')}</div>`;
  }

  const card = (title, body, extra = '') => `<article class="an-card ${extra}"><h3>${title}</h3>${body}</article>`;
  const empty = text => `<p class="muted an-empty">${esc(text)}</p>`;
  const kpi = (label, value, note = '') => `<article class="an-kpi"><small>${esc(label)}</small><strong>${value}</strong>${note ? `<span>${note}</span>` : ''}</article>`;
  const change = (c, prev) => {
    if (c == null) return prev ? '' : '<span class="muted">нет данных за прошлый период</span>';
    const arrow = c > 0 ? '▲' : c < 0 ? '▼' : '■';
    return `<em class="${c > 0 ? 'up' : c < 0 ? 'down' : ''}">${arrow} ${pct(Math.abs(c))}</em> <span class="muted">к прошлому периоду (${fmt(prev)})</span>`;
  };

  // ---------------------------------------------------------------- фильтры и каркас
  function applyPreset() {
    const today = iso(new Date());
    if (state.preset === 'custom') return;
    const days = +state.preset;
    state.to = today;
    state.from = addDays(today, -(days - 1));
    state.grain = days <= 31 ? 'day' : days <= 120 ? 'week' : 'month';
  }

  function filtersHtml({kind = true, scope = true, grain = true} = {}) {
    const f = state.filters || {marketplaces: {}, cabinets: []};
    const opt = (v, t, cur) => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(t)}</option>`;
    return `<div class="an-filters an-noprint">
      ${kind ? `<div class="an-seg" role="tablist">${Object.entries(KINDS).map(([k, t]) => `<button type="button" data-kind="${k}" class="${state.kind === k ? 'on' : ''}">${t}</button>`).join('')}</div>` : ''}
      ${scope ? `<select data-f="marketplace" aria-label="Маркетплейс">${opt('', 'Все маркетплейсы', state.marketplace)}${Object.entries(f.marketplaces).map(([k, v]) => opt(k, v, state.marketplace)).join('')}</select>
      <select data-f="cabinet" aria-label="Кабинет">${opt('', 'Все кабинеты', state.cabinet)}${f.cabinets.map(c => opt(c.id, c.name, state.cabinet)).join('')}</select>` : ''}
      <select data-f="preset" aria-label="Период">${[['7', '7 дней'], ['30', '30 дней'], ['90', '90 дней'], ['365', 'Год'], ['custom', 'Свой период']].map(([v, t]) => opt(v, t, state.preset)).join('')}</select>
      <label class="an-date">с <input type="date" data-f="from" value="${state.from}"></label>
      <label class="an-date">по <input type="date" data-f="to" value="${state.to}"></label>
      ${grain ? `<select data-f="grain" aria-label="Шаг">${[['day', 'По дням'], ['week', 'По неделям'], ['month', 'По месяцам']].map(([v, t]) => opt(v, t, state.grain)).join('')}</select>` : ''}
    </div>`;
  }

  function bindFilters(el, reload) {
    el.querySelectorAll('[data-kind]').forEach(b => b.onclick = () => { state.kind = b.dataset.kind; reload(); });
    el.querySelectorAll('[data-f]').forEach(input => input.onchange = () => {
      const k = input.dataset.f;
      if (k === 'preset') { state.preset = input.value; applyPreset(); }
      else if (k === 'from' || k === 'to') { state[k] = input.value; state.preset = 'custom'; if (state.from > state.to) return toast('Дата «с» позже даты «по»'); }
      else state[k] = input.value;
      reload();
    });
  }

  function toast(text) {
    const t = document.getElementById('toast');
    if (t) { t.textContent = text; t.style.display = 'block'; clearTimeout(toast.timer); toast.timer = setTimeout(() => t.style.display = 'none', 3500); }
    else alert(text);
  }

  const TITLES = {dashboard: 'Общая аналитика', detailed: 'Детальная аналитика', tags: 'Позитив и негатив по тегам', trends: 'Тренды товаров', product: 'Аналитика по товару', reports: 'Сохранённые отчёты'};
  const TABS = {dashboard: 'Дашборд', detailed: 'Детально', tags: 'Теги', trends: 'Тренды', product: 'По товару', reports: 'Отчёты'};

  async function render(section) {
    root = section;
    if (!state.from) applyPreset();
    root.innerHTML = `<div class="an-head"><h2 data-an-title>Общая аналитика</h2>
      <nav class="an-tabs an-noprint">${Object.entries(TABS).map(([k, t]) => `<button type="button" data-tab="${k}">${t}</button>`).join('')}</nav></div>
      <div data-an-body></div>`;
    root.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => openTab(b.dataset.tab));
    if (!state.filters) {
      try { state.filters = await api('/api/analytics/filters'); } catch (e) { state.filters = {marketplaces: {}, cabinets: []}; }
    }
    openTab(state.tab);
  }

  function openTab(tab) {
    state.tab = tab;
    root.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
    root.querySelector('[data-an-title]').textContent = TITLES[tab];
    ({dashboard: renderDashboard, detailed: renderDetailed, tags: renderTags, trends: renderTrends, product: renderProduct, reports: renderReports})[tab]();
  }
  const body = () => root.querySelector('[data-an-body]');
  const loading = (filters = '') => { body().innerHTML = filters + '<p class="muted">Загрузка…</p>'; };
  const fail = (e, filters = '') => { body().innerHTML = filters + `<p class="an-error">Не удалось загрузить: ${esc(e.message)}</p>`; };

  // ---------------------------------------------------------------- Дашборд
  async function renderDashboard() {
    const f = filtersHtml();
    loading(f); bindFilters(body(), renderDashboard);
    let d;
    try { d = await api(`/api/analytics/dashboard?${query({kind: state.kind, grain: state.grain})}`); } catch (e) { fail(e, f); bindFilters(body(), renderDashboard); return; }
    const k = d.kpi, noun = {review: 'отзывов', question: 'вопросов', chat: 'чатов'}[state.kind];
    const series = Object.entries(d.series).map(([mp, values], i) => ({label: mp, color: MP_COLORS[mp] || CAT[i], values}));
    const mpItems = Object.entries(d.marketplaces).map(([mp, v], i) => ({label: mp, value: v, color: MP_COLORS[mp] || CAT[i]}));
    const exportQ = query({kind: state.kind, grain: state.grain});
    let html = f + `<div class="an-actions an-noprint">
        <a class="btn secondary" href="/api/analytics/dashboard?${exportQ}&format=xlsx">⇩ Excel</a>
        <a class="btn secondary" href="/api/analytics/dashboard?${exportQ}&format=csv">⇩ CSV</a>
        <button class="btn secondary" type="button" data-print>PDF / печать</button></div>
      <p class="muted an-period">${KINDS[state.kind]} за ${esc(state.from.split('-').reverse().join('.'))} — ${esc(state.to.split('-').reverse().join('.'))}</p>
      <div class="an-kpis">
        ${kpi(`Новых ${noun}`, fmt(k.new), change(k.change, k.prev))}
        ${kpi('Ожидают ответа', fmt(k.waiting), '<span class="muted">все неотвеченные сейчас</span>')}
        ${kpi('Отвечено за период', fmt(k.answered), k.new ? `<span class="muted">${pct(k.answered / k.new)} от новых</span>` : '')}
        ${kpi('Время ответа (медиана)', minutesText(k.response_minutes), '<span class="muted">от появления до отправки</span>')}
      </div>`;
    if (!k.new) {
      html += empty(state.kind === 'chat' ? 'Чатов за период нет: синхронизация чатов пока не загружала диалоги.' : `За выбранный период ${noun} нет. Выберите другой период или загрузите историю в разделе «Импорт».`);
      body().innerHTML = html; bindFilters(body(), renderDashboard); bindPrint(); return;
    }
    html += `<div class="an-row">
        ${card(`Динамика ${noun} по маркетплейсам`, legend(series.map(s => ({...s, value: fmt(Object.values(s.values).reduce((a, b) => a + b, 0))}))) + stacked(d.keys, series, {grain: d.grain}), 'wide')}
        ${card('Доля маркетплейсов', donut(mpItems, noun))}
      </div>
      ${card('По кабинетам', `<div class="an-tw"><table><thead><tr><th>Кабинет</th><th>Маркетплейс</th><th>Всего</th><th>Доля</th><th>Отвечено</th><th>Без ответа</th><th>Отвечено, %</th></tr></thead><tbody>
        ${d.cabinets.map(c => `<tr><td>${esc(cabName(c.cabinet))}</td><td><span class="an-mp" style="--c:${MP_COLORS[c.marketplace] || '#6b7280'}">${esc(c.marketplace)}</span></td><td>${fmt(c.total)}</td><td>${pct(c.share, 1)}</td><td>${fmt(c.answered)}</td><td>${fmt(c.unanswered)}</td><td><div class="an-meter"><i style="width:${c.answered_share * 100}%"></i></div>${pct(c.answered_share)}</td></tr>`).join('')}
      </tbody></table></div>`)}`;
    if (state.kind === 'review') {
      const rSeries = ['5', '4', '3', '2', '1'].map(r => ({label: `${r}★`, color: RATING_COLORS[r], values: Object.fromEntries(d.keys.map(key => [key, d.ratings_series[key][r]]))}));
      html += `<div class="an-row">
          ${card(`Анализ оценок <span class="an-sub">средняя ${d.avg_rating ? d.avg_rating.toFixed(2).replace('.', ',') : '—'}★</span>`, legend(rSeries.map(s => ({label: s.label, color: s.color}))) + stacked(d.keys, rSeries, {grain: d.grain}), 'wide')}
          ${card('Распределение оценок', donut(['5', '4', '3', '2', '1'].map(r => ({label: `${r}★`, value: d.ratings[r], color: RATING_COLORS[r]})), 'оценок'))}
        </div>`;
    }
    html += `<div class="an-row">${state.kind !== 'review' ? '' : card('С текстом и без', donut([
        {label: 'С текстом', value: d.text.with_text, color: '#2a78d6'}, {label: 'Без текста', value: d.text.without_text, color: '#c4c2bd'}], noun))}
      ${card('Сравнение периодов', comparePeriods(k, noun))}</div>`;
    body().innerHTML = html;
    bindFilters(body(), renderDashboard); bindPrint();
  }
  const cabName = id => ((state.filters && state.filters.cabinets) || []).find(c => c.id === id)?.name || id;
  function comparePeriods(k, noun) {
    const span = Math.round((new Date(state.to) - new Date(state.from)) / 864e5) + 1;
    const items = [{label: `Прошлые ${span} дн.`, value: k.prev, color: '#c4c2bd'}, {label: 'Текущий период', value: k.new, color: '#2a78d6'}];
    return hbars(items) + `<p class="muted">Новых ${noun}: ${fmt(k.prev)} → ${fmt(k.new)}${k.change != null ? ` (${k.change > 0 ? '+' : ''}${pct(k.change)})` : ''}.</p>`;
  }
  function bindPrint() { root.querySelectorAll('[data-print]').forEach(b => b.onclick = () => window.print()); }

  // ---------------------------------------------------------------- Детально
  async function renderDetailed() {
    const f = filtersHtml();
    loading(f); bindFilters(body(), renderDetailed);
    let d;
    try { d = await api(`/api/analytics/detailed?${query({kind: state.kind, grain: state.grain})}`); } catch (e) { fail(e, f); bindFilters(body(), renderDetailed); return; }
    let html = f + `<div class="an-actions an-noprint"><button class="btn secondary" type="button" data-print>PDF / печать</button></div>`;
    if (!d.total) { body().innerHTML = html + empty('За выбранный период данных нет.'); bindFilters(body(), renderDetailed); bindPrint(); return; }
    const m = d.methods, mKeys = Object.keys(m.titles);
    const mSeries = mKeys.map(key => ({label: m.titles[key], color: METHOD_COLORS[key], values: Object.fromEntries(d.keys.map(p => [p, m.series[p][key]]))}));
    const answered = Object.values(m.total).reduce((a, b) => a + b, 0);
    html += `<p class="muted an-period">Способ ответа — как был подготовлен отправленный текст: ИИ, сборщик, шаблон, прошлый ответ или вручную (оператор изменил черновик). Ответы, отправленные до этой версии, учтены как «Шаблоны».</p>
      <div class="an-row">${card('Ответы по способу — динамика', answered ? legend(mSeries.filter(s => m.total[mKeys[mSeries.indexOf(s)]])) + stacked(d.keys, mSeries, {grain: d.grain}) : empty('Отправленных ответов за период нет.'), 'wide')}
        ${card('Доля способов', donut(mKeys.map(key => ({label: m.titles[key], value: m.total[key] || 0, color: METHOD_COLORS[key]})), 'ответов'))}</div>`;
    if (answered && state.kind === 'review') {
      html += card('Способ ответа: отзывы с текстом и без', `<div class="an-tw"><table><thead><tr><th>Способ</th><th>С текстом</th><th>Без текста</th><th>Всего</th></tr></thead><tbody>
        ${mKeys.filter(key => m.total[key]).map(key => `<tr><td><i class="an-dot" style="background:${METHOD_COLORS[key]}"></i>${esc(m.titles[key])}</td><td>${fmt(m.with_text[key] || 0)}</td><td>${fmt(m.without_text[key] || 0)}</td><td><b>${fmt(m.total[key])}</b></td></tr>`).join('')}
      </tbody></table></div>`);
    }
    if (state.kind === 'review') {
      const sSeries = Object.entries(SENT).map(([key, [label, color]]) => ({label, color, values: Object.fromEntries(d.keys.map(p => [p, d.sentiment.series[p][key]]))}));
      const t = d.tags;
      const tSeries = t.top.map((tag, i) => ({label: t.titles[tag], color: CAT[i], values: Object.fromEntries(d.keys.map(p => [p, t.series[p][tag]]))}));
      html += `<div class="an-row">${card('Тональность — динамика', legend(sSeries) + stacked(d.keys, sSeries, {grain: d.grain}), 'wide')}
          ${card('Доля тональности', donut(Object.entries(SENT).map(([key, [label, color]]) => ({label, color, value: d.sentiment.total[key] || 0})), 'отзывов'))}</div>
        <div class="an-kpis">
          ${kpi('Отзывов с фото', fmt(d.media.photo), `<span class="muted">${pct(d.media.photo / d.media.total, 1)} от всех</span>`)}
          ${kpi('Отзывов с видео', fmt(d.media.video), `<span class="muted">${pct(d.media.video / d.media.total, 1)} от всех</span>`)}
          ${kpi('Без фото и видео', fmt(d.media.total - Math.max(d.media.photo, d.media.video)), '<span class="muted">оценка: фото и видео могут совпадать</span>')}
        </div>
        <div class="an-row">${card('Теги — динамика (топ-6)', tSeries.length ? legend(tSeries) + stacked(d.keys, tSeries, {grain: d.grain}) : empty('Теги в отзывах не найдены.'), 'wide')}
          ${card('Популярность тегов', hbars(t.popularity.map((p, i) => ({label: p.title, value: p.count, color: CAT[t.top.indexOf(p.tag)] || '#8fb8ea'}))))}</div>`;
    }
    body().innerHTML = html;
    bindFilters(body(), renderDetailed); bindPrint();
  }

  // ---------------------------------------------------------------- Теги
  async function renderTags() {
    const f = filtersHtml({kind: false, grain: false});
    loading(f); bindFilters(body(), renderTags);
    let d;
    try { d = await api(`/api/analytics/tags?${query()}`); } catch (e) { fail(e, f); bindFilters(body(), renderTags); return; }
    body().innerHTML = f + `<p class="muted an-period">Отзывов за период: ${fmt(d.total)}. Тег «позитив» — покупатель хвалит это, «негатив» — жалуется.</p>` +
      `<div class="an-row">${card('Позитив по тегам', tagTable(d, 'positive'))}${card('Негатив по тегам', tagTable(d, 'negative'))}</div>` +
      card('Баланс по тегам', balance(d.tags));
    bindFilters(body(), renderTags);
  }
  function tagTable(d, polarity) {
    const rows = d.tags.filter(t => t[polarity === 'positive' ? 'pos_total' : 'neg_total']).sort((a, b) => b[polarity === 'positive' ? 'pos_total' : 'neg_total'] - a[polarity === 'positive' ? 'pos_total' : 'neg_total']);
    if (!rows.length) return empty('Нет упоминаний.');
    return `<div class="an-tw"><table><thead><tr><th>Тег</th>${d.marketplaces.map(mp => `<th>${esc(mp)}</th>`).join('')}<th>Всего</th></tr></thead><tbody>${rows.map(t =>
      `<tr><td>${esc(t.title)}</td>${d.marketplaces.map(mp => `<td>${fmt(t[polarity][mp] || 0)}</td>`).join('')}<td><b>${fmt(t[polarity === 'positive' ? 'pos_total' : 'neg_total'])}</b></td></tr>`).join('')}</tbody></table></div>`;
  }
  function balance(tags) {
    const rows = tags.filter(t => t.pos_total + t.neg_total);
    if (!rows.length) return empty('Нет упоминаний.');
    const max = Math.max(...rows.map(t => Math.max(t.pos_total, t.neg_total)));
    return `<div class="an-balance">${legend([{label: 'Хвалят', color: SENT.positive[1]}, {label: 'Недовольны', color: SENT.negative[1]}])}${rows.map(t => `<div data-tip="${esc(`<b>${esc(t.title)}</b><div>хвалят: ${t.pos_total}, недовольны: ${t.neg_total}</div>`)}">
      <span class="neg"><b>${t.neg_total || ''}</b><i style="width:${t.neg_total / max * 100}%"></i></span><em>${esc(t.title)}</em><span class="pos"><i style="width:${t.pos_total / max * 100}%"></i><b>${t.pos_total || ''}</b></span></div>`).join('')}</div>`;
  }

  // ---------------------------------------------------------------- Тренды
  async function renderTrends() {
    const f = filtersHtml({kind: false, grain: false});
    loading(f); bindFilters(body(), renderTrends);
    let d;
    try { d = await api(`/api/analytics/trends?${query()}`); } catch (e) { fail(e, f); bindFilters(body(), renderTrends); return; }
    const table = (items, polarity) => items.length ? `<div class="an-tw"><table><thead><tr><th>Товар</th><th>Сейчас</th><th>Было</th><th>Рост</th></tr></thead><tbody>${items.map(i =>
      `<tr><td><button type="button" class="an-link" data-open-product="${i.product_id}">${esc(i.name || 'Товар')}</button><div class="muted">WB ${i.product_id}</div></td><td><b>${fmt(i.count)}</b></td><td>${fmt(i.previous)}</td>
       <td><em class="an-trend ${polarity}">▲ ${i.change == null ? 'новое' : pct(i.change)}</em></td></tr>`).join('')}</tbody></table></div>` : empty('Роста нет.');
    body().innerHTML = f + `<p class="muted an-period">Сравнение с предыдущим периодом такой же длины. Позитив — отзывы 4–5★, негатив — 1–2★.</p>
      <div class="an-row">${card('Товары с ростом позитивных отзывов', table(d.growth_positive, 'pos'))}${card('Товары с ростом негатива', table(d.growth_negative, 'neg'))}</div>`;
    bindFilters(body(), renderTrends);
    body().querySelectorAll('[data-open-product]').forEach(b => b.onclick = () => { state.product = +b.dataset.openProduct; state.productData = null; openTab('product'); });
  }

  // ---------------------------------------------------------------- По товару
  async function renderProduct() {
    const f = filtersHtml({kind: false, scope: false, grain: false});
    body().innerHTML = f + `<div class="an-picker an-noprint"><input list="anProducts" data-product-input placeholder="Начните вводить название или WB ID товара" value="">
        <datalist id="anProducts"></datalist><button class="btn" type="button" data-product-go>Показать</button></div><div data-product-body></div>`;
    bindFilters(body(), () => { state.productData = null; renderProduct(); });
    const input = body().querySelector('[data-product-input]');
    try {
      const list = await api(`/api/analytics/products?from=${state.from}&to=${state.to}`);
      state.products = list.products;
      body().querySelector('#anProducts').innerHTML = list.products.map(p => `<option value="${esc(p.product_id)}">${esc(p.name || 'Без названия')} · отзывов ${p.reviews}, вопросов ${p.questions}</option>`).join('');
    } catch (e) { state.products = []; }
    const go = () => {
      const id = (input.value.match(/\d{3,12}/) || [])[0];
      if (!id) return toast('Укажите WB ID товара из списка');
      state.product = +id; state.productData = null; loadProduct();
    };
    body().querySelector('[data-product-go]').onclick = go;
    input.onchange = go;
    input.onkeydown = e => { if (e.key === 'Enter') go(); };
    if (state.product) { input.value = state.product; loadProduct(); }
    else if (state.products.length) {
      body().querySelector('[data-product-body]').innerHTML = card('Товары с отзывами за период', `<div class="an-tw"><table><thead><tr><th>Товар</th><th>Отзывов</th><th>Вопросов</th><th>Негатив</th><th>Оценка</th></tr></thead><tbody>${state.products.slice(0, 30).map(p =>
        `<tr><td><button type="button" class="an-link" data-open-product="${p.product_id}">${esc(p.name || 'Товар')}</button><div class="muted">WB ${p.product_id}</div></td><td>${fmt(p.reviews)}</td><td>${fmt(p.questions)}</td><td>${fmt(p.negative)}</td><td>${p.avg_rating ? p.avg_rating.toFixed(1).replace('.', ',') + '★' : '—'}</td></tr>`).join('')}</tbody></table></div>`);
      body().querySelectorAll('[data-open-product]').forEach(b => b.onclick = () => { state.product = +b.dataset.openProduct; input.value = state.product; loadProduct(); });
    } else body().querySelector('[data-product-body]').innerHTML = empty('За период нет отзывов и вопросов по товарам.');
  }

  async function loadProduct() {
    const box = body().querySelector('[data-product-body]');
    box.innerHTML = '<p class="muted">Загрузка…</p>';
    try {
      const [d, tags] = await Promise.all([
        api(`/api/analytics/product/${state.product}?from=${state.from}&to=${state.to}`),
        api(`/api/analytics/tags?from=${state.from}&to=${state.to}&product_id=${state.product}`)]);
      d.tags = tags;
      state.productData = d;
      box.innerHTML = productView(d, {saved: null});
      bindProduct(box, d, null);
    } catch (e) { box.innerHTML = `<p class="an-error">Не удалось загрузить: ${esc(e.message)}</p>`; }
  }

  function productView(d, {saved}) {
    const s = d.stats;
    const wb = `https://www.wildberries.ru/catalog/${d.product_id}/detail.aspx`;
    const exp = saved ? `/api/analytics/reports/${saved}` : `/api/analytics/product/${d.product_id}?from=${d.date_from}&to=${d.date_to}`;
    const sep = exp.includes('?') ? '&' : '?';
    const months = d.charts.months;
    const rSeries = ['5', '4', '3', '2', '1'].map(r => ({label: `${r}★`, color: RATING_COLORS[r], values: Object.fromEntries(months.map(m => [m, d.charts.ratings[m][r]]))}));
    const sSeries = Object.entries(SENT).map(([key, [label, color]]) => ({label, color, values: Object.fromEntries(months.map(m => [m, d.charts.sentiment[m][key]]))}));
    const mKeys = Object.keys(d.charts.method_titles);
    const mSeries = mKeys.map(key => ({label: d.charts.method_titles[key], color: METHOD_COLORS[key], values: Object.fromEntries(months.map(m => [m, d.charts.methods[m][key]]))}));
    const usedMethods = mSeries.filter(x => Object.values(x.values).some(Boolean));
    const side = (items, cls) => items.length ? `<ul class="an-points ${cls}">${items.map(p => `<li><b>${esc(p.title)}</b> <span class="an-count">${fmt(p.count)}</span>${p.quotes.length ? `<blockquote>${p.quotes.map(q => `«${esc(q)}»`).join('<br>')}</blockquote>` : ''}</li>`).join('')}</ul>` : empty('Не найдено.');
    const tagT = (polarity, title) => d.tags ? card(title, tagTable(d.tags, polarity)) : '';
    if (!s.reviews && !s.questions) return `<h3 class="an-product-title">${esc(d.name || 'Товар')} <small>WB ${d.product_id}</small></h3>` + empty('За выбранный период по товару нет отзывов и вопросов.');
    return `<div class="an-product-head"><h3 class="an-product-title">${esc(d.name || 'Товар')} <small><a href="${wb}" target="_blank" rel="noopener">WB ${d.product_id}</a> · ${esc(d.date_from.split('-').reverse().join('.'))} — ${esc(d.date_to.split('-').reverse().join('.'))}</small></h3>
      <div class="an-actions an-noprint">${saved ? '' : '<button class="btn" type="button" data-save>Сохранить отчёт</button>'}
        <a class="btn secondary" href="${exp}${sep}format=xlsx">⇩ Excel</a><a class="btn secondary" href="${exp}${sep}format=csv">⇩ CSV</a>
        <button class="btn secondary" type="button" data-print>PDF / печать</button></div></div>
      <div class="an-kpis">${kpi('Всего отзывов', fmt(s.reviews))}${kpi('С текстом', fmt(s.with_text))}${kpi('С разметкой', fmt(s.with_tags), '<span class="muted">найдены теги</span>')}
        ${kpi('С фото', fmt(s.photo))}${kpi('Вопросов', fmt(s.questions))}${kpi('Средняя оценка', s.avg_rating ? s.avg_rating.toFixed(2).replace('.', ',') + '★' : '—')}</div>
      <div class="an-row">${card('Сильные стороны', side(d.pros, 'pos'))}${card('Слабые стороны', side(d.cons, 'neg'))}</div>
      ${card('Рекомендации', `<div class="an-advice"><b>Совет</b><ul>${d.advice.map(a => `<li>${esc(a)}</li>`).join('') || '<li>Недостаточно отзывов для советов.</li>'}</ul></div>`)}
      <div class="an-row">${tagT('positive', 'Чаще хвалят')}${tagT('negative', 'Чаще недовольны')}</div>
      ${card('ИИ-анализ', `<div data-ai-box>${d.ai ? aiView(d.ai) : (saved ? empty('В этом отчёте ИИ-анализа нет.') : `<p class="muted">ИИ прочитает отзывы с текстом (до 150 последних) и выделит плюсы, минусы и рекомендации. Нужен ключ в разделе «Настройки ИИ».</p><button class="btn secondary an-noprint" type="button" data-ai>Проанализировать с ИИ</button>`)}</div>`)}
      <h3 class="an-section">Графики по товару</h3>
      <div class="an-row">${card('Оценки по месяцам', legend(rSeries) + stacked(months, rSeries, {grain: 'month', height: 200}), 'wide')}${card('Доля оценок', donut(['5', '4', '3', '2', '1'].map(r => ({label: `${r}★`, value: d.ratings[r], color: RATING_COLORS[r]})), 'оценок'))}</div>
      <div class="an-row">${card('Тональность по месяцам', legend(sSeries) + stacked(months, sSeries, {grain: 'month', height: 200}))}
        ${card('Способы ответа по месяцам', usedMethods.length ? legend(usedMethods) + stacked(months, usedMethods, {grain: 'month', height: 200}) : empty('Отправленных ответов нет.'))}</div>`;
  }
  const aiView = ai => `${ai.summary ? `<p>${esc(ai.summary)}</p>` : ''}<div class="an-row an-ai">
      <div><h4>Плюсы</h4><ul>${(ai.pros || []).map(x => `<li>${esc(x)}</li>`).join('') || '<li class="muted">—</li>'}</ul></div>
      <div><h4>Минусы</h4><ul>${(ai.cons || []).map(x => `<li>${esc(x)}</li>`).join('') || '<li class="muted">—</li>'}</ul></div>
      <div><h4>Что сделать</h4><ul>${(ai.advice || []).map(x => `<li>${esc(x)}</li>`).join('') || '<li class="muted">—</li>'}</ul></div></div>
      ${ai.reviews_used ? `<p class="muted">Проанализировано отзывов: ${ai.reviews_used}.</p>` : ''}`;

  function bindProduct(box, d, saved) {
    bindPrint();
    const ai = box.querySelector('[data-ai]');
    if (ai) ai.onclick = async () => {
      ai.disabled = true; ai.textContent = 'ИИ читает отзывы…';
      try {
        d.ai = await api(`/api/analytics/product/${d.product_id}/ai?from=${d.date_from}&to=${d.date_to}`, {method: 'POST'});
        box.querySelector('[data-ai-box]').innerHTML = aiView(d.ai) + '<p class="muted">ИИ-анализ попадёт в Excel/CSV после сохранения отчёта.</p>';
      } catch (e) { ai.disabled = false; ai.textContent = 'Проанализировать с ИИ'; toast(e.message); }
    };
    const save = box.querySelector('[data-save]');
    if (save) save.onclick = async () => {
      save.disabled = true;
      try {
        const {tags, ...data} = d;
        const r = await api('/api/analytics/reports', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
        save.textContent = 'Сохранено'; toast(`Отчёт сохранён: ${r.title}`);
      } catch (e) { save.disabled = false; toast(e.message); }
    };
  }

  // ---------------------------------------------------------------- Отчёты
  async function renderReports() {
    body().innerHTML = `<div class="an-filters an-noprint"><input type="search" data-report-search placeholder="Поиск по названию или WB ID" value="${esc(state.reportSearch)}"></div><div data-reports></div>`;
    const search = body().querySelector('[data-report-search]');
    let timer;
    search.oninput = () => { clearTimeout(timer); timer = setTimeout(() => { state.reportSearch = search.value; loadReports(); }, 250); };
    loadReports();
  }
  async function loadReports() {
    const box = body().querySelector('[data-reports]');
    box.innerHTML = '<p class="muted">Загрузка…</p>';
    let list;
    try { list = (await api(`/api/analytics/reports?search=${encodeURIComponent(state.reportSearch)}`)).reports; } catch (e) { box.innerHTML = `<p class="an-error">${esc(e.message)}</p>`; return; }
    if (!list.length) { box.innerHTML = empty(state.reportSearch ? 'Ничего не найдено.' : 'Сохранённых отчётов пока нет. Откройте вкладку «По товару» и нажмите «Сохранить отчёт».'); return; }
    const date = s => s ? s.slice(0, 10).split('-').reverse().join('.') : '';
    box.innerHTML = card('Сохранённые отчёты', `<div class="an-tw"><table><thead><tr><th>Отчёт</th><th>Период</th><th>Создан</th><th></th></tr></thead><tbody>${list.map(r =>
      `<tr><td><button type="button" class="an-link" data-view="${r.id}">${esc(r.title)}</button></td><td>${date(r.date_from)} — ${date(r.date_to)}</td><td>${new Date(r.created_at).toLocaleString('ru-RU', {dateStyle: 'short', timeStyle: 'short'})}</td>
       <td class="an-row-actions"><button class="btn secondary" type="button" data-view="${r.id}">Открыть</button><a class="btn secondary" href="/api/analytics/reports/${r.id}?format=xlsx">Excel</a><a class="btn secondary" href="/api/analytics/reports/${r.id}?format=csv">CSV</a><button class="btn secondary" type="button" data-del="${r.id}">Удалить</button></td></tr>`).join('')}</tbody></table></div>`);
    box.querySelectorAll('[data-view]').forEach(b => b.onclick = async () => {
      try {
        const d = await api(`/api/analytics/reports/${b.dataset.view}`);
        box.innerHTML = `<button type="button" class="btn secondary an-noprint" data-back>← К списку отчётов</button>` + productView(d, {saved: b.dataset.view});
        box.querySelector('[data-back]').onclick = loadReports;
        bindProduct(box, d, b.dataset.view);
      } catch (e) { toast(e.message); }
    });
    box.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
      if (b.dataset.armed !== '1') { b.dataset.armed = '1'; b.textContent = 'Точно удалить?'; return; }
      try { await api(`/api/analytics/reports/${b.dataset.del}`, {method: 'DELETE'}); loadReports(); } catch (e) { toast(e.message); }
    });
  }

  // ---------------------------------------------------------------- подсказки и стили
  const tip = document.createElement('div');
  tip.className = 'an-tip'; tip.setAttribute('role', 'tooltip');
  document.addEventListener('mousemove', e => {
    const t = e.target.closest && e.target.closest('#analytics [data-tip]');
    if (!t) { tip.style.display = 'none'; return; }
    if (tip.dataset.src !== t.dataset.tip) { tip.innerHTML = t.dataset.tip; tip.dataset.src = t.dataset.tip; }
    if (!tip.isConnected) document.body.append(tip);
    tip.style.display = 'block';
    const w = tip.offsetWidth, h = tip.offsetHeight;
    let x = e.clientX + 14, y = e.clientY + 14;
    if (x + w > innerWidth - 8) x = e.clientX - w - 14;
    if (y + h > innerHeight - 8) y = e.clientY - h - 14;
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  });

  const style = document.createElement('style');
  style.textContent = `
#analytics{--an-ink:#1f2937;--an-muted:#6b7280;--an-line:#eceef3}
#analytics .an-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:12px;margin-bottom:6px}#analytics .an-head h2{margin:0}
.an-tabs{display:flex;flex-wrap:wrap;gap:4px;background:#f3f4f8;padding:4px;border-radius:12px}.an-tabs button,.an-seg button{border:0;background:transparent;padding:8px 13px;border-radius:9px;font:inherit;font-size:14px;font-weight:600;color:#4b5563;cursor:pointer}
.an-tabs button.on,.an-seg button.on{background:#fff;color:#5b21b6;box-shadow:0 1px 3px #0000001a}
.an-filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:14px 0 6px}.an-seg{display:flex;gap:2px;background:#f3f4f8;padding:3px;border-radius:11px}
.an-filters select,.an-filters input{height:38px;padding:0 10px;border:1px solid #ddd6fe;border-radius:10px;font:inherit;font-size:14px;background:#fff;color:var(--an-ink);min-width:0}
.an-filters input[type=search]{min-width:280px}.an-date{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--an-muted)}
.an-actions{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}.an-actions .btn{height:36px;padding:7px 12px;font-size:13px;text-decoration:none;display:inline-flex;align-items:center}
.an-period{margin:6px 0 4px;font-size:13px}
.an-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:12px 0}.an-kpi{border:1px solid #e5e7eb;border-radius:12px;padding:14px;background:#fff;min-width:0}
.an-kpi small{display:block;color:var(--an-muted);font-size:13px}.an-kpi strong{display:block;font-size:26px;color:#111827;margin:4px 0;font-variant-numeric:tabular-nums}.an-kpi span{font-size:12px}
.an-kpi em{font-style:normal;font-weight:700;color:#374151}
.an-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin:14px 0}.an-row:has(.wide){grid-template-columns:minmax(0,2fr) minmax(260px,1fr)}
.an-card{border:1px solid #e5e7eb;border-radius:14px;padding:16px;background:#fff;min-width:0}.an-card h3{font-size:15px;margin:0 0 10px}.an-sub{font-weight:500;color:var(--an-muted);font-size:13px;margin-left:6px}
#analytics>.an-card,[data-an-body]>.an-card,[data-product-body]>.an-card,[data-reports]>.an-card{margin:14px 0}
.an-svg{width:100%;height:auto;display:block;overflow:visible}.an-grid{stroke:var(--an-line);stroke-width:1}.an-axis{font-size:11px;fill:#8b8f99;font-variant-numeric:tabular-nums}
.an-legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin:0 0 8px;font-size:13px;color:#374151}.an-legend span{display:inline-flex;align-items:center;gap:6px}.an-legend i,.an-donut li i,.an-tip i,.an-dot{display:inline-block;width:10px;height:10px;border-radius:3px;flex:none}.an-dot{margin-right:7px}
.an-donut{display:flex;align-items:center;gap:16px;flex-wrap:wrap}.an-donut svg{width:140px;height:140px;flex:none}.an-donut-v{font-size:20px;font-weight:800;fill:#111827}
.an-donut ul{list-style:none;padding:0;margin:0;display:grid;gap:6px;flex:1;min-width:150px;font-size:13px}.an-donut li{display:grid;grid-template-columns:10px 1fr auto 44px;gap:8px;align-items:center}.an-donut li small{color:var(--an-muted);text-align:right}
.an-hbars>div{display:grid;grid-template-columns:minmax(90px,150px) 1fr 44px;gap:8px;align-items:center;margin:7px 0;font-size:13px}.an-hbars>div>div{background:#f3f4f8;border-radius:4px;height:12px;overflow:hidden}.an-hbars i{display:block;height:100%;border-radius:0 4px 4px 0}.an-hbars b{text-align:right;font-variant-numeric:tabular-nums}
.an-tw{overflow-x:auto;border:1px solid #eef0f5;border-radius:12px}.an-tw td:not(:first-child),.an-tw th:not(:first-child){white-space:nowrap}.an-tw th,.an-tw td{padding:10px;font-size:13px}.an-tw td{font-variant-numeric:tabular-nums}.an-tw tbody tr:hover{background:#faf8ff}
.an-mp{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:700;color:var(--c);border:1px solid color-mix(in srgb,var(--c) 40%,#fff);background:color-mix(in srgb,var(--c) 8%,#fff)}
.an-meter{display:inline-block;width:60px;height:6px;background:#f3f4f8;border-radius:3px;margin-right:8px;vertical-align:middle;overflow:hidden}.an-meter i{display:block;height:100%;background:#2a78d6}
.an-balance>div{display:grid;grid-template-columns:1fr minmax(120px,170px) 1fr;gap:10px;align-items:center;margin:6px 0;font-size:13px}.an-balance em{font-style:normal;text-align:center;font-weight:600}
.an-balance span{display:flex;align-items:center;gap:6px;height:14px}.an-balance .neg{justify-content:flex-end}.an-balance i{display:block;height:12px}.an-balance .neg i{background:#e34948;border-radius:4px 0 0 4px}.an-balance .pos i{background:#2a78d6;border-radius:0 4px 4px 0}
.an-link{border:0;background:none;padding:0;color:#5b21b6;font:inherit;font-weight:600;text-align:left;cursor:pointer}.an-link:hover{text-decoration:underline}
.an-trend{font-style:normal;font-weight:700}.an-trend.pos{color:#2a78d6}.an-trend.neg{color:#e34948}
.an-picker{display:flex;gap:8px;margin:10px 0}.an-picker input{flex:1;min-width:0;height:40px;padding:0 12px;border:1px solid #ddd6fe;border-radius:10px;font:inherit}
.an-product-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:10px;margin-top:12px}.an-product-title{margin:0;font-size:18px}.an-product-title small{display:block;font-size:13px;font-weight:400;color:var(--an-muted);margin-top:3px}
.an-points{list-style:none;padding:0;margin:0;display:grid;gap:10px}.an-points li{padding:10px 12px;border-radius:10px;border-left:3px solid}.an-points.pos li{border-color:#2a78d6;background:#f5f9fe}.an-points.neg li{border-color:#e34948;background:#fef6f5}
.an-count{font-size:12px;color:var(--an-muted);margin-left:6px}.an-points blockquote{margin:6px 0 0;font-size:13px;color:#4b5563;line-height:1.45}
.an-advice{background:#faf8ff;border:1px solid #ede9fe;border-radius:10px;padding:12px 14px}.an-advice ul{margin:6px 0 0;padding-left:20px;line-height:1.5}
.an-ai h4{margin:0 0 6px;font-size:14px}.an-ai ul{margin:0;padding-left:18px;line-height:1.5;font-size:14px}.an-section{font-size:16px;margin:22px 0 0}
.an-row-actions{white-space:nowrap}.an-row-actions .btn{height:32px;padding:5px 10px;font-size:12px;margin:2px;text-decoration:none;display:inline-flex;align-items:center}
.an-empty{padding:18px;border:1px dashed #e5e7eb;border-radius:12px;text-align:center}.an-error{color:#b91c1c}
.an-tip{position:fixed;z-index:1000;display:none;pointer-events:none;background:#111827;color:#fff;font-size:12px;line-height:1.5;padding:8px 10px;border-radius:8px;box-shadow:0 6px 20px #0003;max-width:260px}
.an-tip i{margin-right:6px;vertical-align:-1px}.an-tip-total{border-top:1px solid #ffffff33;margin-top:4px;padding-top:4px}
.an-card>p.muted{font-size:13px}
@media(max-width:760px){.an-tw table{min-width:480px}.an-filters select,.an-filters .an-date{flex:1 1 140px}.an-date input{flex:1}.an-row,.an-row:has(.wide){grid-template-columns:1fr}.an-filters input[type=search]{min-width:0;width:100%}.an-balance>div{grid-template-columns:1fr 100px 1fr}}
@media print{body{background:#fff}body *{visibility:hidden}#analytics,#analytics *{visibility:visible}#analytics{position:absolute;left:0;top:0;width:100%;border:0;box-shadow:none;padding:0}
  .an-noprint,.an-tip{display:none!important}.an-card,.an-kpi{break-inside:avoid}.an-row,.an-row:has(.wide){grid-template-columns:1fr 1fr}}`;
  document.head.appendChild(style);

  return {render};
})();
