/* WB OPS: единая таблица установки первых цен Wildberries. */
window.WBOPSRepricer = (() => {
  const STORAGE = 'wbops.prices.table.v1';
  const XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
  const TEMPLATE_BASE64 = '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';
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const key = v => String(v ?? '').replace(/\s+/g, '').toLocaleLowerCase('ru-RU');
  const money = v => Number(v).toLocaleString('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' ₽';
  const number = v => Number(String(v ?? '').replace(/\s+/g, '').replace(',', '.'));
  const uid = () => 'price-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 9);
  const st = {catalog: [], rows: [], job: null, timer: null, checking: false, importing: false};
  let root;
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE) || 'null');
    if (Array.isArray(saved?.rows)) st.rows = saved.rows;
    if (saved?.job?.id && Array.isArray(saved.job.entries)) st.job = saved.job;
    if (!st.job) st.rows.forEach(row => { if (row.state === 'checking' || row.state === 'running') { row.state = 'ready'; row.status = 'Готов к запуску'; } });
  } catch (_) { /* Таблица работает и без сохранения. */ }
  const save = () => {
    try { localStorage.setItem(STORAGE, JSON.stringify({rows: st.rows, job: st.job})); } catch (_) { /* Без сохранения. */ }
  };
  const api = async (url, opt = {}) => {
    const form = opt.body instanceof FormData;
    const response = await fetch(url, {method: opt.method || 'GET',
      headers: opt.body && !form ? {'Content-Type': 'application/json'} : {},
      body: form ? opt.body : opt.body ? JSON.stringify(opt.body) : undefined});
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const error = new Error(data.detail === 'Not Found' ? 'Backend ещё не перезапущен. Обновите сервис и повторите.' : data.detail || 'Не удалось выполнить запрос');
      error.status = response.status;
      throw error;
    }
    return opt.raw ? response : response.json();
  };
  const label = item => `${item.vendor || item.nm_id} — ${item.cabinet} [${item.nm_id}]`;
  function selected(value, id = '') {
    const same = st.catalog.find(item => item.id === id && [key(item.vendor), key(item.nm_id), key(label(item))].includes(key(value)));
    if (same) return same;
    const matches = st.catalog.filter(item => [key(item.vendor), key(item.nm_id), key(label(item))].includes(key(value)));
    return matches.length === 1 ? matches[0] : null;
  }
  const empty = row => ['article', 'target_base', 'target_price'].every(field => row[field] == null || row[field] === '');
  const locked = row => st.checking || (st.job?.entries || []).some(entry => entry.uid === row.uid);
  function validation(row) {
    if (!row.article?.trim()) return 'Укажите артикул';
    const item = selected(row.article, row.id);
    if (!item && (!row.id || st.catalog.length)) return 'Артикул не найден или неоднозначен — выберите товар из списка';
    if (item) row.id = item.id;
    const values = [row.target_base, row.target_price].map(number);
    if (values.some(value => !Number.isFinite(value) || value <= 0)) return 'Заполните две целевые цены положительными числами';
    if (values[0] < 1 || values[0] < values[1]) return 'Цена до скидки должна быть не меньше цены после скидки';
    return '';
  }
  function status(row, state, text) { row.state = state; row.status = text; }
  function note(text, bad = false) {
    const host = root?.querySelector('[data-note]');
    if (host) { host.textContent = text; host.className = 'pc-note' + (bad ? ' pc-error' : ''); }
  }
  function buttons() {
    if (!root) return;
    const count = st.rows.filter(row => row.state !== 'done' && !empty(row) && !validation(row)).length;
    const start = root.querySelector('[data-start]');
    start.disabled = !!st.job || st.checking || st.importing || !count;
    start.textContent = count ? `Запустить все готовые (${count})` : 'Запустить все готовые';
    root.querySelector('[data-import]').disabled = !!st.job || st.checking || st.importing;
    root.querySelector('[data-add]').disabled = st.checking || st.importing;
    root.querySelector('[data-clear-done]').disabled = !!st.job || st.checking || st.importing;
    root.querySelector('[data-clear-all]').disabled = !!st.job || st.checking || st.importing;
  }
  function drawTable() {
    const body = root?.querySelector('[data-table-body]');
    if (!body) return;
    body.innerHTML = st.rows.map(row => {
      const disabled = locked(row) ? 'disabled' : '';
      const input = (field, title, article = false) => `<input data-field="${field}" name="${field}" value="${esc(row[field])}" ${field === 'expected_current' ? 'readonly aria-readonly="true" style="background:#f8fafc;color:#64748b" placeholder="Из WB"' : article ? 'list="pcArticles" autocomplete="off" placeholder="Артикул или WB ID"' : 'inputmode="decimal" placeholder="0"'} aria-label="${title}" ${disabled}>`;
      return `<tr data-row="${esc(row.uid)}"><td>${input('article', 'Артикул', true)}</td><td>${input('expected_current', 'Цена сейчас')}</td>
        <td>${input('target_base', 'Цена до скидки')}</td><td>${input('target_price', 'Цена после скидки')}</td>
        <td><span data-status class="pc-status ${row.state === 'error' ? 'pc-error' : row.state === 'done' ? 'pc-done' : ''}">${esc(row.status || 'Заполните строку')}</span></td>
        <td><button type="button" class="pc-remove" data-remove aria-label="Удалить строку" ${disabled}>×</button></td></tr>`;
    }).join('');
    buttons();
  }
  function drawCatalog() {
    const list = root?.querySelector('[data-articles]');
    if (list) list.innerHTML = st.catalog.map(item => `<option value="${esc(label(item))}"></option>`).join('');
  }
  async function loadCatalog(fresh = false) {
    const data = await api('/api/repricer' + (fresh ? '?fresh=true' : ''));
    st.catalog = data.rows || [];
    st.rows.forEach(row => {
      const item = selected(row.article, row.id);
      if (item) row.id = item.id;
      row.expected_current = item?.price_before_spp ?? '';
    });
    drawCatalog();
    save(); drawTable();
    return data;
  }
  function addRow(source = {}) {
    const row = {uid: uid(), id: '', article: '', expected_current: '', target_base: '', target_price: '', state: 'empty', status: 'Заполните строку', ...source};
    st.rows.push(row);
    return row;
  }
  async function render(section) {
    root = section;
    root.className = 'panel pc-page';
    if (!st.rows.length) addRow();
    root.innerHTML = `<h2>Цены</h2><p class="muted">Добавьте товары вручную или из Excel, проверьте цены и запустите изменение.</p>
      <div class="pc-actions"><button type="button" class="btn secondary" data-template>Скачать шаблон Excel</button>
        <label class="btn secondary pc-upload">Загрузить Excel<input type="file" accept=".xlsx,.csv" data-import hidden></label>
        <button type="button" class="btn secondary" data-add>Добавить строку</button>
        <button type="button" class="btn secondary" data-clear-done>Очистить готовые</button>
        <button type="button" class="btn secondary" data-clear-all>Очистить всё</button>
        <button type="button" class="btn" data-start>Запустить все готовые</button></div>
      <datalist id="pcArticles" data-articles></datalist><div class="pc-scroll"><table class="pc-table">
        <thead><tr><th>Артикул</th><th>Цена сейчас</th><th>Цена до скидки</th><th>Цена после скидки</th><th>Статус</th><th></th></tr></thead>
        <tbody data-table-body></tbody></table></div><div class="pc-note" data-note role="status" aria-live="polite"></div>
      <p class="muted pc-help">Цена сейчас загружается автоматически из WB. В Excel эту колонку заполнять не нужно. Перед запуском сервис обновляет данные.</p>
      <style>.pc-page h2{margin-top:0}.pc-actions{display:flex;gap:10px;flex-wrap:wrap;margin:20px 0}.pc-upload{cursor:pointer}.pc-scroll{overflow-x:auto;border:1px solid #e5e7eb;border-radius:10px}.pc-table{width:100%;border-collapse:collapse;min-width:880px}.pc-table th{text-align:left;background:#f8fafc;font-size:12px;padding:12px;white-space:nowrap}.pc-table td{padding:10px;border-top:1px solid #e5e7eb;vertical-align:top}.pc-table th:first-child{width:28%}.pc-table th:nth-child(5){width:24%}.pc-table input{width:100%;min-width:100px;box-sizing:border-box;padding:10px;border:1px solid #d1d5db;border-radius:7px;font:inherit;font-size:13px}.pc-table td:first-child input{min-width:210px}.pc-table input:disabled{background:#f3f4f6;color:#6b7280}.pc-status{display:block;font-size:12px;line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere;padding:6px 0}.pc-error{color:#b91c1c}.pc-done{color:#15803d}.pc-remove{border:0;background:none;color:#6b7280;cursor:pointer;font-size:23px;padding:4px}.pc-note{white-space:pre-wrap;line-height:1.5;margin-top:16px}.pc-help{font-size:12px;margin-top:18px}</style>`;
    root.querySelector('[data-add]').onclick = () => { addRow(); save(); drawTable(); root.querySelector('tr:last-child [data-field=article]')?.focus(); };
    root.querySelector('[data-clear-done]').onclick = () => clearRows(false);
    root.querySelector('[data-clear-all]').onclick = () => clearRows(true);
    root.querySelector('[data-template]').onclick = downloadTemplate;
    root.querySelector('[data-import]').onchange = importFile;
    root.querySelector('[data-start]').onclick = startAll;
    const body = root.querySelector('[data-table-body]');
    body.oninput = event => {
      const field = event.target.dataset.field, tr = event.target.closest('[data-row]');
      const row = st.rows.find(item => item.uid === tr?.dataset.row);
      if (!row || !field || field === 'expected_current' || locked(row)) return;
      row[field] = event.target.value;
      if (field === 'article') {
        const item = selected(row.article);
        row.id = item?.id || '';
        if (item) {
          row.expected_current = item.price_before_spp ?? '';
          tr.querySelector('[data-field=expected_current]').value = row.expected_current;
        }
      }
      const error = validation(row);
      status(row, error ? 'empty' : 'ready', error || 'Готов к запуску');
      const host = tr.querySelector('[data-status]');
      host.textContent = row.status; host.className = 'pc-status';
      save(); buttons();
    };
    body.onclick = event => {
      if (!event.target.closest('[data-remove]')) return;
      const row = st.rows.find(item => item.uid === event.target.closest('[data-row]')?.dataset.row);
      if (!row || locked(row)) return;
      st.rows = st.rows.filter(item => item !== row); save(); drawTable();
    };
    drawTable();
    try {
      const data = await loadCatalog(true);
      if (!data.cabinets?.length) note('Добавьте кабинет Wildberries с доступом API к ценам.', true);
      buttons();
    } catch (error) { note(error.message, true); }
    if (st.job) { note('Изменение цен выполняется.'); poll(); }
  }
  function clearRows(all) {
    if (st.job || st.checking || st.importing) return;
    st.rows = all ? [] : st.rows.filter(row => row.state !== 'done' && row.state !== 'success');
    if (!st.rows.length) addRow();
    save(); drawTable(); note(all ? 'Таблица очищена.' : 'Готовые строки удалены.');
  }
  async function downloadTemplate(event) {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      let bytes, unavailable = false;
      for (const path of ['/api/repricer/template.xlsx', '/api/repricer/template']) {
        try {
          const response = await api(path, {raw: true});
          if (!(response.headers.get('Content-Type') || '').includes(XLSX)) throw new Error('Сервер не вернул файл Excel');
          bytes = new Uint8Array(await response.arrayBuffer()); break;
        } catch (error) { if (error.status !== 404) throw error; unavailable = true; }
      }
      if (!bytes && unavailable) {
        bytes = Uint8Array.from(atob(TEMPLATE_BASE64), char => char.charCodeAt(0));
        note('Backend ещё не перезапущен. Скачан встроенный шаблон Excel; загрузка файла станет доступна после обновления сервиса.');
      }
      if (!bytes || bytes[0] !== 80 || bytes[1] !== 75) throw new Error('Получен некорректный файл Excel');
      const url = URL.createObjectURL(new Blob([bytes], {type: XLSX})), link = document.createElement('a');
      link.href = url; link.download = 'shablon_cen_wb.xlsx'; document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { note(error.message, true); }
    finally { button.disabled = false; }
  }
  async function importFile(event) {
    const file = event.target.files?.[0]; event.target.value = '';
    if (!file || st.job || st.checking) return;
    st.importing = true; buttons(); note('Читаем Excel и проверяем товары…');
    const body = new FormData(); body.append('file', file);
    try {
      const result = await api('/api/repricer/import', {method: 'POST', body});
      if (!Array.isArray(result.rows)) throw new Error('Backend ещё не обновлён: файл прочитан, но редактируемые строки не возвращены. Перезапустите сервис.');
      st.rows = st.rows.filter(row => !empty(row));
      for (const source of result.rows) {
        const errors = source.errors || [];
        addRow({...source, state: errors.length ? 'error' : 'ready', status: errors.join('; ') || 'Готов к запуску'});
      }
      save(); drawTable();
      note(`Добавлено строк: ${result.rows.length}. Исправьте ошибки в таблице и запустите все готовые.`);
    } catch (error) { note(error.message, true); }
    finally { st.importing = false; buttons(); }
  }
  function change(row) {
    const target = number(row.target_price);
    const current = number(row.expected_current);
    return {id: row.id, first_price: true, expected_current: Number.isFinite(current) && current > 0 ? current : undefined,
      target_base: number(row.target_base), target_price: target, new_price: target};
  }
  async function startAll() {
    if (st.job || st.checking || st.importing) return;
    const rows = st.rows.filter(row => row.state !== 'done' && !empty(row));
    if (!rows.length) return;
    const counts = new Map();
    rows.forEach(row => {
      const error = validation(row);
      status(row, error ? 'error' : 'ready', error || 'Готов к запуску');
      if (!error) counts.set(row.id, (counts.get(row.id) || 0) + 1);
    });
    rows.forEach(row => { if (counts.get(row.id) > 1) status(row, 'error', 'Товар указан несколько раз — оставьте одну строку'); });
    if (rows.some(row => row.state === 'error')) { save(); drawTable(); note('Пакет не запущен. Исправьте ошибки во всех заполненных строках.', true); return; }
    st.checking = true;
    rows.forEach(row => status(row, 'checking', 'Проверяется')); save(); drawTable(); note('Проверяем актуальные цены WB…');
    try {
      await loadCatalog(true);
      const changes = rows.map(change);
      const result = await api('/api/repricer/preview', {method: 'POST', body: {changes}});
      const checked = new Map((result.items || []).map(item => [item.id, item]));
      for (const row of rows) {
        const item = checked.get(row.id);
        const errors = item?.errors || (item ? [] : ['WB не вернул результат проверки товара']);
        row.current_warning = item?.current_warning || '';
        if (!errors.length && item.already_done) {
          row.expected_current = item.new_price ?? item.old_price ?? row.expected_current;
          status(row, 'done', 'Готово · цена уже установлена ' + money(row.expected_current));
        } else status(row, errors.length ? 'error' : 'ready', errors.join('; ') || 'Готов к запуску');
      }
      save(); drawTable();
      if (rows.some(row => row.state === 'error')) { note('Пакет не запущен. Исправьте ошибки в таблице.', true); return; }
      const pending = rows.filter(row => row.state !== 'done');
      if (!pending.length) { note('Все цены уже установлены. Отправка не требуется.'); return; }
      const warnings = pending.filter(row => row.current_warning);
      const warning = warnings.length ? `У ${warnings.length} товаров контрольная цена отличается от WB. Будет использована актуальная цена WB.\n` + warnings.slice(0, 5).map(row => row.current_warning).join('\n') + '\n\n' : '';
      if (!window.confirm(warning + `Запустить изменение цен для ${pending.length} товаров?`)) { note('Запуск отменён.'); return; }
      const operation = await api('/api/repricer/apply', {method: 'POST', body: {changes: pending.map(change), confirm: true, action: 'Установка первых цен'}});
      if (!operation.id && operation.already_done?.length === pending.length) {
        pending.forEach(row => {
          row.expected_current = operation.prices?.[row.id] ?? row.expected_current;
          status(row, 'done', 'Готово · цена уже установлена ' + money(row.expected_current));
        });
        save(); drawTable(); note('Все цены уже установлены. Отправка не требуется.'); return;
      }
      if (!operation.id) throw new Error('Backend не вернул номер операции; проверьте результат в WB перед повторным запуском');
      st.job = {id: operation.id, entries: pending.map(row => ({uid: row.uid, id: row.id}))};
      pending.forEach(row => status(row, 'running', 'В процессе')); save(); drawTable(); note('Изменение цен выполняется.'); poll();
    } catch (error) {
      rows.filter(row => row.state !== 'done').forEach(row => status(row, 'error', error.message)); save(); drawTable(); note('Пакет не запущен: ' + error.message, true);
    } finally { st.checking = false; drawTable(); }
  }
  async function poll() {
    clearTimeout(st.timer);
    const job = st.job;
    if (!job) return;
    try {
      const operation = await api('/api/repricer/ops/' + job.id);
      if (operation.status === 'running') {
        job.entries.forEach(entry => { const row = st.rows.find(item => item.uid === entry.uid); if (row) status(row, 'running', 'В процессе'); });
        save(); drawTable(); st.timer = setTimeout(poll, 3000); return;
      }
      const legacy = !operation.prices || job.entries.some(entry => operation.prices[entry.id] == null && (!operation.items?.[entry.id] || operation.items[entry.id] === 'ok'));
      if (legacy) await loadCatalog(true);
      for (const entry of job.entries) {
        const row = st.rows.find(item => item.uid === entry.uid);
        if (!row) continue;
        const result = operation.items?.[entry.id];
        if (result && result !== 'ok') status(row, 'error', result);
        else if (operation.status === 'Прервано') status(row, 'error', 'Изменение прервано. Проверьте цену WB перед повторным запуском');
        else {
          const final = operation.prices?.[entry.id] ?? st.catalog.find(item => item.id === entry.id)?.price_before_spp;
          status(row, 'done', final == null ? 'Готово · фактическая цена WB недоступна' : 'Готово · фактическая цена ' + money(final));
          if (final != null) row.expected_current = final;
        }
      }
      st.job = null; save(); drawTable(); note('Обработка завершена. Результат указан в каждой строке.');
    } catch (error) {
      note('Не удалось проверить операцию: ' + error.message + '. Повторяем проверку…', true);
      st.timer = setTimeout(poll, 5000);
    }
  }
  return {render};
})();
