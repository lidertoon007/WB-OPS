/* WB OPS: actual unit economics by marketplace, cabinet and article. */
window.WBOPSUnitEconomics = (() => {
  const base = '/api/unit-economics';
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2});
  const percent = new Intl.NumberFormat('ru-RU', {style: 'percent', maximumFractionDigits: 2});
  const dateTime = new Intl.DateTimeFormat('ru-RU', {dateStyle: 'medium', timeStyle: 'short'});
  const monthName = new Intl.DateTimeFormat('ru-RU', {month: 'long', year: 'numeric', timeZone: 'UTC'});
  const groups = {
    finance: 'Финансы', per_unit: 'На 1 шт', prices: 'Цены', funnel: 'Воронка',
    inventory: 'Заказы и остатки', all: 'Все колонки'
  };
  const filters = {
    marketplace: ['Маркетплейс', 'Все маркетплейсы'], cabinet: ['Кабинет', 'Все кабинеты'],
    brand: ['Бренд', 'Все бренды'], category: ['Категория', 'Все категории']
  };
  const bounds = {date_from: ['period-from', 'С начала отчёта'], date_to: ['period-to', 'До конца отчёта']};
  const priority = ['Артикул', 'Название', 'Маркетплейс', 'Кабинет',
    'Выручка нетто, ₽', 'Прибыль, ₽', 'Маржа, % выручки', 'Нетто, шт',
    'Расходы МП итого, ₽', 'Себестоимость', 'Налог'];
  const filterOptions = (data, key) => data.filters[key].map(option =>
    typeof option === 'string' ? {value: option, label: option} : option);

  async function api(path, init) {
    const response = await fetch(base + path, init);
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Не удалось выполнить запрос.');
    return data;
  }

  function format(value, column) {
    if (value == null || value === '') return '—';
    if (column.format === 'percent') return percent.format(value);
    return column.format === 'number' ? number.format(value) : esc(value);
  }

  function table(data, group) {
    if (!data.rows.length) return '<p class="ue-empty muted">По выбранным фильтрам нет строк.</p>';
    const indices = data.columns.map((col, i) => ({...col, i}));
    const selected = indices.filter(col => group === 'all' || col.group === group || col.group === 'identity');
    if (group !== 'all' && !selected.some(col => col.group === group))
      return '<p class="ue-empty muted">В этом файле нет показателей для выбранной группы.</p>';
    const leading = priority.map(key => selected.find(col => col.key === key)).filter(Boolean);
    const columns = [...leading, ...selected.filter(col => !priority.includes(col.key))];
    const heading = columns.map(col => `<th scope="col" class="${col.format}">${esc(col.key)}
      ${col.snapshot ? '<small>Снимок</small>' : ''}</th>`).join('');
    const cells = (row, total = false) => columns.map((col, i) => {
      if (total && i === 0) return '<th scope="row">Итого</th>';
      const value = row[col.i];
      const negative = typeof value === 'number' && value < 0 ? ' ue-negative' : '';
      const content = total && col.format === 'text' ? '' : format(value, col);
      return `<td class="${col.format}${negative}">${content}</td>`;
    }).join('');
    return `<div class="ue-scroll" role="region" aria-label="Таблица фактической юнит-экономики" tabindex="0">
      <table class="ue-table"><thead><tr>${heading}</tr></thead>
      <tbody><tr class="ue-total">${cells(data.totals, true)}</tr>
      ${data.rows.map(row => `<tr>${cells(row)}</tr>`).join('')}</tbody></table></div>`;
  }

  function render(root) {
    root.innerHTML = `<div class="ue-head">
      <div class="ue-actions">
        <button type="button" class="btn" data-ue-build disabled>Собрать из кэша</button>
        <a class="btn secondary" data-ue-excel href="${base}/report/download" hidden>Скачать Excel</a>
      </div>
      <p class="muted" data-ue-meta></p>
    </div>
    <div class="ue-run" data-ue-run role="status" aria-live="polite" hidden></div>
    <p class="muted" data-ue-notice role="status">Загружаю отчёт…</p>
    <div data-ue-content hidden>
      <div class="ue-period" data-ue-period hidden>
        <label>Период с<select data-ue-period-from></select></label>
        <label>по<select data-ue-period-to></select></label>
        <button type="button" class="btn secondary" data-ue-period-all>Весь период</button>
      </div>
      <p class="ue-note muted" data-ue-period-note hidden></p>
      <div class="ue-filters">
        ${Object.entries(filters).map(([key, [label]]) => `<label>${label}<select data-ue-filter="${key}"></select></label>`).join('')}
        <label class="ue-search">Поиск<input type="search" data-ue-search maxlength="160" placeholder="Артикул или название"></label>
      </div>
      <div class="ue-groups" role="group" aria-label="Группы колонок">
        ${Object.entries(groups).map(([key, label]) =>
          `<button type="button" data-ue-group="${key}" aria-pressed="${key === 'finance'}">${label}</button>`).join('')}
      </div>
      <p class="ue-note muted" data-ue-snapshot hidden></p>
      <div data-ue-table></div>
      <div class="ue-pagination">
        <span data-ue-count class="muted" aria-live="polite"></span>
        <button type="button" class="btn secondary" data-ue-prev aria-label="Предыдущая страница">Назад</button>
        <button type="button" class="btn secondary" data-ue-next aria-label="Следующая страница">Вперёд</button>
      </div>
    </div>`;

    const node = name => root.querySelector(`[data-ue-${name}]`);
    const active = {marketplace: '', cabinet: '', brand: '', category: '', q: '', page: 1, date_from: '', date_to: ''};
    const build = node('build'), prev = node('prev'), next = node('next');
    const fields = [...root.querySelectorAll('[data-ue-filter]')];
    const tabs = [...root.querySelectorAll('[data-ue-group]')];
    let report, group = 'finance', requestId = 0, searchTimer, waitingForUnit = false, jobRunning = false;

    function paintTable() {
      if (!report?.file) return;
      node('table').innerHTML = table(report, group);
      const note = node('snapshot');
      note.hidden = !report.columns.some(col => col.snapshot && (col.group === group || group === 'all'));
      note.textContent = 'Цены и остатки взяты на дату загрузки данных.'
        + (report.period_filtered && (group === 'inventory' || group === 'all')
          ? ' Заказы в день рассчитаны по календарным дням выбранного периода до даты сборки. Запас в днях учитывает этот темп.'
          : '');
    }

    function paintPeriod() {
      node('period').hidden = !report.period_filter;
      const notice = node('period-note');
      notice.hidden = !report.period_filter_notice;
      notice.textContent = report.period_filter_notice || '';
      if (!report.period_filter) return;
      const months = report.available_periods.map(month =>
        `<option value="${esc(month)}">${monthName.format(new Date(`${month}-01T00:00:00Z`))}</option>`).join('');
      for (const [key, [name, label]] of Object.entries(bounds)) {
        const select = node(name);
        select.innerHTML = `<option value="">${label}</option>${months}`;
        select.value = report.active_period[key] || '';
      }
      node('period-all').disabled = !active.date_from && !active.date_to;
    }

    function paintReport() {
      const hasFile = Boolean(report.file);
      node('excel').hidden = !hasFile;
      node('content').hidden = !hasFile;
      node('notice').hidden = hasFile;
      node('meta').textContent = hasFile
        ? `Период ${report.period} · Собран ${dateTime.format(new Date(report.updated_at))} · ${report.file}` : '';
      if (!hasFile) {
        node('notice').textContent = 'Отчёт ещё не собран. Нажмите «Собрать из кэша».';
        return;
      }
      paintPeriod();
      for (const select of fields) {
        const key = select.dataset.ueFilter;
        const options = [{value: '', label: filters[key][1]}, ...filterOptions(report, key)];
        select.innerHTML = options.map(({value, label}) => `<option value="${esc(value)}">${esc(label)}</option>`).join('');
        select.value = active[key];
      }
      paintTable();
      const first = (report.page - 1) * report.page_size + 1;
      const last = Math.min(report.page * report.page_size, report.total_rows);
      node('count').textContent = report.total_rows ? `Строки ${number.format(first)}–${number.format(last)} из ${number.format(report.total_rows)}` : '0 строк';
      prev.disabled = report.page <= 1;
      next.disabled = last >= report.total_rows;
    }

    async function loadReport(canResetFilters = true) {
      clearTimeout(searchTimer);
      const id = ++requestId;
      const params = new URLSearchParams(active);
      for (const key of Object.keys(bounds)) {
        if (!active[key]) params.delete(key);
      }
      try {
        const data = await api('/report?' + params);
        if (id !== requestId) return;
        if (canResetFilters && data.file) {
          const unavailable = Object.keys(filters).filter(key =>
            active[key] && !filterOptions(data, key).some(option => option.value === active[key]));
          unavailable.push(...Object.keys(bounds).filter(key =>
            active[key] && !data.available_periods?.includes(active[key])));
          if (unavailable.length) {
            unavailable.forEach(key => { active[key] = ''; });
            active.page = 1;
            return loadReport(false);
          }
        }
        report = data;
        paintReport();
      } catch (error) {
        if (id !== requestId) return;
        node('excel').hidden = !report?.file;
        node('content').hidden = !report?.file;
        node('notice').hidden = false;
        node('notice').textContent = error.message;
      }
    }

    function showJob(state) {
      const running = state.status === 'running';
      const unit = state.mode === 'unit';
      jobRunning = running;
      build.disabled = running;
      const outcome = {running: 'Собираю фактическую юнит-экономику из кэша…',
        done: 'Юнит-экономика собрана.', error: 'Не удалось собрать юнит-экономику.',
        interrupted: 'Сборка юнит-экономики прервана.'};
      const message = unit ? state.message || outcome[state.status] : running ? 'Собирается ОПиУ. Дождитесь окончания.' : '';
      const job = node('run');
      job.hidden = !message;
      const logs = unit && (running || state.status === 'error') ? (state.log || []).slice(-6) : [];
      job.innerHTML = message ? `<strong>${esc(message)}</strong>${logs.length ? `<pre>${esc(logs.join('\n'))}</pre>` : ''}` : '';
      if (unit && running) waitingForUnit = true;
      if (running) setTimeout(poll, 2000);
      else if (waitingForUnit) {
        waitingForUnit = false;
        loadReport();
      }
    }

    async function poll() {
      try { showJob(await api('/update')); }
      catch (error) {
        if (jobRunning) setTimeout(poll, 5000);
        else build.disabled = false;
      }
    }

    build.onclick = async () => {
      build.disabled = true;
      try {
        waitingForUnit = true;
        showJob(await api('/update', {method: 'POST'}));
      } catch (error) {
        waitingForUnit = false;
        build.disabled = false;
        const job = node('run');
        job.hidden = false;
        job.textContent = error.message;
      }
    };
    for (const field of fields) {
      field.onchange = () => {
        const key = field.dataset.ueFilter;
        active[key] = field.value;
        if (key === 'marketplace') {
          active.cabinet = '';
          fields.find(select => select.dataset.ueFilter === 'cabinet').value = '';
        }
        active.page = 1;
        loadReport();
      };
    }
    for (const [key, [name]] of Object.entries(bounds)) {
      const select = node(name);
      select.onchange = () => {
        active[key] = select.value;
        if (active.date_from && active.date_to && active.date_from > active.date_to) {
          const other = key === 'date_from' ? 'date_to' : 'date_from';
          active[other] = select.value;
          node(bounds[other][0]).value = select.value;
        }
        active.page = 1;
        loadReport();
      };
    }
    node('period-all').onclick = () => {
      active.date_from = active.date_to = '';
      active.page = 1;
      loadReport();
    };
    node('search').oninput = event => {
      active.q = event.target.value;
      active.page = 1;
      requestId += 1;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(loadReport, 220);
    };
    for (const tab of tabs) {
      tab.onclick = () => {
        group = tab.dataset.ueGroup;
        tabs.forEach(button => button.setAttribute('aria-pressed', String(button === tab)));
        paintTable();
      };
    }
    const turnPage = step => {
      active.page = report?.page + step || 1;
      loadReport();
    };
    prev.onclick = () => turnPage(-1);
    next.onclick = () => turnPage(1);
    loadReport();
    poll();
  }

  const style = document.createElement('style');
  style.textContent = `
.wb-member [data-nav=unit-economics]{display:none!important}
#unitEconomics{min-width:0}
.ue-head,.ue-actions,.ue-groups,.ue-pagination,.ue-period{display:flex;align-items:center;flex-wrap:wrap;gap:10px}
.ue-period{align-items:end;margin-bottom:14px}
.ue-period[hidden]{display:none}
.ue-head{justify-content:space-between;margin-bottom:16px}
.ue-head p{margin:0;overflow-wrap:anywhere;font-size:13px}
.ue-actions a{text-decoration:none}
.ue-filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,145px),1fr));gap:10px;margin:0 0 14px}
.ue-filters label,.ue-period label{display:grid;gap:5px;min-width:0;font-size:13px;font-weight:700}
.ue-filters .ue-search{grid-column:span 2}
.ue-filters input,.ue-filters select,.ue-period select{width:100%;min-width:0;border:1px solid #dfe1ed;border-radius:8px;padding:9px 10px;background:#fff;color:#1f2937;font:inherit;font-weight:400}
.ue-groups{gap:6px;margin-bottom:12px}
.ue-groups button{border:1px solid #dfe1ed;border-radius:8px;padding:7px 11px;background:#fff;color:#4b5563;font:inherit;font-size:13px;cursor:pointer}
.ue-groups button[aria-pressed=true]{border-color:#c4b5fd;background:#f3e8ff;color:#5b21b6;font-weight:700}
.ue-scroll{overflow:auto;max-height:65vh;border:1px solid #e5e7eb;border-radius:10px}
.ue-table{border-collapse:separate;border-spacing:0;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
.ue-table :is(th,td){text-align:right;padding:8px 10px;border-bottom:1px solid #eef0f5;white-space:nowrap}
.ue-table .text,.ue-table :is(th,td):first-child{text-align:left}
.ue-table td.text:not(:first-child){max-width:250px;white-space:normal;overflow-wrap:anywhere}
.ue-table thead th{position:sticky;top:0;z-index:2;background:#f8f9fd;min-width:105px;white-space:normal}
.ue-table thead small{display:block;color:#6b7280;font-size:11px;font-weight:400;margin-top:2px}
.ue-table tr>:first-child{position:sticky;left:0;z-index:1;background:#fff;max-width:160px;white-space:normal;overflow-wrap:anywhere;box-shadow:1px 0 #e5e7eb}
.ue-table thead tr>:first-child{z-index:3;background:#f8f9fd}
.ue-table .ue-total>*{background:#f3e8ff;font-weight:700}
.ue-table .ue-negative{color:#b91c1c}
.ue-pagination{margin-top:12px;font-size:13px}
.ue-pagination span{margin-right:auto}
.ue-run{padding:10px 12px;border:1px solid #ddd6fe;border-radius:10px;background:#faf5ff;margin:0 0 12px}
.ue-run pre{max-height:120px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;margin-bottom:0;font-size:12px}
.ue-empty{margin:20px 0}
.ue-note{font-size:13px;margin:0 0 12px}
@media(max-width:600px){.ue-filters .ue-search{grid-column:1/-1}}
`;
  document.head.appendChild(style);
  return {render};
})();
