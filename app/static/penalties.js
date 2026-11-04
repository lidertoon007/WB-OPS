/* WB OPS: «Аналитика штрафов» — штрафы WB из отчётов реализации: динамика, причины, артикулы, связь с FBS-заданиями и прочие удержания. */
window.WBOPSPenalties = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const num = (v, d = 0) => v == null || v === '' || isNaN(v) ? '—' : Number(v).toLocaleString('ru-RU', {minimumFractionDigits: d, maximumFractionDigits: d});
  const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const parse = s => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const ru = s => s ? s.slice(0, 10).split('-').reverse().join('.') : '';
  const dt = s => s ? new Date(s).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}) : '—';
  const st = {tab: 'summary', f: {preset: 'days:90', from: '', to: '', cabinet: '', warehouse: '', brand: '', category: '', group: '', reason: '', q: ''},
    data: null, fs: {sort: 'day', desc: true, offset: 0}, as: {sort: 'sum', desc: true, q: ''}, poll: null};
  let root = null;
  try { Object.assign(st.f, JSON.parse(localStorage.getItem('wbops.pen.f') || '{}'), {reason: ''}); } catch (e) { /* без хранилища */ }
  const remember = () => { try { localStorage.setItem('wbops.pen.f', JSON.stringify(st.f)); } catch (e) { /* ignore */ } };
  const api = async (url, opt) => { const r = await fetch(url, opt); const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.detail || 'Ошибка запроса'); return d; };
  const G = () => Object.fromEntries((st.data?.groups || []).map(g => [g.key, g]));
  const gColor = k => (G()[k] || {}).color || '#868e96', gTitle = k => (G()[k] || {}).title || k;
  const WBS = {sold: 'Выкуплен', canceled_by_client: 'Отказ при получении', declined_by_client: 'Отменён покупателем', canceled: 'Отменён', defect: 'Брак / подмена', ready_for_pickup: 'В ПВЗ', sorted: 'В пути', waiting: 'В сборке'};
  const rub = v => num(v, v != null && Math.abs(v) > 0 && Math.abs(v) < 10 ? 2 : 0);
  const short = s => s.replace(/^Штраф МП\.\s*/, '').replace(/^Штраф за /, 'За ');

  function presets() {
    const t = new Date(), m0 = new Date(t.getFullYear(), t.getMonth(), 1);
    return [['days:30', 'Последние 30 дней', addDays(t, -29), t], ['days:90', 'Последние 90 дней', addDays(t, -89), t], ['days:180', 'Последние 180 дней', addDays(t, -179), t],
      ['days:365', 'Последние 12 месяцев', addDays(t, -364), t], ['month:0', 'Текущий месяц', m0, t],
      ['month:1', 'Прошлый месяц', new Date(t.getFullYear(), t.getMonth() - 1, 1), new Date(t.getFullYear(), t.getMonth(), 0)], ['custom', 'Свой период…', null, null]];
  }
  function period() {
    if (st.f.preset === 'custom' && st.f.from && st.f.to) return [st.f.from, st.f.to];
    const p = presets().find(x => x[0] === st.f.preset) || presets()[1];
    return [iso(p[2]), iso(p[3])];
  }
  function query(extra = {}) {
    const [from, to] = period(), q = new URLSearchParams({from, to});
    ['cabinet', 'warehouse', 'brand', 'category', 'group', 'reason', 'q'].forEach(k => st.f[k] && q.set(k, st.f[k]));
    Object.entries(extra).forEach(([k, v]) => v !== '' && v != null && q.set(k, v));
    return q;
  }

  // ---------------------------------------------------------------- каркас
  async function render(section) {
    root = section;
    root.className = 'panel rv-page fb-page pn-page';
    root.innerHTML = `<div class="db-head"><h2>Аналитика штрафов</h2><span class="muted db-fresh" data-fresh></span></div>
      <div class="db-filters" data-filters></div><div data-notes></div>
      <div class="rnp-tabs fb-tabs" role="tablist">${[['summary', 'Сводка'], ['feed', 'Лента штрафов'], ['reasons', 'Причины'], ['articles', 'Артикулы'], ['deductions', 'Прочие удержания']]
        .map(([k, t]) => `<button type="button" role="tab" data-tab="${k}">${t}</button>`).join('')}</div>
      <div data-body><p class="muted">Загрузка…</p></div>`;
    root.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => { st.tab = b.dataset.tab; drawBody(); });
    await load();
  }
  async function load() {
    const [from, to] = period(), days = (parse(to) - parse(from)) / 864e5 + 1;
    const q = query({prev_from: iso(addDays(parse(from), -days)), prev_to: iso(addDays(parse(from), -1))});
    try { st.data = await api('/api/penalties?' + q); }
    catch (e) { root.querySelector('[data-body]').innerHTML = `<p class="rv-error">${esc(e.message)}</p>`; return; }
    drawFilters(); drawNotes(); drawBody();
    clearTimeout(st.poll);
    if (st.data.state?.status === 'running' || st.data.download === 'running') st.poll = setTimeout(waitSync, 5000);
  }
  async function waitSync() {
    if (!root?.isConnected) return;
    const s = await api('/api/penalties/status').catch(() => ({}));
    if (s.status === 'running' || s.download === 'running') { st.poll = setTimeout(waitSync, 5000); return; }
    load();
  }
  function drawFilters() {
    const o = st.data.options, f = st.f, [from, to] = period();
    const opt = (v, t, cur) => `<option value="${esc(v)}" ${String(v) === String(cur) ? 'selected' : ''}>${esc(t)}</option>`;
    const busy = st.data.state?.status === 'running' || st.data.download === 'running';
    root.querySelector('[data-filters]').innerHTML = `
      <select data-f="preset" aria-label="Период" class="db-period">${presets().map(([k, t]) => opt(k, t, f.preset)).join('')}</select>
      ${f.preset === 'custom' ? `<label class="db-date">с <input type="date" data-f="from" value="${from}"></label><label class="db-date">по <input type="date" data-f="to" value="${to}"></label>` : ''}
      ${o.cabinets.length > 1 ? `<select data-f="cabinet" aria-label="Кабинет">${opt('', 'Все кабинеты', f.cabinet)}${o.cabinets.map(c => opt(c, c, f.cabinet)).join('')}</select>` : ''}
      ${o.warehouses?.length ? `<select data-f="warehouse" aria-label="Склад">${opt('', 'Все склады', f.warehouse)}${o.warehouses.map(w => opt(w, w, f.warehouse)).join('')}</select>` : ''}
      <select data-f="group" aria-label="Тип штрафа">${opt('', 'Все типы штрафов', f.group)}${st.data.groups.map(g => opt(g.key, g.title, f.group)).join('')}</select>
      <select data-f="brand" aria-label="Бренд">${opt('', 'Все бренды', f.brand)}${o.brands.map(b => opt(b, b, f.brand)).join('')}</select>
      <select data-f="category" aria-label="Предмет">${opt('', 'Все предметы', f.category)}${o.categories.map(b => opt(b, b, f.category)).join('')}</select>
      <input type="search" data-f="q" placeholder="Артикул, WB ID, srid, причина" value="${esc(f.q)}" aria-label="Поиск">
      <span class="rp-tools"><button type="button" class="db-ghost" data-sync ${busy ? 'disabled' : ''}>↻ ${busy ? 'Загружаю…' : 'Обновить из WB'}</button></span>
      ${f.reason ? `<div class="pn-reason-chip">Причина: <b>${esc(short(f.reason))}</b> <button type="button" data-clear-reason aria-label="Сбросить">×</button></div>` : ''}`;
    let t;
    root.querySelectorAll('[data-f]').forEach(el => {
      const h = () => {
        const k = el.dataset.f;
        if (k === 'preset' && el.value === 'custom') { st.f.from = from; st.f.to = to; }
        st.f[k] = el.value; st.fs.offset = 0; remember();
        clearTimeout(t); t = setTimeout(load, k === 'q' ? 400 : 0);
      };
      el.type === 'search' ? (el.oninput = h) : (el.onchange = h);
    });
    const cr = root.querySelector('[data-clear-reason]'); if (cr) cr.onclick = () => { st.f.reason = ''; load(); };
    root.querySelector('[data-sync]').onclick = async e => {
      e.currentTarget.disabled = true;
      try { await api('/api/penalties/sync', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({download: true})});
        st.data.download = 'running'; drawNotes(); st.poll = setTimeout(waitSync, 5000); }
      catch (err) { alert(err.message); e.currentTarget.disabled = false; }
    };
    const d = st.data;
    root.querySelector('[data-fresh]').textContent = d.last_day || d.last_sale ? `Отчёты WB по ${ru([d.last_day, d.last_sale].filter(Boolean).sort().pop())} · из отчётов реализации` : 'Данные ещё не загружены';
  }
  function drawNotes() {
    const d = st.data, s = d.state || {}, notes = [];
    if (d.download === 'running') notes.push('Скачиваю свежие отчёты реализации WB (через расчёт ОПиУ) — это несколько минут. Страница обновится сама.');
    if (s.status === 'running') notes.push(esc(s.message || 'Читаю отчёты WB…'));
    if (s.status === 'error') notes.push(esc(s.message));
    if (!d.synced_at && s.status !== 'running' && d.download !== 'running' && s.status !== 'error') notes.push('Отчётов реализации ещё нет. Нажмите «Обновить из WB».');
    root.querySelector('[data-notes]').innerHTML = notes.map(n => `<div class="db-note">${n}</div>`).join('');
  }
  function drawBody() {
    root.querySelectorAll('[data-tab]').forEach(b => { b.classList.toggle('active', b.dataset.tab === st.tab); b.setAttribute('aria-selected', String(b.dataset.tab === st.tab)); });
    ({summary: drawSummary, feed: drawFeed, reasons: drawReasons, articles: drawArticles, deductions: drawDeductions}[st.tab])();
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
    const len = String(value).replace(/<[^>]+>/g, '').length;
    return `<article class="db-tile ${cls}"><div class="db-title">${esc(title)}${opts.hint ? ` <span class="db-i" tabindex="0" title="${esc(opts.hint)}" aria-label="${esc(opts.hint)}">i</span>` : ''}</div>
      <div class="db-value ${len > 17 ? 'long' : len > 13 ? 'mid' : ''}">${value}</div><div class="db-change">${delta}</div></article>`;
  }
  function drawSummary() {
    const d = st.data, s = d.summary, p = d.previous || {}, has = !!d.previous, body = root.querySelector('[data-body]');
    const pv = k => has ? p[k] : null;
    if (!d.synced_at) { body.innerHTML = '<div class="rv-empty"><b>Нет данных</b><p class="muted">Нажмите «Обновить из WB», чтобы скачать отчёты реализации.</p></div>'; return; }
    const top = d.reasons[0];
    body.innerHTML = `<div class="fb-lost-hero ${s.total ? '' : 'ok'} pn-hero">
        <div><span>Штрафы WB за период</span><b>${num(s.total)} ₽</b>
          <p>${s.total ? `${num(s.count)} штрафов по ${num(s.articles)} артикулам · ${s.pct_revenue == null ? '' : `${num(s.pct_revenue, 2)}% от продаж · `}чаще всего — «${esc(gTitle(s.top_group))}»${top ? `, самая дорогая причина: «${esc(short(top.reason))}» (${num(top.share, 0)}%)` : ''}.` : 'За период штрафов нет.'}</p></div>
        ${s.total ? `<div class="fb-lost-side"><span>≈ в месяц при таком темпе</span><b>${num(s.total / d.days * 30)} ₽</b><span>${s.per_1000 == null ? '' : `${num(s.per_1000, 1)} штрафа на 1000 продаж`}</span></div>` : ''}
      </div>
      <div class="db-grid fb-grid">
        ${tile('Сумма штрафов', `${num(s.total)}<sup>₽</sup>`, s.total, pv('total'), {good: 'down', unit: '₽', hint: 'Сумма по полю «Штрафы» детализации отчётов реализации WB за даты операций в периоде'})}
        ${tile('Количество штрафов', `${num(s.count)}<sup>шт</sup>`, s.count, pv('count'), {good: 'down'})}
        ${tile('Доля от продаж', `${num(s.pct_revenue, 2)}<sup>%</sup>`, s.pct_revenue, pv('pct_revenue'), {good: 'down', pp: true, d: 2, hint: 'Штрафы к сумме продаж за вычетом возвратов (по цене продажи из отчёта реализации)'})}
        ${tile('Средний штраф', `${num(s.avg)}<sup>₽</sup>`, s.avg, pv('avg'), {good: 'down', unit: '₽'})}
        ${tile('Прочие удержания', `${num(s.deductions)}<sup>₽</sup>`, s.deductions, pv('deductions'), {unit: '₽', hint: 'Не штрафы: реклама «WB Продвижение», подписки, баллы за отзывы, транзит, утилизация. Подробно — во вкладке «Прочие удержания»'})}
      </div>
      ${reconcile()}
      <section class="tr-box"><h3>Штрафы по ${d.weekly ? 'неделям' : 'дням'}</h3><p class="muted fb-sub">Дата операции в отчёте реализации · ${legend()}</p>${timeline()}</section>
      <div class="fb-two">
        <section class="tr-box"><h3>По типам</h3><p class="muted fb-sub">Сумма, количество и изменение к прошлому периоду</p>${groupsBlock()}</section>
        <section class="tr-box"><h3>Главные причины</h3><p class="muted fb-sub">Нажмите на причину — откроется лента этих штрафов</p>${topReasons()}</section>
      </div>
      <section class="tr-box"><h3>Артикулы с самыми большими штрафами</h3>${topArticles()}</section>
      <p class="muted rp-note">Штрафы WB приходят в отчётах реализации — еженедельно, по понедельникам, а за дни текущей недели — в ежедневных отчётах. Поэтому за последние дни штрафов может ещё не быть.
        Задания FBS связываются со штрафом по srid — так видно склад и дату заказа.</p>`;
    body.querySelectorAll('[data-goreason]').forEach(b => b.onclick = () => { st.f.reason = b.dataset.goreason; st.tab = 'feed'; st.fs.offset = 0; load(); });
    body.querySelectorAll('[data-gogroup]').forEach(b => b.onclick = () => { st.f.group = b.dataset.gogroup; remember(); st.tab = 'feed'; st.fs.offset = 0; load(); });
    body.querySelectorAll('[data-goart]').forEach(b => b.onclick = () => { st.f.q = b.dataset.goart; remember(); st.tab = 'feed'; st.fs.offset = 0; load(); });
  }
  function legend() {
    const used = new Set(st.data.timeline.flatMap(t => Object.keys(t.groups)));
    return st.data.groups.filter(g => used.has(g.key)).map(g => `<i class="fb-dot" style="background:${g.color};margin-left:8px"></i>${esc(g.title)}`).join(' ');
  }
  function reconcile() {
    const r = st.data.reconcile;
    if (!r || !r.reports) return '';
    const ok = Math.abs(r.wb - r.ours) < 1;
    return `<div class="pn-check ${ok ? 'ok' : 'warn'}">${ok ? '✓' : '!'} Сверка с итогами ${num(r.reports)} отчётов WB, целиком попавших в период: в отчётах ${num(r.wb, 2)} ₽, в детализации ${num(r.ours, 2)} ₽${ok ? ' — сходится.' : ' — есть расхождение, обновите данные.'}</div>`;
  }
  function timeline() {
    const t = st.data.timeline, max = Math.max(1, ...t.map(x => x.total));
    if (!t.some(x => x.total)) return '<p class="muted">Штрафов нет.</p>';
    const W = Math.max(1000, t.length * 24), H = 230, P = 34, bw = (W - P * 2) / t.length, base = H - 24;
    const bars = t.map((x, i) => {
      let y = base;
      const segs = st.data.groups.map(g => { const v = x.groups[g.key] || 0; if (!v) return ''; const h = v / max * (H - 50); y -= h;
        return `<rect x="${P + i * bw + 2}" y="${y + 1}" width="${Math.max(2, bw - 4)}" height="${Math.max(1, h - 1)}" rx="2" fill="${g.color}"/>`; }).join('');
      const label = t.length <= 18 || i % Math.ceil(t.length / 18) === 0 ? `<text x="${P + i * bw + bw / 2}" y="${H - 8}" class="fb-ax" text-anchor="middle">${ru(x.from).slice(0, 5)}</text>` : '';
      const tip = `${st.data.weekly ? `Неделя ${ru(x.from)}–${ru(x.to)}` : ru(x.from)}: ${num(x.total)} ₽, ${x.count} шт${x.pct_revenue != null ? `, ${num(x.pct_revenue, 2)}% от продаж` : ''}\n` +
        st.data.groups.filter(g => x.groups[g.key]).map(g => `${g.title}: ${num(x.groups[g.key])} ₽`).join('\n');
      return segs + label + `<rect x="${P + i * bw}" y="${P}" width="${bw}" height="${base - P}" fill="transparent"><title>${esc(tip)}</title></rect>`;
    }).join('');
    return `<div class="fb-chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Штрафы по периодам"><text x="${P}" y="16" class="fb-ax">${num(max)} ₽</text>
      <line x1="${P}" x2="${W - P}" y1="${base}" y2="${base}" stroke="#e5e7eb"/>${bars}</svg></div>`;
  }
  function delta(cur, prev) {
    if (prev == null || (!cur && !prev)) return '<span class="muted">—</span>';
    const d = cur - prev;
    if (!prev) return `<span class="neg">новое</span>`;
    return `<span class="${d > 0 ? 'neg' : d < 0 ? 'pos' : 'muted'}">${d > 0 ? '+' : ''}${num(d / prev * 100, 0)}%</span>`;
  }
  function groupsBlock() {
    const s = st.data.summary, p = st.data.previous, total = s.total || 1;
    const rows = st.data.groups.filter(g => s.groups[g.key] || (p && p.groups[g.key]));
    if (!rows.length) return '<p class="muted">Штрафов нет.</p>';
    return `<div class="fb-bands">${rows.map(g => s.groups[g.key] ? `<span style="width:${s.groups[g.key].sum / total * 100}%;background:${g.color}" title="${esc(g.title)}: ${num(s.groups[g.key].sum)} ₽"></span>` : '').join('')}</div>
      <table class="fb-mini"><tr><th>Тип</th><th class="r">Штрафов</th><th class="r">Сумма</th><th class="r">Доля</th><th class="r">К прошлому</th></tr>
      ${rows.map(g => { const v = s.groups[g.key] || {sum: 0, count: 0}; return `<tr class="pn-click" data-gogroup="${g.key}"><td><i style="background:${g.color}"></i>${esc(g.title)}</td>
        <td class="r">${num(v.count)}</td><td class="r"><b>${num(v.sum)} ₽</b></td><td class="r muted">${num(v.sum / total * 100, 0)}%</td><td class="r">${delta(v.sum, p ? (p.groups[g.key] || {}).sum || 0 : null)}</td></tr>`; }).join('')}</table>`;
  }
  function topReasons() {
    const r = st.data.reasons.slice(0, 7), max = Math.max(1, ...r.map(x => x.sum));
    if (!r.length) return '<p class="muted">Штрафов нет.</p>';
    return `<table class="fb-mini pn-top">${r.map(x => `<tr class="pn-click" data-goreason="${esc(x.reason)}"><td><i style="background:${gColor(x.group)}"></i>${esc(short(x.reason))}<small class="muted"> · ${num(x.count)} шт</small></td>
      <td><span class="fb-hbar"><i style="width:${x.sum / max * 100}%;background:${gColor(x.group)}"></i></span></td><td class="r"><b>${rub(x.sum)} ₽</b></td></tr>`).join('')}</table>`;
  }
  function topArticles() {
    const a = st.data.articles.slice(0, 8);
    if (!a.length) return '<p class="muted">Штрафов нет.</p>';
    const max = Math.max(1, ...a.map(x => x.sum));
    return `<div class="rnp-scroll"><table class="rnp-table fb-groups"><thead><tr><th class="l">Артикул</th><th>Штрафов</th><th>Сумма, ₽</th><th>Продажи, ₽</th><th>% от продаж</th><th class="l">Главная причина</th><th class="l">Типы</th></tr></thead><tbody>
      ${a.map(x => `<tr class="pn-click" data-goart="${esc(x.article || x.nm_id)}"><td class="l"><b>${esc(x.article || '—')}</b><small>${x.nm_id}${x.subject ? ' · ' + esc(x.subject) : ''}</small></td>
        <td>${num(x.count)}</td><td><b>${num(x.sum)}</b></td><td>${num(x.revenue)}</td><td class="${x.pct_revenue >= 3 ? 'fb-hot' : x.pct_revenue >= 1 ? 'fb-mid' : ''}">${x.pct_revenue == null ? '—' : num(x.pct_revenue, 2) + '%'}</td>
        <td class="l">${esc(short(x.top_reason))}</td><td class="l">${miniBar(x.groups, x.sum, max)}</td></tr>`).join('')}</tbody></table></div>`;
  }
  function miniBar(groups, sum, max) {
    return `<div class="fb-bands sm" style="width:${Math.max(8, sum / max * 150)}px">${st.data.groups.map(g => groups[g.key] ? `<span style="width:${groups[g.key] / sum * 100}%;background:${g.color}" title="${esc(g.title)}: ${num(groups[g.key])} ₽"></span>` : '').join('')}</div>`;
  }

  // ---------------------------------------------------------------- лента
  async function drawFeed() {
    const body = root.querySelector('[data-body]'), fs = st.fs;
    body.innerHTML = '<p class="muted">Загрузка…</p>';
    let d;
    try { d = await api('/api/penalties/items?' + query({sort: fs.sort, desc: fs.desc, offset: fs.offset, limit: 100})); }
    catch (e) { body.innerHTML = `<p class="rv-error">${esc(e.message)}</p>`; return; }
    if (st.tab !== 'feed') return;
    const s = st.data.summary;
    const th = (k, t, cls = '') => `<th class="${cls}"><button type="button" class="rp-sort ${fs.sort === k ? 'on' : ''}" data-fsort="${k}">${t}<span>${fs.sort === k ? (fs.desc ? '↓' : '↑') : '↕'}</span></button></th>`;
    body.innerHTML = `<div class="sk-chips">
        <button type="button" class="sk-chip ${!st.f.group ? 'on' : ''}" data-fg="">Все типы</button>
        ${st.data.groups.filter(g => s.groups[g.key] || st.f.group === g.key).map(g => `<button type="button" class="sk-chip ${st.f.group === g.key ? 'on' : ''}" data-fg="${g.key}"><i class="fb-dot" style="background:${g.color}"></i>${esc(g.title)} <b>${num((s.groups[g.key] || {}).count || 0)}</b></button>`).join('')}
        <span class="muted pn-feed-sum">${num(d.total)} шт · ${num(d.sum)} ₽</span>
        <a class="db-ghost fb-csv" href="/api/penalties/items.csv?${query()}" download>⇩ CSV</a></div>
      <div class="rnp-scroll"><table class="rnp-table fb-feed"><thead><tr>${th('day', 'Дата', 'l')}${th('reason', 'Причина', 'l')}${th('article', 'Артикул', 'l')}${th('amount', 'Сумма')}
        <th class="l">Задание FBS</th><th class="l">Склад</th><th class="l">Заказ</th><th class="l">srid</th></tr></thead><tbody>
      ${d.items.map(r => { const fb = r.fbs; return `<tr><td class="l">${ru(r.day)}</td>
        <td class="l pn-reason"><span class="fb-chip" style="--c:${gColor(r.group)}">${esc(r.group_title)}</span><small>${esc(r.reason)}</small></td>
        <td class="l"><b>${esc(r.article || '—')}</b><small>${r.nm_id || ''}${r.subject ? ' · ' + esc(r.subject) : ''}${st.data.options.cabinets.length > 1 ? ' · ' + esc(r.account) : ''}</small></td>
        <td class="neg"><b>${num(r.amount, 2)} ₽</b></td>
        <td class="l">${fb ? `${fb.order_id}<small>${esc(WBS[fb.wb_status] || fb.wb_status || '')}</small>` : '<span class="muted">—</span>'}</td>
        <td class="l">${fb ? esc(fb.warehouse) : '<span class="muted">—</span>'}</td>
        <td class="l">${fb ? `${dt(fb.created_at)}<small>${fb.hours_to_sc != null ? `до СЦ ${num(fb.hours_to_sc, 1)} ч` : 'не принят WB'}${fb.price ? ` · ${num(fb.price)} ₽` : ''}</small>` : '<span class="muted">—</span>'}</td>
        <td class="l"><small class="pn-srid">${esc(r.srid || '')}</small></td></tr>`; }).join('') || '<tr><td colspan="8" class="muted l">Штрафов нет.</td></tr>'}</tbody></table></div>
      <div class="fb-pager"><button type="button" class="db-ghost" data-page="-1" ${fs.offset ? '' : 'disabled'}>← Назад</button>
        <span class="muted">${d.total ? `${fs.offset + 1}–${Math.min(fs.offset + 100, d.total)} из ${num(d.total)}` : ''}</span>
        <button type="button" class="db-ghost" data-page="1" ${fs.offset + 100 < d.total ? '' : 'disabled'}>Вперёд →</button></div>`;
    body.querySelectorAll('[data-fg]').forEach(b => b.onclick = () => { st.f.group = b.dataset.fg; remember(); fs.offset = 0; load(); });
    body.querySelectorAll('[data-fsort]').forEach(b => b.onclick = () => { if (fs.sort === b.dataset.fsort) fs.desc = !fs.desc; else { fs.sort = b.dataset.fsort; fs.desc = true; } fs.offset = 0; drawFeed(); });
    body.querySelectorAll('[data-page]').forEach(b => b.onclick = () => { fs.offset = Math.max(0, fs.offset + Number(b.dataset.page) * 100); drawFeed(); });
  }

  // ---------------------------------------------------------------- причины
  function drawReasons() {
    const body = root.querySelector('[data-body]'), r = st.data.reasons, has = !!st.data.previous;
    if (!r.length) { body.innerHTML = '<div class="rv-empty"><b>Штрафов за период нет</b></div>'; return; }
    body.innerHTML = `<div class="pn-cards">${r.map(x => `<article class="pn-card" style="--c:${gColor(x.group)}">
        <header><span class="fb-chip" style="--c:${gColor(x.group)}">${esc(gTitle(x.group))}</span><h3>${esc(x.reason)}</h3></header>
        <div class="pn-nums"><div><span>Сумма</span><b>${rub(x.sum)} ₽</b></div><div><span>Штрафов</span><b>${num(x.count)}</b></div><div><span>Средний</span><b>${rub(x.avg)} ₽</b></div>
          <div><span>Доля</span><b>${num(x.share, 1)}%</b></div>${has ? `<div><span>К прошлому</span><b>${delta(x.sum, x.prev_sum)}</b></div>` : ''}</div>
        <p class="muted pn-meta">${ru(x.first)} — ${ru(x.last)} · ${num(x.articles)} артикул${x.articles % 10 === 1 && x.articles % 100 !== 11 ? '' : 'ов'}${x.fbs_linked ? ` · ${num(x.fbs_linked)} связаны с заданиями FBS` : ''}</p>
        <div class="pn-tip"><b>Что делать:</b> ${esc(x.tip)}</div>
        <table class="fb-mini">${x.top_articles.map(a => `<tr><td>${a.nm_id ? `<b>${esc(a.article || a.nm_id)}</b> <small class="muted">${a.nm_id}</small>` : '<span class="muted">Без артикула (по поставке)</span>'}</td><td class="r">${num(a.count)} шт</td><td class="r"><b>${rub(a.sum)} ₽</b></td></tr>`).join('')}</table>
        <button type="button" class="db-ghost pn-more" data-goreason="${esc(x.reason)}">Все штрафы этой причины →</button></article>`).join('')}</div>`;
    body.querySelectorAll('[data-goreason]').forEach(b => b.onclick = () => { st.f.reason = b.dataset.goreason; st.tab = 'feed'; st.fs.offset = 0; load(); });
  }

  // ---------------------------------------------------------------- артикулы
  function drawArticles() {
    const body = root.querySelector('[data-body]'), A = st.as, q = A.q.toLowerCase(), has = !!st.data.previous;
    const rows = st.data.articles.filter(r => !q || `${r.article} ${r.nm_id} ${r.subject} ${r.brand}`.toLowerCase().includes(q)).sort((a, b) => {
      const x = a[A.sort], y = b[A.sort];
      if (typeof x === 'string') return (A.desc ? -1 : 1) * x.localeCompare(y, 'ru', {numeric: true});
      if (x == null) return 1; if (y == null) return -1; return (A.desc ? -1 : 1) * (x - y); });
    const max = Math.max(1, ...rows.map(r => r.sum));
    const th = (k, t, cls = '') => `<th class="${cls}"><button type="button" class="rp-sort ${A.sort === k ? 'on' : ''}" data-asort="${k}">${t}<span>${A.sort === k ? (A.desc ? '↓' : '↑') : '↕'}</span></button></th>`;
    body.innerHTML = `<div class="rnp-tools"><input type="search" data-aq placeholder="Найти артикул" value="${esc(A.q)}"><span class="muted">${rows.length} артикулов со штрафами</span></div>
      <div class="rnp-scroll"><table class="rnp-table fb-groups"><thead><tr>${th('article', 'Артикул', 'l')}${th('count', 'Штрафов')}${th('sum', 'Сумма, ₽')}${has ? th('prev_sum', 'Прошлый период, ₽') : ''}
        ${th('revenue', 'Продажи, ₽')}${th('qty', 'Продано, шт')}${th('pct_revenue', '% от продаж')}${th('repeat', 'Повторы, ₽')}<th class="l">Главная причина</th><th class="l">Типы</th>${th('last', 'Последний', 'l')}</tr></thead><tbody>
      ${rows.map(x => `<tr><td class="l"><b>${esc(x.article || '—')}</b><small>${x.nm_id}${x.brand ? ' · ' + esc(x.brand) : ''}${x.subject ? ' · ' + esc(x.subject) : ''}</small></td>
        <td>${num(x.count)}</td><td><b>${num(x.sum)}</b></td>${has ? `<td>${num(x.prev_sum)} ${delta(x.sum, x.prev_sum)}</td>` : ''}<td>${num(x.revenue)}</td><td>${num(x.qty)}</td>
        <td class="${x.pct_revenue >= 3 ? 'fb-hot' : x.pct_revenue >= 1 ? 'fb-mid' : ''}">${x.pct_revenue == null ? '—' : num(x.pct_revenue, 2) + '%'}</td>
        <td title="Сумма по самой частой причине у артикула">${num(x.repeat)}</td><td class="l">${esc(short(x.top_reason))}</td><td class="l">${miniBar(x.groups, x.sum, max)}</td><td class="l">${ru(x.last)}</td></tr>`).join('')
        || `<tr><td colspan="11" class="muted l">Нет данных.</td></tr>`}</tbody></table></div>
      <p class="muted rp-note">% от продаж — штрафы артикула к его продажам за вычетом возвратов за тот же период. Красным — от 3%, серым — от 1%.</p>`;
    body.querySelectorAll('[data-asort]').forEach(b => b.onclick = () => { if (A.sort === b.dataset.asort) A.desc = !A.desc; else { A.sort = b.dataset.asort; A.desc = true; } drawArticles(); });
    const s = body.querySelector('[data-aq]');
    s.oninput = () => { A.q = s.value; const pos = s.selectionStart; drawArticles(); const n = root.querySelector('[data-aq]'); n.focus(); n.setSelectionRange(pos, pos); };
  }

  // ---------------------------------------------------------------- прочие удержания
  function drawDeductions() {
    const body = root.querySelector('[data-body]'), d = st.data.deductions, total = d.categories.reduce((a, c) => a + c.sum, 0) || 1;
    const colors = {ads: '#4c6ef5', jam: '#ae3ec9', reviews: '#f59f00', transit: '#12b886', utilization: '#868e96', other: '#adb5bd'};
    body.innerHTML = `<div class="db-note">Это не штрафы: WB удерживает их за свои услуги (реклама, подписки, баллы за отзывы, транзит, утилизация). Показаны для полной картины списаний.</div>
      <div class="fb-two"><section class="tr-box"><h3>По видам</h3>${d.categories.length ? `<div class="fb-bands">${d.categories.map(c => `<span style="width:${c.sum / total * 100}%;background:${colors[c.key] || '#adb5bd'}" title="${esc(c.title)}: ${num(c.sum)} ₽"></span>`).join('')}</div>
        <table class="fb-mini">${d.categories.map(c => `<tr><td><i style="background:${colors[c.key] || '#adb5bd'}"></i>${esc(c.title)}</td><td class="r">${num(c.count)}</td><td class="r"><b>${num(c.sum)} ₽</b></td><td class="r muted">${num(c.sum / total * 100, 0)}%</td></tr>`).join('')}</table>` : '<p class="muted">Удержаний нет.</p>'}</section>
        <section class="tr-box"><h3>Итого</h3><div class="pn-big">${num(st.data.summary.deductions)} ₽</div><p class="muted">${num(st.data.summary.deductions_count)} удержаний за период${st.data.previous ? ` · в прошлом периоде ${num(st.data.previous.deductions)} ₽` : ''}</p></section></div>
      <section class="tr-box"><h3>Список удержаний</h3><div class="rnp-scroll"><table class="rnp-table fb-feed"><thead><tr><th class="l">Дата</th><th class="l">Вид</th><th class="l">Описание</th><th>Сумма</th></tr></thead><tbody>
        ${d.items.map(r => `<tr><td class="l">${ru(r.day)}</td><td class="l">${esc(r.category)}</td><td class="l">${esc(r.reason)}${r.article ? `<small>${esc(r.article)} · ${r.nm_id}</small>` : ''}</td><td><b>${num(r.amount, 2)} ₽</b></td></tr>`).join('') || '<tr><td colspan="4" class="muted l">Удержаний нет.</td></tr>'}
      </tbody></table></div></section>`;
  }

  const style = document.createElement('style');
  style.textContent = `
.pn-hero{background:linear-gradient(135deg,#fff5f5,#fff0e6)}
.pn-check{border-radius:10px;padding:9px 13px;font-size:13px;margin:-4px 0 14px}.pn-check.ok{background:#e9f5ea;color:#2f6b2f}.pn-check.warn{background:#fff4e6;color:#a1520c}
.pn-click{cursor:pointer}.pn-click:hover td{background:#faf7ff}
.pn-top td{padding:7px 6px}.pn-top td.r{white-space:nowrap}.pn-nums b span{font-size:15px}.pn-top .fb-hbar{width:120px}
.pn-reason small{display:block;max-width:360px;white-space:normal}.pn-srid{font-family:ui-monospace,monospace;color:#6b7280;word-break:break-all}
.pn-feed-sum{margin-left:auto;font-size:13px}.pn-page .fb-csv{margin-left:10px}
.pn-reason-chip{display:inline-flex;align-items:center;gap:6px;background:#ede9fe;color:#5b21b6;border-radius:999px;padding:6px 8px 6px 12px;font-size:13px}
.pn-reason-chip button{border:0;background:#fff;border-radius:50%;width:22px;height:22px;cursor:pointer;color:#5b21b6;font-size:15px;line-height:1}
.pn-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:14px}
.pn-card{border:1px solid #e5e7eb;border-left:4px solid var(--c);border-radius:14px;padding:16px 18px;background:#fff;display:flex;flex-direction:column;gap:10px}
.pn-card header{display:flex;flex-direction:column;gap:6px}.pn-card h3{margin:0;font-size:15px;line-height:1.35}
.pn-nums{display:flex;flex-wrap:wrap;gap:8px 22px}.pn-nums span{display:block;font-size:12px;color:#6b7280}.pn-nums b{font-size:18px;font-variant-numeric:tabular-nums}
.pn-meta{margin:0;font-size:12px}.pn-tip{background:#f8f9fb;border-radius:10px;padding:10px 12px;font-size:13px;line-height:1.45}
.pn-more{align-self:flex-start}.pn-big{font-size:32px;font-weight:800;margin:6px 0}
@media(max-width:700px){.pn-cards{grid-template-columns:1fr}}`;
  document.head.appendChild(style);

  return {render};
})();
