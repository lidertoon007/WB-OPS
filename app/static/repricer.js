/* WB OPS: установка первой цены Wildberries. */
window.WBOPSRepricer = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const money = v => Number(v).toLocaleString('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' ₽';
  const key = v => String(v ?? '').replace(/\s+/g, '').toLocaleLowerCase('ru-RU');
  const st = {rows: [], imported: null, job: null, timer: null};
  let root;
  const api = async (url, opt = {}) => {
    const form = opt.body instanceof FormData;
    const response = await fetch(url, {method: opt.method || 'GET',
      headers: opt.body && !form ? {'Content-Type': 'application/json'} : {},
      body: form ? opt.body : opt.body ? JSON.stringify(opt.body) : undefined});
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || 'Не удалось выполнить запрос');
    }
    return opt.raw ? response : response.json();
  };
  const option = r => `${r.vendor || r.nm_id} — ${r.cabinet} [${r.nm_id}]`;
  function selected(value) {
    const exact = st.rows.find(r => option(r) === value);
    if (exact) return exact;
    const matches = st.rows.filter(r => key(r.vendor) === key(value) || String(r.nm_id) === value.trim());
    return matches.length === 1 ? matches[0] : null;
  }
  function message(where, text, bad = false) {
    const host = root?.querySelector('[data-' + where + '-status]');
    if (host) { host.textContent = text; host.className = bad ? 'pc-status pc-error' : 'pc-status'; }
  }
  function busy() {
    root?.querySelectorAll('[data-start]').forEach(button => { button.disabled = !!st.job || (button.dataset.start === 'excel' && !st.imported?.changes?.length); });
  }
  async function loadRows() {
    const data = await api('/api/repricer');
    st.rows = data.rows || [];
    const list = root.querySelector('[data-articles]');
    list.innerHTML = st.rows.map(r => `<option value="${esc(option(r))}"></option>`).join('');
    if (!data.cabinets?.length) message('manual', 'Добавьте кабинет Wildberries с доступом API к ценам.', true);
  }
  async function render(section) {
    root = section;
    root.className = 'panel pc-page';
    root.innerHTML = `<h2>Цены</h2><p class="muted">Установка первой цены Wildberries. Введите артикул и нужные цены или загрузите Excel.</p>
      <div class="pc-columns"><section class="pc-box"><h3>Установить вручную</h3>
        <form data-manual><div class="pc-fields">
          <label>Артикул<input name="article" list="pcArticles" placeholder="Найти артикул или WB ID" autocomplete="off" required><datalist id="pcArticles" data-articles></datalist></label>
          <label>Цена сейчас<input name="expected_current" type="number" min="0.01" step="0.01" required placeholder="Заполнится после выбора"></label>
          <label>Цена до скидки<input name="target_base" type="number" min="1" step="0.01" required placeholder="15000"></label>
          <label>Цена после скидки<input name="target_price" type="number" min="0.01" step="0.01" required placeholder="9706"></label>
        </div><button class="btn pc-primary" type="submit" data-start="manual">Запустить изменение</button></form>
        <div class="pc-status" data-manual-status role="status" aria-live="polite"></div></section>
      <section class="pc-box"><h3>Через Excel</h3>
        <p class="muted">Четыре колонки: Артикул · Цена сейчас · Цена до скидки · Цена после скидки.</p>
        <div class="pc-actions"><button type="button" class="btn secondary" data-template>Скачать шаблон Excel</button>
          <label class="btn secondary pc-upload">Загрузить Excel<input type="file" accept=".xlsx,.csv" data-import hidden></label></div>
        <p class="muted" data-import-summary>Загрузите заполненный шаблон.</p>
        <button type="button" class="btn pc-primary" data-start="excel" disabled>Запустить 0 цен</button>
        <div class="pc-status" data-excel-status role="status" aria-live="polite"></div></section></div>
      <p class="muted pc-note">Цена сейчас — контрольное значение. Перед запуском сервис проверяет актуальную цену WB. При отказе WB процесс останавливается, а причина появится здесь.</p>
      <style>.pc-page h2{margin-top:0}.pc-columns{display:grid;grid-template-columns:minmax(320px,1fr) minmax(280px,1fr);gap:24px}.pc-box{padding:22px;border:1px solid #e5e7eb;border-radius:14px;background:#fff}.pc-box h3{margin-top:0}.pc-fields{display:grid;grid-template-columns:1fr 1fr;gap:16px}.pc-fields label{display:flex;flex-direction:column;gap:7px;font-size:13px;font-weight:600}.pc-fields input{min-width:0;width:100%;box-sizing:border-box;padding:11px;border:1px solid #d1d5db;border-radius:8px;font:inherit}.pc-fields label:first-child{grid-column:1/-1}.pc-primary{margin-top:20px;min-height:44px;width:100%}.pc-actions{display:flex;gap:10px;flex-wrap:wrap}.pc-upload{cursor:pointer}.pc-status{white-space:pre-wrap;line-height:1.6;margin-top:16px;overflow-wrap:anywhere}.pc-error{color:#b91c1c}.pc-note{margin-top:20px;font-size:12px}@media(max-width:900px){.pc-columns{grid-template-columns:1fr}}@media(max-width:500px){.pc-fields{grid-template-columns:1fr}}</style>`;
    const form = root.querySelector('[data-manual]');
    form.elements.article.oninput = () => {
      const row = selected(form.elements.article.value);
      if (row && form.dataset.selected !== row.id) {
        form.dataset.selected = row.id;
        form.elements.expected_current.value = row.price_before_spp || '';
      } else if (!row) { delete form.dataset.selected; form.elements.expected_current.value = ''; }
    };
    form.onsubmit = async event => {
      event.preventDefault();
      if (st.job) return;
      const row = selected(form.elements.article.value);
      if (!row) { message('manual', 'Выберите однозначный артикул из списка. Для одинаковых артикулов укажите кабинет.', true); return; }
      const change = {id: row.id, first_price: true, expected_current: Number(form.elements.expected_current.value),
        target_base: Number(form.elements.target_base.value), target_price: Number(form.elements.target_price.value)};
      const button = form.querySelector('button');
      button.disabled = true;
      message('manual', 'Проверяем цену WB…');
      try {
        const data = await api('/api/repricer/preview', {method: 'POST', body: {changes: [change]}});
        const item = data.items?.[0];
        if (!item || item.errors?.length) throw new Error(item?.errors?.join('\n') || 'Товар не найден');
        const note = item.current_warning ? item.current_warning + '\n\n' : '';
        if (window.confirm(note + `Запустить изменение цены для ${row.vendor || row.nm_id}?\nЦена после скидки: ${money(item.new_price)}.`)) {
          await start([change], 'manual');
        } else message('manual', 'Запуск отменён.');
      } catch (error) { message('manual', error.message, true); }
      finally { busy(); }
    };
    root.querySelector('[data-template]').onclick = async event => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const response = await api('/api/repricer/template', {raw: true});
        if (!(response.headers.get('Content-Type') || '').includes('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')) throw new Error('Сервер не вернул файл Excel');
        const bytes = new Uint8Array(await response.arrayBuffer());
        if (bytes[0] !== 80 || bytes[1] !== 75) throw new Error('Получен некорректный файл Excel');
        const blob = new Blob([bytes], {type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'});
        const url = URL.createObjectURL(blob), link = document.createElement('a');
        link.href = url; link.download = 'shablon_cen_wb.xlsx'; document.body.append(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (error) { message('excel', error.message, true); }
      finally { button.disabled = false; }
    };
    root.querySelector('[data-import]').onchange = async event => {
      const file = event.target.files?.[0];
      event.target.value = '';
      if (!file || st.job) return;
      st.imported = null; busy(); message('excel', 'Проверяем файл и цены WB…');
      const body = new FormData(); body.append('file', file);
      try {
        const result = await api('/api/repricer/import', {method: 'POST', body});
        st.imported = result;
        const errors = (result.items || []).filter(item => item.errors?.length);
        const warnings = (result.items || []).filter(item => item.current_warning);
        root.querySelector('[data-import-summary]').textContent = `Готово: ${result.changes.length}. Ошибок: ${errors.length}.`;
        root.querySelector('[data-start=excel]').textContent = `Запустить ${result.changes.length} цен`;
        const notes = [];
        if (warnings.length) notes.push(`У ${warnings.length} товаров «Цена сейчас» отличается от WB. Для запуска будет использована актуальная цена WB.`);
        if (errors.length) notes.push(errors.map(item => `${item.vendor || item.nm_id}: ${item.errors.join('; ')}`).join('\n'));
        message('excel', notes.join('\n'), !!errors.length);
      } catch (error) { message('excel', error.message, true); }
      finally { busy(); }
    };
    root.querySelector('[data-start=excel]').onclick = async () => {
      const changes = st.imported?.changes || [];
      if (!changes.length || st.job) return;
      if (!window.confirm(`Запустить изменение ${changes.length} цен? Будут использованы актуальные данные WB и цена из последней колонки файла.`)) return;
      try { await start(changes, 'excel'); } catch (error) { message('excel', error.message, true); busy(); }
    };
    try { await loadRows(); } catch (error) { message('manual', error.message, true); }
    if (!st.job) {
      try { st.job = JSON.parse(localStorage.getItem('wbops.prices.job') || 'null'); } catch (_) { /* Хранилище недоступно. */ }
    }
    busy();
    if (st.job) { message(st.job.where, 'Выполняется изменение цен…'); poll(); }
  }
  async function start(changes, where) {
    const operation = await api('/api/repricer/apply', {method: 'POST', body: {changes, confirm: true, action: 'Установка первой цены'}});
    st.job = {id: operation.id, ids: changes.map(c => c.id), where};
    try { localStorage.setItem('wbops.prices.job', JSON.stringify(st.job)); } catch (_) { /* Без сохранения. */ }
    message(where, 'Выполняется изменение цен…'); busy(); poll();
  }
  async function poll() {
    clearTimeout(st.timer);
    const job = st.job;
    if (!job) return;
    try {
      const operation = await api('/api/repricer/ops/' + job.id);
      if (operation.status === 'running') {
        message(job.where, 'Выполняется изменение цен…');
        st.timer = setTimeout(poll, 3000); return;
      }
      await loadRows();
      const byId = new Map(st.rows.map(row => [row.id, row]));
      const lines = job.ids.map(id => {
        const row = byId.get(id), result = operation.items?.[id];
        const error = operation.status === 'Прервано' ? 'Изменение прервано перезапуском сервиса. Проверьте цену WB' : result && result !== 'ok' ? result : '';
        const price = row?.price_before_spp != null ? `Цена после скидки: ${money(row.price_before_spp)}` : 'Цена WB недоступна';
        return `${row?.vendor || id}: ${error || 'Готово'}. ${price}.`;
      });
      message(job.where, lines.join('\n'), operation.errors > 0 || operation.status === 'Прервано');
      st.job = null;
      try { localStorage.removeItem('wbops.prices.job'); } catch (_) { /* Без сохранения. */ }
      busy();
    } catch (error) {
      message(job.where, 'Не удалось проверить результат: ' + error.message + '. Повторяем проверку…', true);
      st.timer = setTimeout(poll, 5000);
    }
  }
  return {render};
})();
