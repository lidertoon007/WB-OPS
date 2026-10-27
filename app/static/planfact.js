/* WB OPS: «План-факт» — планы на месяц по бизнесу и артикулам, факт, отклонения и прогноз на конец месяца. */
window.WBOPSPlanFact = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const num = (v, d = 0) => v == null || v === '' || isNaN(v) ? '—' : Number(v).toLocaleString('ru-RU', {minimumFractionDigits: d, maximumFractionDigits: d});
  const MONTHS = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];
  const mTitle = m => { const [y, mm] = m.split('-').map(Number); return `${MONTHS[mm - 1][0].toUpperCase()}${MONTHS[mm - 1].slice(1)} ${y}`; };
  const shift = (m, n) => { const [y, mm] = m.split('-').map(Number); const d = new Date(y, mm - 1 + n, 1); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`; };
  const nowM = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`; };
  const ST = {good: ['Выполняется', '#2f9e44', '#e9f5ea'], warn: ['На грани', '#e8590c', '#fff4e6'], bad: ['Не выполняется', '#e03131', '#fdecec']};
  const st = {tab: 'summary', month: nowM(), f: {brand: '', category: ''}, data: null, chart: 'orders_sum', am: 'orders', as: {sort: 'fact', desc: true, q: '', mode: ''},
    pq: '', fill: {growth: 10, target: 'all', overwrite: false}};
  let root = null;
  try { Object.assign(st.f, JSON.parse(localStorage.getItem('wbops.pf.f') || '{}')); } catch (e) { /* без хранилища */ }
  const remember = () => { try { localStorage.setItem('wbops.pf.f', JSON.stringify(st.f)); } catch (e) { /* ignore */ } };
  const api = async (url, opt) => { const r = await fetch(url, opt); const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.detail || 'Ошибка запроса'); return d; };
  const M = () => Object.fromEntries(st.data.metrics.map(m => [m.key, m]));
  const fmt = (key, v) => { const m = M()[key]; return v == null ? '—' : m.unit === '%' ? num(v, 1) + '%' : num(v) + (m.unit === '₽' ? ' ₽' : ' шт'); };
  const label = key => { const m = M()[key]; return m.unit === '%' ? m.title : `${m.title}, ${m.unit}`; };
  const chip = s => s ? `<span class="pf-chip" style="--c:${ST[s][1]};--b:${ST[s][2]}">${ST[s][0]}</span>` : '<span class="muted">нет плана</span>';

  // ---------------------------------------------------------------- каркас
  async function render(section) {
    root = section;
    root.className = 'panel rv-page fb-page pf-page';
    root.innerHTML = `<div class="db-head"><h2>План-факт</h2><span class="muted db-fresh" data-fresh></span></div>
      <div class="db-filters" data-filters></div><div data-notes></div>
      <div class="rnp-tabs fb-tabs" role="tablist">${[['summary', 'Сводка'], ['articles', 'По артикулам'], ['plans', 'Планы']]
        .map(([k, t]) => `<button type="button" role="tab" data-tab="${k}">${t}</button>`).join('')}</div>
      <div data-body><p class="muted">Загрузка…</p></div>`;
    root.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => { st.tab = b.dataset.tab; drawBody(); });
    await load();
  }
  async function load() {
    const q = new URLSearchParams({month: st.month});
    if (st.f.brand) q.set('brand', st.f.brand);
    if (st.f.category) q.set('category', st.f.category);
    try { st.data = await api('/api/planfact?' + q); }
    catch (e) { root.querySelector('[data-body]').innerHTML = `<p class="rv-error">${esc(e.message)}</p>`; return; }
    drawFilters(); drawNotes(); drawBody();
  }
  function drawFilters() {
    const o = st.data.options || {}, f = st.f;
    const opt = (v, t, cur) => `<option value="${esc(v)}" ${String(v) === String(cur) ? 'selected' : ''}>${esc(t)}</option>`;
    const months = []; for (let i = -12; i <= 2; i++) months.push(shift(nowM(), i));
    if (!months.includes(st.month)) months.push(st.month);
    root.querySelector('[data-filters]').innerHTML = `
      <span class="pf-month"><button type="button" class="db-ghost" data-mshift="-1" aria-label="Прошлый месяц">‹</button>
        <select data-month aria-label="Месяц">${months.sort().reverse().map(m => opt(m, mTitle(m) + (m === nowM() ? ' (текущий)' : ''), st.month)).join('')}</select>
        <button type="button" class="db-ghost" data-mshift="1" aria-label="Следующий месяц">›</button></span>
      <select data-f="brand" aria-label="Бренд">${opt('', 'Все бренды', f.brand)}${(o.brands || []).map(b => opt(b, b, f.brand)).join('')}</select>
      <select data-f="category" aria-label="Категория">${opt('', 'Все категории', f.category)}${(o.categories || []).map(b => opt(b, b, f.category)).join('')}</select>
      <span class="rp-tools"><button type="button" class="db-ghost" data-sync>↻ Обновить данные WB</button></span>`;
    root.querySelector('[data-month]').onchange = e => { st.month = e.target.value; load(); };
    root.querySelectorAll('[data-mshift]').forEach(b => b.onclick = () => { st.month = shift(st.month, Number(b.dataset.mshift)); load(); });
    root.querySelectorAll('[data-f]').forEach(el => el.onchange = () => { st.f[el.dataset.f] = el.value; remember(); load(); });
    root.querySelector('[data-sync]').onclick = async e => {
      e.currentTarget.disabled = true; e.currentTarget.textContent = 'Загружаю…';
      try { await api('/api/rnp/sync', {method: 'POST'}); setTimeout(load, 60000); }
      catch (err) { alert(err.message); }
    };
    const s = st.data.synced_at;
    root.querySelector('[data-fresh]').textContent = s ? `Факт из РНП на ${new Date(s).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'})}` : 'Факт из РНП ещё не загружен';
  }
  function drawNotes() {
    const d = st.data, notes = [];
    if (!d.has_tokens) notes.push('Нет кабинета WB с ключом API — факт не загрузится. Добавьте ключ в «Кабинетах маркетплейсов».');
    if (d.rnp_state?.status === 'running') notes.push('РНП загружает свежие заказы и рекламу из WB — цифры обновятся через несколько минут.');
    if (d.no_cost) notes.push(`У ${num(d.no_cost)} заказов нет себестоимости — прибыль по ним завышена. Заполните её в «Товары → Номенклатура».`);
    root.querySelector('[data-notes]').innerHTML = notes.map(n => `<div class="db-note">${n}</div>`).join('');
  }
  function drawBody() {
    root.querySelectorAll('[data-tab]').forEach(b => { b.classList.toggle('active', b.dataset.tab === st.tab); b.setAttribute('aria-selected', String(b.dataset.tab === st.tab)); });
    ({summary: drawSummary, articles: drawArticles, plans: drawPlans}[st.tab])();
  }
  async function savePlan(scope, metric, value) {
    await api('/api/planfact/plan', {method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({month: st.month, items: [{scope, metric, value: value === '' || value == null ? null : Number(String(value).replace(/\s/g, '').replace(',', '.'))}]})});
  }

  // ---------------------------------------------------------------- сводка
  function drawSummary() {
    const d = st.data, t = d.total, body = root.querySelector('[data-body]');
    const planned = d.metrics.filter(m => t[m.key].plan != null);
    const cnt = {good: 0, warn: 0, bad: 0}; planned.forEach(m => t[m.key].status && cnt[t[m.key].status]++);
    const timePct = d.elapsed / d.days_total * 100;
    body.innerHTML = `<div class="pf-hero">
        <div><span>${mTitle(d.month)}${d.current ? '' : d.future ? ' · будущий месяц' : ' · месяц закрыт'}</span>
          <b>${d.current ? `Прошло ${d.elapsed} из ${d.days_total} дней` : d.future ? 'Месяц ещё не начался' : `${d.days_total} дней`}</b>
          <div class="pf-time"><i style="width:${d.future ? 0 : d.current ? timePct : 100}%"></i></div>
          <p>${planned.length ? `${cnt.good ? `<span style="color:#2f9e44">●</span> выполняется ${cnt.good}` : ''} ${cnt.warn ? `<span style="color:#e8590c">●</span> на грани ${cnt.warn}` : ''} ${cnt.bad ? `<span style="color:#e03131">●</span> не выполняется ${cnt.bad}` : ''} из ${planned.length} показателей с планом.`
            : 'Планы на этот месяц ещё не заданы — откройте вкладку «Планы» и нажмите «Заполнить по прошлому месяцу».'}
            ${d.current ? ` Прогноз — по среднему темпу ${d.pace_window === 'month' ? 'за полные дни месяца' : 'за последние 7 дней'}.` : ''}</p></div>
        ${planned.length ? '' : '<button type="button" class="btn" data-goplans>Задать планы</button>'}
      </div>
      <div class="pf-cards">${d.metrics.map(m => card(m, t[m.key])).join('')}</div>
      ${alerts()}
      <section class="tr-box"><div class="fb-lost-head"><h3>Накопительно: факт, план и прогноз</h3>
        <div class="rnp-tabs fb-mini-tabs">${d.metrics.filter(m => m.kind === 'sum').map(m => `<button type="button" data-chart="${m.key}" class="${st.chart === m.key ? 'active' : ''}">${esc(label(m.key))}</button>`).join('')}</div></div>
        ${cumulative()}</section>
      <div class="fb-two">
        <section class="tr-box"><h3>Артикулы по выполнению</h3><p class="muted fb-sub">Прогноз против плана, только артикулы с планом</p>${artStatus()}</section>
        <section class="tr-box"><h3>Сводка в Telegram</h3>${notifyBox()}</section>
      </div>
      <p class="muted rp-note">Факт — из РНП: заказы и реклама из API WB, выручка = заказы × процент выкупа артикула, прибыль — по юнит-экономике за прошлые 60 дней с себестоимостью, налогом и рекламой.
        Прогноз на конец месяца = факт по вчера + средний темп × оставшиеся дни. Для рекламы и ДРР план — это лимит: перерасход отмечается красным.</p>`;
    body.querySelectorAll('[data-chart]').forEach(b => b.onclick = () => { st.chart = b.dataset.chart; drawSummary(); });
    body.querySelectorAll('[data-goplans]').forEach(b => b.onclick = () => { st.tab = 'plans'; drawBody(); });
    body.querySelectorAll('[data-goart]').forEach(b => b.onclick = () => { st.as.q = b.dataset.goart; st.tab = 'articles'; drawBody(); });
    body.querySelectorAll('[data-pedit]').forEach(b => b.onclick = () => {
      const k = b.dataset.pedit, cur = t[k].plan_source === 'set' ? t[k].plan : '';
      const v = prompt(`План на ${mTitle(st.month).toLowerCase()} — ${label(k)}:`, cur ?? '');
      if (v === null) return;
      savePlan('', k, v.trim()).then(load).catch(e => alert(e.message));
    });
    bindNotify(body);
  }
  function card(m, v) {
    const s = v.status, has = v.plan != null, sum = m.kind === 'sum';
    const pf = sum && has && v.plan ? Math.min(100, v.fact / v.plan * 100) : 0, pfc = sum && has && v.plan ? Math.min(100, v.forecast / v.plan * 100) : 0;
    let foot = '';
    if (sum && has && st.data.current) {
      if (m.dir === 'up') foot = v.need_per_day ? `Нужно ${fmt(m.key, v.need_per_day)} в день, сейчас ${fmt(m.key, v.pace_per_day)}` : 'План уже выполнен';
      else foot = v.left_per_day != null ? (v.left_per_day >= 0 ? `Можно ${fmt(m.key, v.left_per_day)} в день, сейчас ${fmt(m.key, v.pace_per_day)}` : 'Лимит уже превышен') : '';
      if (v.gap != null) foot += `<br>${m.dir === 'up' ? (v.gap >= 0 ? 'Опережение' : 'Отставание') : (v.gap > 0 ? 'Перерасход' : 'Экономия')} от графика: ${fmt(m.key, Math.abs(v.gap))}`;
    } else if (!sum && has && v.diff != null) foot = `${v.diff >= 0 ? '+' : ''}${num(v.diff, 1)} п.п. к плану`;
    else if (!has) foot = v.prev != null ? `Прошлый месяц: ${fmt(m.key, v.prev)}` : '';
    return `<article class="pf-card ${s || ''}" style="${s ? `--c:${ST[s][1]};--b:${ST[s][2]}` : ''}">
      <div class="pf-card-h"><span>${esc(label(m.key))}${m.dir === 'down' ? ' <small class="muted">лимит</small>' : ''}</span>${chip(s)}</div>
      <div class="pf-nums"><div><small>План</small><b>${has ? fmt(m.key, v.plan) : '—'}</b>${v.plan_source === 'articles' ? '<small class="muted">сумма артикулов</small>' : ''}</div>
        <div><small>Факт</small><b>${fmt(m.key, v.fact)}</b>${sum && has && v.done_pct != null ? `<small class="muted">${num(v.done_pct, 0)}% плана</small>` : ''}</div>
        <div><small>Прогноз</small><b>${st.data.future ? '—' : fmt(m.key, v.forecast)}</b>${sum && has && v.forecast_pct != null ? `<small style="color:${s ? ST[s][1] : 'inherit'}">${num(v.forecast_pct, 0)}% плана</small>` : ''}</div></div>
      ${sum && has ? `<div class="pf-bar" title="Факт ${num(pf, 0)}% · прогноз ${num(v.forecast_pct, 0)}% плана"><i class="pf-fc" style="width:${pfc}%"></i><i class="pf-f" style="width:${pf}%"></i>${st.data.current ? `<em style="left:${st.data.elapsed / st.data.days_total * 100}%" title="Где должен быть факт сегодня"></em>` : ''}</div>` : ''}
      <div class="pf-foot muted">${foot}</div>
      <button type="button" class="pf-edit" data-pedit="${m.key}">${has && v.plan_source === 'set' ? 'Изменить план' : 'Задать план'}</button></article>`;
  }
  function alerts() {
    const a = st.data.alerts;
    if (!a.length) return Object.values(st.data.total).some(v => v.plan != null) ? '<div class="pn-check ok">✓ Существенных отклонений от плана нет.</div>' : '';
    return `<section class="tr-box ad-ins"><h3>Отклонения от плана</h3><div class="ad-ins-list">${a.map(x => `<button type="button" class="ad-ins-item ${x.level}" ${x.scope !== 'total' ? `data-goart="${esc(x.text.split(':')[0])}"` : 'data-goplans'}>
      <b>${x.level === 'bad' ? '⚠' : '•'} ${x.scope === 'total' ? 'Весь бизнес' : 'Артикул'}</b><span>${esc(x.text)}</span></button>`).join('')}</div></section>`;
  }
  function cumulative() {
    const d = st.data, k = st.chart, v = d.total[k], days = d.daily, n = days.length;
    let acc = 0; const fact = [];
    days.forEach((x, i) => { if (x[k] != null && (d.current ? i < d.elapsed + 1 : !d.future)) { acc += x[k]; fact.push([i, acc]); } });
    const plan = v.plan, fc = v.forecast;
    const hi = Math.max(1, plan || 0, fc || 0, acc) * 1.08;
    const W = 1000, H = 240, P = 46, X = i => P + i / Math.max(1, n - 1) * (W - 2 * P), Y = val => H - 26 - val / hi * (H - 50);
    const path = fact.map((p, i) => `${i ? 'L' : 'M'}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(' ');
    const lastF = fact[fact.length - 1];
    const lbls = days.map((x, i) => (i % Math.ceil(n / 15) === 0 || i === n - 1) ? `<text x="${X(i)}" y="${H - 8}" class="fb-ax" text-anchor="middle">${x.day.slice(8, 10)}.${x.day.slice(5, 7)}</text>` : '').join('');
    return `<div class="fb-chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Накопительный факт и план">
      <text x="${P}" y="14" class="fb-ax">${fmt(k, hi)}</text><line x1="${P}" x2="${W - P}" y1="${H - 26}" y2="${H - 26}" stroke="#e5e7eb"/>${lbls}
      ${plan != null ? `<line x1="${X(0)}" y1="${Y(plan / n)}" x2="${X(n - 1)}" y2="${Y(plan)}" stroke="#adb5bd" stroke-width="2" stroke-dasharray="6 5"/><text x="${X(n - 1)}" y="${Y(plan) - 6}" class="fb-ax" text-anchor="end">план ${fmt(k, plan)}</text>` : ''}
      ${lastF && d.current && fc != null ? `<line x1="${X(lastF[0])}" y1="${Y(lastF[1])}" x2="${X(n - 1)}" y2="${Y(fc)}" stroke="#7c3aed" stroke-width="2" stroke-dasharray="3 4"/><text x="${X(n - 1)}" y="${Y(fc) + 14}" class="fb-ax" text-anchor="end">прогноз ${fmt(k, fc)}</text>` : ''}
      <path d="${path}" fill="none" stroke="#7c3aed" stroke-width="2.5"/>
      ${fact.map(p => `<circle cx="${X(p[0])}" cy="${Y(p[1])}" r="3" fill="#7c3aed"><title>${days[p[0]].day.split('-').reverse().join('.')}: ${fmt(k, p[1])} накопительно, за день ${fmt(k, days[p[0]][k])}${plan != null ? `, по плану к этому дню ${fmt(k, plan * (p[0] + 1) / n)}` : ''}</title></circle>`).join('')}
      </svg></div><p class="muted fb-sub"><span style="color:#7c3aed">━</span> факт накопительно · <span style="color:#7c3aed">┄</span> прогноз · <span style="color:#adb5bd">┄</span> план равномерно по дням</p>`;
  }
  function artStatus() {
    const c = st.data.status_counts;
    const rows = st.data.metrics.filter(m => c[m.key]).map(m => { const x = c[m.key], tot = (x.good || 0) + (x.warn || 0) + (x.bad || 0);
      return `<tr><td>${esc(label(m.key))}</td><td><div class="fb-bands sm" style="width:160px">${['good', 'warn', 'bad'].map(s => x[s] ? `<span style="width:${x[s] / tot * 100}%;background:${ST[s][1]}" title="${ST[s][0]}: ${x[s]}"></span>` : '').join('')}</div></td>
        <td class="r"><span style="color:#2f9e44">${x.good || 0}</span> / <span style="color:#e8590c">${x.warn || 0}</span> / <span style="color:#e03131">${x.bad || 0}</span></td></tr>`; }).join('');
    return rows ? `<table class="fb-mini">${rows}</table><p class="muted fb-sub">Выполняются / на грани / не выполняются. Подробно — во вкладке «По артикулам».</p>` : '<p class="muted">Планов по артикулам пока нет.</p>';
  }
  function notifyBox() {
    const s = st.data.settings;
    return `<label class="r-check"><input type="checkbox" data-notify ${s.notify ? 'checked' : ''}> Каждое утро присылать сводку план-факта</label>
      <label class="pf-hour">в <select data-hour>${Array.from({length: 24}, (_, h) => `<option value="${h}" ${h === s.hour ? 'selected' : ''}>${String(h).padStart(2, '0')}:00</option>`).join('')}</select> по Москве</label>
      <p class="muted fb-sub">В сводке — выполнение по каждому показателю и отстающие артикулы. Канал — из «Настроек уведомлений».</p>
      <button type="button" class="db-ghost" data-ntest>Отправить сейчас</button>`;
  }
  function bindNotify(body) {
    const save = () => api('/api/planfact/settings', {method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({notify: body.querySelector('[data-notify]').checked, hour: Number(body.querySelector('[data-hour]').value)})}).then(s => { st.data.settings = s; }).catch(e => alert(e.message));
    body.querySelector('[data-notify]').onchange = save;
    body.querySelector('[data-hour]').onchange = save;
    body.querySelector('[data-ntest]').onclick = async e => { e.currentTarget.disabled = true;
      try { const r = await api('/api/planfact/test', {method: 'POST'}); alert(r.sent ? 'Сводка отправлена' : 'Не удалось отправить — проверьте настройки Telegram'); } catch (err) { alert(err.message); }
      e.currentTarget.disabled = false; };
  }

  // ---------------------------------------------------------------- по артикулам
  function drawArticles() {
    const d = st.data, body = root.querySelector('[data-body]'), A = st.as, k = st.am, m = M()[k], q = A.q.toLowerCase(), sum = m.kind === 'sum';
    const modes = [['', 'Все', () => true], ['plan', 'С планом', a => a.metrics[k].plan != null], ['bad', 'Не выполняются', a => a.metrics[k].status === 'bad'],
      ['warn', 'На грани', a => a.metrics[k].status === 'warn'], ['good', 'Выполняются', a => a.metrics[k].status === 'good'], ['noplan', 'Без плана', a => a.metrics[k].plan == null]];
    const md = modes.find(x => x[0] === A.mode) || modes[0];
    const val = (a, f) => f === 'article' ? a.article : f === 'pct' ? (sum ? a.metrics[k].forecast_pct : a.metrics[k].diff) : a.metrics[k][f];
    const rows = d.articles.filter(md[2]).filter(a => !q || `${a.article} ${a.nm_id || ''} ${a.brand}`.toLowerCase().includes(q)).sort((x, y) => {
      const a = val(x, A.sort), b = val(y, A.sort);
      if (typeof a === 'string') return (A.desc ? -1 : 1) * a.localeCompare(b, 'ru', {numeric: true});
      if (a == null) return 1; if (b == null) return -1; return (A.desc ? -1 : 1) * (a - b); });
    const th = (f, t, cls = '') => `<th class="${cls}"><button type="button" class="rp-sort ${A.sort === f ? 'on' : ''}" data-asort="${f}">${t}<span>${A.sort === f ? (A.desc ? '↓' : '↑') : '↕'}</span></button></th>`;
    body.innerHTML = `<div class="sk-chips">${d.metrics.map(x => `<button type="button" class="sk-chip ${k === x.key ? 'on' : ''}" data-am="${x.key}">${esc(label(x.key))}</button>`).join('')}</div>
      <div class="sk-chips">${modes.map(([key, t, fn]) => `<button type="button" class="sk-chip ${A.mode === key ? 'on' : ''}" data-amode="${key}">${t} <b>${d.articles.filter(fn).length}</b></button>`).join('')}
        <input type="search" data-aq placeholder="Найти артикул" value="${esc(A.q)}" class="ad-qsearch"></div>
      <div class="rnp-scroll"><table class="rnp-table fb-groups pf-table"><thead><tr>${th('article', 'Артикул', 'l')}<th>План ${m.dir === 'down' ? '(лимит)' : ''}</th>${th('fact', 'Факт')}${th('forecast', 'Прогноз')}
        ${th('pct', sum ? 'Прогноз к плану' : 'Отклонение', 'l')}${sum && d.current ? `${th('pace_per_day', 'Темп в день')}<th>${m.dir === 'up' ? 'Нужно в день' : 'Можно в день'}</th>` : ''}${th('prev', 'Прошлый месяц')}<th class="l">Статус</th></tr></thead><tbody>
      ${rows.slice(0, 500).map(a => { const v = a.metrics[k]; return `<tr><td class="l"><b>${esc(a.article)}</b><small>${a.nm_id || ''}${a.category ? ' · ' + esc(a.category) : ''}</small></td>
        <td><input class="pf-in" data-scope="${esc(a.key)}" data-metric="${k}" value="${v.plan ?? ''}" placeholder="—" inputmode="decimal" aria-label="План"></td>
        <td>${fmt(k, v.fact)}</td><td>${d.future ? '—' : fmt(k, v.forecast)}</td>
        <td class="l">${sum ? (v.plan ? `<span class="fb-hbar"><i style="width:${Math.min(100, v.forecast_pct || 0)}%;background:${v.status ? ST[v.status][1] : '#adb5bd'}"></i></span> ${num(v.forecast_pct, 0)}%` : '—') : (v.diff != null ? `${v.diff >= 0 ? '+' : ''}${num(v.diff, 1)} п.п.` : '—')}</td>
        ${sum && d.current ? `<td>${fmt(k, v.pace_per_day)}</td><td>${m.dir === 'up' ? (v.need_per_day == null ? '—' : v.need_per_day ? fmt(k, v.need_per_day) : '✓') : (v.left_per_day == null ? '—' : fmt(k, v.left_per_day))}</td>` : ''}
        <td>${fmt(k, v.prev)}</td><td class="l">${chip(v.status)}</td></tr>`; }).join('') || '<tr><td colspan="9" class="muted l">Нет артикулов.</td></tr>'}</tbody></table></div>
      <p class="muted rp-note">План можно вписать прямо в таблицу — сохраняется сразу. План заказов и маржи артикула синхронизирован с РНП.</p>`;
    body.querySelectorAll('[data-am]').forEach(b => b.onclick = () => { st.am = b.dataset.am; drawArticles(); });
    body.querySelectorAll('[data-amode]').forEach(b => b.onclick = () => { A.mode = b.dataset.amode; drawArticles(); });
    body.querySelectorAll('[data-asort]').forEach(b => b.onclick = () => { const f = b.dataset.asort; if (A.sort === f) A.desc = !A.desc; else { A.sort = f; A.desc = true; } drawArticles(); });
    const s = body.querySelector('[data-aq]');
    s.oninput = () => { A.q = s.value; const pos = s.selectionStart; drawArticles(); const n = root.querySelector('[data-aq]'); n.focus(); n.setSelectionRange(pos, pos); };
    bindInputs(body);
  }
  function bindInputs(body) {
    body.querySelectorAll('.pf-in').forEach(inp => {
      inp.onkeydown = e => { if (e.key === 'Enter') inp.blur(); };
      inp.onchange = async () => {
        inp.classList.add('saving');
        try { await savePlan(inp.dataset.scope, inp.dataset.metric, inp.value.trim()); inp.classList.remove('saving'); inp.classList.add('saved'); clearTimeout(st.reload); st.reload = setTimeout(load, 900); }
        catch (e) { inp.classList.remove('saving'); inp.classList.add('err'); alert(e.message); }
      };
    });
  }

  // ---------------------------------------------------------------- планы
  function drawPlans() {
    const d = st.data, body = root.querySelector('[data-body]'), F = st.fill, q = st.pq.toLowerCase();
    const cell = (scope, key, v) => `<td><input class="pf-in" data-scope="${esc(scope)}" data-metric="${key}" value="${v.plan_source === 'articles' ? '' : v.plan ?? ''}"
      placeholder="${v.plan_source === 'articles' ? num(v.plan) + ' (сумма)' : v.prev != null ? num(v.prev, M()[key].unit === '%' ? 1 : 0) : '—'}" inputmode="decimal" aria-label="${esc(label(key))}"></td>`;
    const arts = d.articles.filter(a => !q || `${a.article} ${a.nm_id || ''}`.toLowerCase().includes(q));
    body.innerHTML = `<section class="tr-box pf-fill"><h3>Заполнить планы по прошлому месяцу</h3>
        <p class="muted fb-sub">Возьмём факт за прошлый месяц (${mTitle(shift(st.month, -1)).toLowerCase()}), пересчитаем на число дней выбранного месяца и добавим рост. Реклама — без роста.</p>
        <div class="pf-fill-row"><label>Рост, % <input type="number" data-fill="growth" value="${F.growth}" step="1"></label>
          <label>Что заполнить <select data-fill="target"><option value="all" ${F.target === 'all' ? 'selected' : ''}>Весь бизнес и артикулы</option><option value="total" ${F.target === 'total' ? 'selected' : ''}>Только весь бизнес</option><option value="articles" ${F.target === 'articles' ? 'selected' : ''}>Только артикулы</option></select></label>
          <label class="r-check"><input type="checkbox" data-fill="overwrite" ${F.overwrite ? 'checked' : ''}> Перезаписать уже заданные</label>
          <button type="button" class="btn" data-dofill>Заполнить</button></div></section>
      <div class="rnp-tools"><input type="search" data-pq placeholder="Найти артикул" value="${esc(st.pq)}"><span class="muted">Серым в полях — факт прошлого месяца для ориентира. Пустое поле — плана нет.</span></div>
      <div class="rnp-scroll"><table class="rnp-table fb-groups pf-table pf-grid"><thead><tr><th class="l">Планы на ${mTitle(st.month).toLowerCase()}</th>${d.metrics.map(m => `<th>${esc(label(m.key))}${m.dir === 'down' ? '<small>лимит</small>' : ''}</th>`).join('')}</tr></thead><tbody>
        <tr class="pf-total"><td class="l"><b>Весь бизнес</b><small>если пусто — сумма планов артикулов</small></td>${d.metrics.map(m => cell('', m.key, d.total[m.key])).join('')}</tr>
        ${arts.slice(0, 400).map(a => `<tr><td class="l"><b>${esc(a.article)}</b><small>${a.nm_id || ''}${a.category ? ' · ' + esc(a.category) : ''}</small></td>${d.metrics.map(m => cell(a.key, m.key, a.metrics[m.key])).join('')}</tr>`).join('')}
      </tbody></table></div>`;
    body.querySelectorAll('[data-fill]').forEach(el => el.onchange = () => { F[el.dataset.fill] = el.type === 'checkbox' ? el.checked : el.type === 'number' ? Number(el.value) : el.value; });
    body.querySelector('[data-dofill]').onclick = async e => {
      e.currentTarget.disabled = true;
      try { const r = await api('/api/planfact/autofill', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({month: st.month, ...F})});
        alert(r.written ? `Заполнено планов: ${r.written}` : 'Нечего заполнять: планы уже заданы (включите «Перезаписать») или за прошлый месяц нет данных'); await load(); }
      catch (err) { alert(err.message); e.currentTarget.disabled = false; }
    };
    const s = body.querySelector('[data-pq]');
    s.oninput = () => { st.pq = s.value; const pos = s.selectionStart; drawPlans(); const n = root.querySelector('[data-pq]'); n.focus(); n.setSelectionRange(pos, pos); };
    bindInputs(body);
  }

  const style = document.createElement('style');
  style.textContent = `
.pf-month{display:inline-flex;align-items:center;gap:4px}.pf-month select{min-width:190px}
.pf-hero{display:flex;justify-content:space-between;align-items:center;gap:16px;background:linear-gradient(135deg,#f3f0ff,#eef6ff);border:1px solid #d9d2fb;border-radius:14px;padding:16px 20px;margin-bottom:14px}
.pf-hero span{font-size:13px;color:#4c3d7a;font-weight:600}.pf-hero b{display:block;font-size:24px;color:#3b2a7a;margin:4px 0 8px}.pf-hero p{margin:8px 0 0;font-size:13px;color:#4b5563}
.pf-time{height:8px;width:min(520px,100%);background:#e4defc;border-radius:4px;overflow:hidden}.pf-time i{display:block;height:100%;background:#7c3aed}
.pf-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;margin-bottom:14px}
.pf-card{position:relative;border:1px solid #e5e7eb;border-top:4px solid var(--c,#d1d5db);border-radius:14px;padding:14px 16px 12px;background:#fff;display:flex;flex-direction:column;gap:10px}
.pf-card-h{display:flex;justify-content:space-between;align-items:center;gap:8px}.pf-card-h span{font-weight:700;font-size:14px}
.pf-chip{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:700;color:var(--c);background:var(--b);border-radius:999px;padding:3px 9px;white-space:nowrap}
.pf-nums{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.pf-nums small{display:block;font-size:11px;color:#6b7280}.pf-nums b{display:block;font-size:17px;font-variant-numeric:tabular-nums;white-space:nowrap}
.pf-bar{position:relative;height:10px;background:#f1f2f4;border-radius:5px;overflow:visible}.pf-bar i{position:absolute;left:0;top:0;height:100%;border-radius:5px}
.pf-bar .pf-fc{background:var(--b,#e5e7eb);border:1px dashed var(--c,#adb5bd);box-sizing:border-box}.pf-bar .pf-f{background:var(--c,#7c3aed)}
.pf-bar em{position:absolute;top:-4px;width:2px;height:18px;background:#111827;opacity:.55}
.pf-foot{font-size:12px;line-height:1.45;min-height:17px}
.pf-edit{align-self:flex-start;border:0;background:none;color:#6d28d9;font:inherit;font-size:12px;font-weight:600;cursor:pointer;padding:0}
.pf-in{width:110px;height:32px;border:1px solid #e3e5ea;border-radius:8px;padding:0 8px;font:inherit;text-align:right}
.pf-in.saving{border-color:#f59f00}.pf-in.saved{border-color:#2f9e44;background:#f4fbf4}.pf-in.err{border-color:#e03131}
.pf-grid th small{display:block;font-weight:400;color:#6b7280}.pf-total td{background:#faf7ff}
.pf-fill-row{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}.pf-fill-row label{display:grid;gap:4px;font-size:13px;font-weight:600}
.pf-fill-row input[type=number],.pf-fill-row select{height:38px;border:1px solid #e3e5ea;border-radius:9px;padding:0 10px;font:inherit}.pf-fill-row input[type=number]{width:100px}
.pf-hour{display:inline-flex;gap:6px;align-items:center;margin:8px 0;font-size:13px}.pf-hour select{height:32px;border:1px solid #e3e5ea;border-radius:8px}
@media(max-width:700px){.pf-hero{flex-direction:column;align-items:flex-start}.pf-cards{grid-template-columns:1fr}}`;
  document.head.appendChild(style);

  return {render};
})();
