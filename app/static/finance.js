/* WB OPS · Финансы: ОПиУ из папки opiu (opiu_all.py). */
window.WBOPSFinance = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const rub = v => v == null ? '–' : Math.round(v).toLocaleString('ru-RU');
  const pct = v => v == null ? '–' : (v * 100).toLocaleString('ru-RU', {minimumFractionDigits: 1, maximumFractionDigits: 1}) + '%';
  const neg = v => v != null && v < 0 ? ' class="f-neg"' : '';
  let root, report = null, polling = null;
  // Фильтры: кабинет ("" — все, «Ozon» — площадка, «Ozon|Еврозоо» — кабинет), период таблицы, расшифровка
  const flt = {cabinet: '', lastMonths: 6, showDetails: false};
  try { Object.assign(flt, JSON.parse(localStorage.getItem('wbops.finance.f') || '{}')); } catch (e) { /* без хранилища */ }
  const remember = () => { try { localStorage.setItem('wbops.finance.f', JSON.stringify(flt)); } catch (e) { /* ignore */ } };
  const opt = (value, label, current) => `<option value="${esc(value)}"${value === current ? ' selected' : ''}>${esc(label)}</option>`;

  function cabinetSelect(cabs) {
    const perMp = new Map(), groups = new Map();
    cabs.forEach(c => {
      perMp.set(c.marketplace, (perMp.get(c.marketplace) || 0) + 1);
      const g = c.marketplace + (c.legal_name ? ' · ' + c.legal_name : '');
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(c);
    });
    const whole = [...perMp].filter(([, n]) => n > 1).map(([mp]) => opt(mp, `${mp} — все кабинеты`, flt.cabinet)).join('');
    return `<select data-f-cabinet class="btn secondary" aria-label="Кабинет">${opt('', 'Все кабинеты', flt.cabinet)}
      ${whole ? `<optgroup label="Маркетплейс целиком">${whole}</optgroup>` : ''}
      ${[...groups].map(([g, list]) => `<optgroup label="${esc(g)}">${list.map(c => opt(c.key, c.cabinet, flt.cabinet)).join('')}</optgroup>`).join('')}</select>`;
  }

  function runCabinetSelect(cabs) {
    const options = cabs.filter(c => c.panel_id || c.cabinet).map(c =>
      opt(c.panel_id || c.cabinet, c.cabinet, '')).join('');
    return options ? `<select data-f-run-cabinet class="btn secondary" aria-label="Кабинет для загрузки"><option value="">Все кабинеты</option>${options}</select>` : '';
  }

  async function api(url, options) {
    const r = await fetch(url, options);
    let d = {};
    try { d = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error(d.detail || ('Ошибка ' + r.status));
    return d;
  }

  function kpiCard(title, value, sub, cls = '') {
    return `<article class="f-kpi ${cls}"><small>${esc(title)}</small><strong${neg(value)}>${rub(value)}</strong><span class="muted">${sub || ''}</span></article>`;
  }

  function renderKpi(s) {
    const k = s.kpi, rev = k.revenue?.total, share = x => rev ? pct(Math.abs(x ?? 0) / rev) + ' выручки' : '';
    const last = s.periods.at(-1) || '';
    return `<div class="f-kpis">
      ${kpiCard('Выручка за вычетом возвратов', rev, last ? `${esc(last)}: ${rub(k.revenue?.last)}` : '')}
      ${kpiCard('Расходы маркетплейсов', k.mp_expenses?.total, share(k.mp_expenses?.total))}
      ${kpiCard('Результат по данным МП', k.mp_result?.total, share(k.mp_result?.total))}
      ${kpiCard('Себестоимость', k.cogs?.total, share(k.cogs?.total))}
      ${kpiCard('Налог', k.tax?.total, share(k.tax?.total))}
      ${kpiCard('Прибыль', k.profit?.total, `${s.quality?.reliable ? 'рентабельность' : 'оценка, данные неполные'} ${pct(k.margin?.total)}`, 'f-kpi-main' + (s.quality?.reliable ? '' : ' f-kpi-warn'))}
    </div>`;
  }

  function renderTable(s) {
    const n = s.periods.length, from = flt.lastMonths ? Math.max(0, n - flt.lastMonths) : 0;
    const periods = s.periods.slice(from).reverse(), fc = s.forecast_title;
    const head = `<tr><th class="f-sticky">Статья</th><th>Итого</th><th>% выр.</th>${fc ? `<th class="f-fc">${esc(fc)}</th>` : ''}${periods.map(p => `<th>${esc(p)}</th>`).join('')}</tr>`;
    const body = s.rows.map(r => {
      if (r.kind === 'section') return `<tr class="f-section"><td class="f-sticky" colspan="${3 + periods.length + (fc ? 1 : 0)}">${esc(r.label)}</td></tr>`;
      if (r.kind === 'detail' && !flt.showDetails) return '';
      const isShare = r.kind === 'share', fmt = isShare ? pct : rub;
      const cells = r.values.slice(from).reverse().map(v => `<td${neg(v)}>${fmt(v)}</td>`).join('');
      return `<tr class="f-${r.kind}${/^ПРИБЫЛЬ/.test(r.label) ? ' f-profit' : ''}"><td class="f-sticky">${esc(r.label)}</td>
        <td${neg(r.total)}>${fmt(r.total)}</td><td>${isShare ? '' : pct(r.pct)}</td>${fc ? `<td class="f-fc"${neg(r.forecast)}>${fmt(r.forecast)}</td>` : ''}${cells}</tr>`;
    }).join('');
    return `<div class="f-scroll"><table class="f-table">${head}${body}</table></div>`;
  }

  function renderControl(rows) {
    if (!rows.length) return '<p class="muted">Лист «Контроль» пуст.</p>';
    return `<div class="f-scroll"><table class="f-table f-control"><tr><th>Маркетплейс</th><th>Кабинет</th><th>Показатель</th><th>Значение</th><th>Статус</th></tr>
      ${rows.map(r => `<tr class="${r.problem ? 'f-problem' : ''}"><td>${esc(r.marketplace)}</td><td>${esc(r.cabinet)}</td><td>${esc(r.metric)}</td>
      <td>${typeof r.value === 'number' ? (Math.abs(r.value) < 10 && !Number.isInteger(r.value) ? r.value.toLocaleString('ru-RU') : rub(r.value)) : esc(r.value)}</td><td>${esc(r.status)}</td></tr>`).join('')}</table></div>`;
  }

  function renderGoods(g, query = '') {
    const h = g.header, idx = name => h.indexOf(name);
    const iArt = idx('Артикул'), iMp = idx('Маркетплейс'), iRev = idx('Выручка');
    if (iArt < 0) return '<p class="muted">Лист «Товары» не найден.</p>';
    let rows = g.rows.filter(r => iMp < 0 || r[iMp] === (g.scope || 'Все маркетплейсы'));
    if (query) rows = rows.filter(r => String(r[iArt] ?? '').toLowerCase().includes(query.toLowerCase()));
    if (iRev >= 0) rows.sort((a, b) => (b[iRev] || 0) - (a[iRev] || 0));
    const cols = h.map((name, i) => [name, i]).filter(([name, i]) => i !== iMp && name !== 'Название');
    return `<div class="f-scroll"><table class="f-table"><tr>${cols.map(([n]) => `<th>${esc(n)}</th>`).join('')}</tr>
      ${rows.map(r => `<tr>${cols.map(([n, i]) => { const v = r[i]; return `<td${typeof v === 'number' ? neg(v) : ''}>${typeof v === 'number' ? (/%/.test(n) ? pct(v) : rub(v)) : esc(v)}</td>`; }).join('')}</tr>`).join('') || '<tr><td colspan="9" class="muted">Нет товаров</td></tr>'}</table></div>`;
  }

  function renderReport() {
    const box = root.querySelector('[data-f-report]');
    if (!report) return;
    const s = report.summary, problems = report.control.filter(r => r.problem);
    const fs = report.finance_status || {}, cabs = Object.entries(fs.cabinets || {});
    const quality = report.quality || {};
    const statusTitle = {complete: 'полностью загружен', cache: 'из кэша', api_error: 'ошибка API', skipped: 'пропущен', loading: 'загружается', interrupted: 'прерван'};
    const statusClass = st => st === 'complete' ? 'f-status-ok' : (st === 'cache' ? 'f-status-cache' : 'f-status-bad');
    const activity = cabs.length ? `<div class="f-cab-status"><b>Кабинеты в последнем запуске:</b>${cabs.map(([name, c]) => `<span class="f-status ${statusClass(c.status)}"><b>${esc(name)}</b> — ${esc(statusTitle[c.status] || c.status || 'неизвестно')}${c.rows != null ? `, ${rub(c.rows)} строк` : ''}</span>`).join('')}</div>` : '';
    box.innerHTML = `
      <p class="muted">${esc(s.subtitle)} Файл: <b>${esc(report.file)}</b>, собран ${esc(report.updated_at.replace('T', ' '))}.</p>
      ${activity}
      ${!quality.reliable ? `<div class="f-alert"><b>Прибыль нельзя считать надёжной.</b> ${esc((quality.reasons || []).slice(0, 3).join('; '))}</div>` : ''}
      ${problems.length ? `<div class="f-alert"><b>На листе «Контроль» есть замечания (${problems.length}):</b><ul>${problems.slice(0, 6).map(p => `<li>${esc(p.marketplace)}${p.cabinet && p.cabinet !== '—' ? ' · ' + esc(p.cabinet) : ''}: ${esc(p.metric)} — ${esc(typeof p.value === 'number' ? rub(p.value) : p.value)} <i>${esc(p.status)}</i></li>`).join('')}</ul></div>` : '<div class="f-ok">Сверки сошлись, замечаний нет.</div>'}
      ${renderKpi(s)}
      <div class="f-head"><h3>ОПиУ по месяцам</h3><div class="toolbar">
        ${cabinetSelect(report.cabinets || [])}
        ${runCabinetSelect(report.cabinets || [])}
        <label class="r-check"><input type="checkbox" data-f-details ${flt.showDetails ? 'checked' : ''}> ${flt.cabinet ? 'Расшифровка статей' : 'По маркетплейсам'}</label>
        <select data-f-months class="btn secondary"><option value="6"${flt.lastMonths === 6 ? ' selected' : ''}>Последние 6 мес.</option><option value="12"${flt.lastMonths === 12 ? ' selected' : ''}>Последние 12 мес.</option><option value="0"${!flt.lastMonths ? ' selected' : ''}>Весь период</option></select></div></div>
      ${renderTable(s)}
      <h3>Контроль</h3>${renderControl(report.control)}
      <div class="f-head"><h3>Товары за период</h3><input data-f-goods-search placeholder="Поиск по артикулу"></div>
      <div data-f-goods>${renderGoods(report.goods)}</div>`;
    box.querySelector('[data-f-cabinet]').onchange = e => { flt.cabinet = e.target.value; remember(); loadReport(); };
    const runCab = box.querySelector('[data-f-run-cabinet]');
    if (runCab) runCab.onchange = e => { root.dataset.financeCabinet = e.target.value; };
    box.querySelector('[data-f-details]').onchange = e => { flt.showDetails = e.target.checked; remember(); renderReport(); };
    box.querySelector('[data-f-months]').onchange = e => { flt.lastMonths = Number(e.target.value); remember(); renderReport(); };
    box.querySelector('[data-f-goods-search]').oninput = e => { box.querySelector('[data-f-goods]').innerHTML = renderGoods(report.goods, e.target.value.trim()); };
  }

  async function loadReport() {
    const box = root.querySelector('[data-f-report]');
    box.innerHTML = '<p class="muted">Загрузка отчёта…</p>';
    try {
      report = await api('/api/finance/report?cabinet=' + encodeURIComponent(flt.cabinet));
      if (report.selected !== flt.cabinet) { flt.cabinet = report.selected || ''; remember(); }   // кабинета больше нет в отчёте
      renderReport();
    }
    catch (e) { report = null; box.innerHTML = `<div class="f-alert">${esc(e.message)}</div>`; }
  }

  function showRun(st) {
    const out = root.querySelector('[data-f-run]');
    const running = st.status === 'running';
    root.querySelectorAll('[data-f-update]').forEach(b => b.disabled = running);
    if (st.status === 'idle') { out.hidden = true; return; }
    out.hidden = false;
    const mpTitles = {wb: ' WB', ym: ' Яндекс Маркета', ozon: ' Ozon'};
    const how = {full: ' с догрузкой новых данных маркетплейсов', reload: ' с полной перезагрузкой данных маркетплейсов'};
    const scope = (st.marketplace || st.only) ? (mpTitles[st.marketplace || st.only] || ` ${st.marketplace || st.only}`) : '';
    const title = running ? `Собираю отчёт${how[st.mode] || ' из кэша'}…`
      : st.status === 'done' ? 'Отчёт собран.'
      : st.status === 'interrupted' ? `Сборка прервана${st.code != null ? ` (код ${st.code})` : ''}.`
      : `Сборка завершилась с ошибкой (код ${st.code}).`;
    out.innerHTML = `<b>${esc(title.replace('маркетплейсов', 'маркетплейса' + scope))}</b><pre>${esc((st.log || []).slice(-15).join('\n'))}</pre>`;
  }

  async function poll() {
    clearTimeout(polling);
    try {
      const st = await api('/api/finance/update');
      showRun(st);
      if (st.status === 'running') { polling = setTimeout(poll, 2000); return; }
      if ((st.status === 'done' || st.status === 'interrupted') && root.dataset.waiting) { delete root.dataset.waiting; loadReport(); }
    } catch (e) { polling = setTimeout(poll, 5000); }
  }

  async function startUpdate(mode, only = '', cabinet = '') {
    const labels = {wb: 'Wildberries', ym: 'Яндекс Маркета', ozon: 'Ozon'};
    if (mode === 'full' && only && !confirm(`Обновить данные ${labels[only] || only} через API? Для Ozon отключите VPN.`)) return;
    if (mode === 'full' && !only && !confirm('Обновить данные всех маркетплейсов через API? Для Ozon отключите VPN. Wildberries догрузит только новые недели к уже скачанным; Ozon и Яндекс Маркет загружаются целиком — это может занять до часа.')) return;
    if (mode === 'reload' && !confirm('Загрузить все данные Wildberries заново за весь период? Нужно, только если в кэше что-то не так: при ~200 тыс. строк это несколько минут с паузами из-за лимита WB.')) return;
    try {
      const body = {mode};
      if (only) body.marketplace = only;
      if (cabinet) body.cabinet = cabinet;
      const st = await api('/api/finance/update', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      root.dataset.waiting = '1'; showRun(st); poll();
    }
    catch (e) { root.querySelector('[data-f-run]').hidden = false; root.querySelector('[data-f-run]').textContent = e.message; }
  }

  async function loadSettings() {
    const form = root.querySelector('[data-f-settings]');
    try {
      const s = await api('/api/finance/settings');
      form.dir.value = s.dir; form.python.value = s.python;
      form.querySelector('[data-f-resolved]').textContent = s.resolved_dir ? 'Используется папка: ' + s.resolved_dir : 'Папка opiu с расчётом не найдена.';
      if (!s.resolved_dir) form.closest('details').open = true;
    } catch (e) {}
    form.onsubmit = async ev => {
      ev.preventDefault();
      try { await api('/api/finance/settings', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({dir: form.dir.value, python: form.python.value})}); toastMsg('Настройки сохранены'); loadSettings(); loadReport(); }
      catch (e) { toastMsg(e.message); }
    };
  }
  const toastMsg = text => (typeof window.toast === 'function' ? window.toast(text) : alert(text));

  function render(section) {
    root = section;
    root.className = 'panel';
    root.innerHTML = `<div class="r-head"><h2>ОПиУ маркетплейсов</h2><div class="toolbar">
        <button class="btn" data-f-update="cache" title="Из кэша — 1–2 минуты; кабинеты, которых нет в кэше, загрузятся из API">Собрать из кэша</button>
        <button class="btn secondary" data-f-update="full" title="Загрузить из API только новые данные: закрытые недели WB берутся из кэша">Обновить данные МП</button>
        <button class="btn secondary" data-f-update="full" data-f-only="wb" title="Загрузить данные Wildberries через API">WB</button>
        <button class="btn secondary" data-f-update="full" data-f-only="ym" title="Загрузить данные Яндекс Маркета через API">ЯМ</button>
        <button class="btn secondary" data-f-update="full" data-f-only="ozon" title="Загрузить данные Ozon через API">Ozon</button>
        <button class="btn secondary" data-f-update="reload" title="Загрузить все недели WB заново за весь период — если в кэше что-то не так">Полная перезагрузка</button>
        <a class="btn secondary" href="/api/finance/report/download">⇩ Excel</a></div></div>
      <p class="muted">Расчёт ОПиУ — в папке <b>WB OPS\\opiu</b> (opiu_all.py); здесь — последний собранный отчёт. Ключи API берутся из кабинетов панели. Налог считается от фактической цены покупки.</p>
      <div data-f-run class="f-run" role="status" aria-live="polite" hidden></div>
      <details class="f-settings"><summary>Настройки источника</summary><form data-f-settings class="r-form">
        <label>Папка проекта ОПиУ<input name="dir" placeholder="Пусто — папка opiu внутри WB OPS"></label>
        <label>Python для запуска<input name="python" placeholder="Пусто — Python из WB OPS"></label>
        <small class="muted" data-f-resolved></small><button class="btn secondary" type="submit">Сохранить</button></form></details>
      <div data-f-report></div>`;
    root.querySelectorAll('[data-f-update]').forEach(b => b.onclick = () => startUpdate(b.dataset.fUpdate, b.dataset.fOnly || '', root.dataset.financeCabinet || ''));
    loadSettings(); loadReport(); poll();
  }

  const style = document.createElement('style');
  style.textContent = `#finances .r-head{flex-wrap:wrap}#finances .r-head .toolbar{flex-wrap:wrap}@media(max-width:600px){#finances .r-head .btn{white-space:nowrap}}.f-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:16px 0}.f-kpi{border:1px solid #e5e7eb;border-radius:12px;padding:14px}.f-kpi small{display:block;color:#6b7280;min-height:30px}.f-kpi strong{display:block;font-size:22px;color:#111827;margin:4px 0}.f-kpi-main{border-color:#8b5cf6;background:#faf5ff}.f-kpi-main strong{color:#6d28d9}
.f-neg,.f-kpi strong.f-neg{color:#b91c1c!important}.f-kpi-warn{border-color:#f59e0b;background:#fffbeb}.f-kpi-warn strong{color:#b45309}.f-cab-status{display:flex;flex-wrap:wrap;gap:7px;align-items:center;padding:10px 12px;border:1px solid #e5e7eb;border-radius:10px;background:#f9fafb;margin:10px 0}.f-status{padding:4px 8px;border-radius:999px;font-size:12px}.f-status-ok{background:#dcfce7;color:#166534}.f-status-cache{background:#fef3c7;color:#92400e}.f-status-bad{background:#fee2e2;color:#991b1b}.f-scroll{overflow-x:auto;border:1px solid #eef0f5;border-radius:10px;margin:8px 0 20px}
.f-table{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}.f-table th,.f-table td{padding:7px 10px;text-align:right;border-bottom:1px solid #f1f2f6}.f-table th{background:#f9fafb;font-weight:700;position:sticky;top:0}.f-table td:first-child,.f-table th:first-child{text-align:left}
.f-sticky{position:sticky;left:0;background:#fff;z-index:1;max-width:340px;white-space:normal}.f-table th.f-sticky{background:#f9fafb}.f-section td{background:#f3f0ff;font-weight:700;color:#5b21b6;text-align:left}.f-section .f-sticky{background:#f3f0ff}
.f-total td{font-weight:700;background:#fffbeb}.f-total .f-sticky{background:#fffbeb}.f-profit td{background:#ecfdf5;font-weight:700}.f-profit .f-sticky{background:#ecfdf5}.f-detail td{color:#6b7280;font-size:12px}.f-detail .f-sticky{padding-left:26px}.f-share td{color:#4b5563}.f-fc{background:#fdf4ff}
.f-control td:nth-child(3){text-align:left;white-space:normal}.f-control td{text-align:left}.f-problem td{background:#fff7ed;color:#9a3412}
.f-alert{padding:12px 14px;border-radius:10px;background:#fff7ed;color:#9a3412;margin:10px 0}.f-alert ul{margin:6px 0 0 18px;padding:0}.f-ok{padding:10px 14px;border-radius:10px;background:#ecfdf5;color:#065f46;margin:10px 0}
.f-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}.f-head h3{margin:12px 0 4px}.f-head input{padding:8px 10px;border:1px solid #ddd6fe;border-radius:10px}
.f-run{padding:12px;border:1px solid #ddd6fe;border-radius:10px;background:#faf5ff;margin:10px 0}.f-run pre{max-height:220px;overflow:auto;font-size:12px;white-space:pre-wrap;margin:8px 0 0}.f-settings{margin:8px 0}.f-settings summary{cursor:pointer;color:#6d28d9}.r-form input:not([type]){padding:9px;border:1px solid #ddd6fe;border-radius:10px}`;
  document.head.appendChild(style);
  return {render};
})();
