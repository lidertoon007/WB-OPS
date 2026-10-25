/* WB OPS · Индивидуальные ответы на вопросы и модели товаров (таблица замеров). */
(() => {
  const main = document.querySelector('main.shell');
  if (!main) return;
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const say = t => (typeof window.toast === 'function' ? window.toast(t) : alert(t));
  async function api(url, options) {
    const r = await fetch(url, options); let d = {};
    try { d = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error(d.detail || ('Ошибка ' + r.status));
    return d;
  }
  const json = (method, body) => ({method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const section = id => { let s = document.getElementById(id); if (!s) { s = document.createElement('section'); s.id = id; s.className = 'panel'; main.append(s); } return s; };
  const INTENT = {size_pick: 'подбор размера', length: 'длина', bottom: 'ширина низа', what_height: 'на какой рост', waist_for_size: 'обхват талии размера',
    composition: 'состав', stretch: 'тянется ли', density: 'плотность', season: 'сезон / тепло', shrink: 'усадка', country: 'страна', fastener: 'застёжка',
    rise: 'посадка', price: 'цена', refund: 'возврат денег', return: 'возврат / обмен', availability: 'наличие', find_mark: 'поиск по маркировке',
    units: 'единицы замеров', ru_map: 'российский размер', complaint: 'жалоба', general: 'общий вопрос'};

  // ---- Все вопросы: режим и проверка ----------------------------------------------------------
  const qp = section('questionRepliesPanel');
  qp.innerHTML = `<div class="r-head"><h2>Ответы на вопросы</h2><button class="btn secondary" data-q-regen>Пересоздать черновики</button></div>
    <p class="muted">Сервис понимает, о чём спрашивают (размер, рост, длина, ширина низа, состав, цена…), и берёт цифры покупателя. Точные размеры советует по таблице замеров модели из раздела <a href="/products" data-nav="products">«Товары»</a>; если таблицы нет — цифры не придумывает.</p>
    <div class="rr-modes" data-q-modes></div><div class="muted" data-q-coverage></div>
    <h3>Проверить на вопросе</h3>
    <div class="rr-try"><textarea data-q-text placeholder="Например: ОТ 94, ОБ 102, рост 186 — какой размер брать? Не будут короткими?"></textarea>
      <div class="rr-try-row"><select data-q-product><option value="">Модель не выбрана</option></select><input data-q-name placeholder="Имя (необязательно)">
        <button class="btn" data-q-try>Показать ответ</button></div>
      <div data-q-result class="rr-result" hidden></div></div>`;
  const MODES = [['individual', 'Индивидуальные', 'Ответ по сути вопроса с цифрами покупателя и данными модели. Без интернета и ключей.'],
    ['ai', 'ИИ', 'Пишет ИИ по данным модели; цифры не из таблицы замеров отклоняются. Если ИИ недоступен — ответ сборщика.'],
    ['templates', 'Шаблоны', 'Прежний режим: шаблон по теме вопроса.']];
  let qmode = 'individual';
  async function loadQMode() {
    try {
      const d = await api('/api/replies/question-mode'); qmode = d.mode;
      qp.querySelector('[data-q-coverage]').innerHTML = `Таблица замеров заполнена у ${d.models_with_sizes} из ${d.models_total} моделей. ${d.models_with_sizes < d.models_total ? 'Заполните остальные в разделе <a href="/products" data-nav="products">«Товары»</a> — ответы о размерах станут точными.' : ''}`;
    } catch (e) {}
    qp.querySelector('[data-q-modes]').innerHTML = MODES.map(([k, t, d]) => `<label class="rr-mode ${qmode === k ? 'active' : ''}"><input type="radio" name="q-mode" value="${k}" ${qmode === k ? 'checked' : ''}><b>${t}</b><span>${d}</span></label>`).join('');
    qp.querySelectorAll('[name=q-mode]').forEach(r => r.onchange = async () => {
      try { await api('/api/replies/question-mode', json('PUT', {mode: r.value})); say('Режим ответов на вопросы сохранён. Нажмите «Пересоздать черновики», чтобы обновить загруженные вопросы.'); } catch (e) { say(e.message); }
      loadQMode();
    });
  }
  qp.querySelector('[data-q-try]').onclick = async () => {
    const out = qp.querySelector('[data-q-result]'); out.hidden = false; out.textContent = 'Готовлю ответ…';
    try {
      const d = await api('/api/replies/question-preview', json('POST', {text: qp.querySelector('[data-q-text]').value, product_id: qp.querySelector('[data-q-product]').value, author_name: qp.querySelector('[data-q-name]').value}));
      const params = Object.entries(d.params).map(([k, v]) => `${k}: ${v}`).join(', ');
      out.innerHTML = `<p class="rr-reply">${esc(d.text)}</p><p class="muted">Тема: ${esc(d.intents.map(i => INTENT[i] || i).join(', ') || '—')}${params ? ' · параметры: ' + esc(params) : ''} · ${d.model ? 'модель ' + esc(d.model) + (d.model_has_sizes ? '' : ' (таблица замеров не заполнена)') : 'модель не выбрана'}${d.ai_error ? ' · ИИ: ' + esc(d.ai_error) : ''}</p>`;
    } catch (e) { out.textContent = e.message; }
  };
  qp.querySelector('[data-q-regen]').onclick = async e => {
    if (!confirm('Пересоздать черновики всех неотправленных вопросов? Подтверждённые и исправленные вручную не изменятся.')) return;
    const b = e.currentTarget; b.disabled = true; b.textContent = 'Пересоздаю…';
    try { const d = await api('/api/replies/regenerate-questions', {method: 'POST'}); say(`Обновлено черновиков: ${d.updated}, без изменений: ${d.kept}`); if (typeof window.load === 'function') window.load(); }
    catch (err) { say(err.message); }
    b.disabled = false; b.textContent = 'Пересоздать черновики';
  };

  // ---- Товары: модели и таблица замеров ----------------------------------------------------------
  const mp = section('modelsPanel');
  mp.innerHTML = `<div class="r-head"><h2>Модели и замеры</h2><button class="btn secondary" data-m-new>Новая модель</button></div>
    <p class="muted">По этим данным сервис отвечает на вопросы о размере, длине, составе и т. п. Одна модель — один артикул продавца, к ней можно привязать несколько WB ID (цвета). Обхваты — тела покупателя в сантиметрах, диапазоном: 86-90.</p>
    <div class="m-layout"><div data-m-list class="m-list">Загрузка…</div><div data-m-editor class="m-editor"><p class="muted">Выберите модель слева.</p></div></div>`;
  let meta = {fact_fields: {}, size_fields: {}}, current = null;
  async function loadModels(select) {
    try {
      const d = await api('/api/models'); meta = d;
      qp.querySelector('[data-q-product]').innerHTML = '<option value="">Модель не выбрана</option>' + d.models.filter(m => m.nm_ids.length).map(m => `<option value="${m.nm_ids[0]}">${esc(m.key)}${m.name ? ' — ' + esc(m.name) : ''}</option>`).join('');
      mp.querySelector('[data-m-list]').innerHTML = d.models.map(m => `<button class="m-item ${current === m.key ? 'active' : ''}" data-m-key="${esc(m.key)}"><b>${esc(m.key)}</b><span>${esc(m.name || 'без названия')}</span>
        <small>${m.sizes ? `размеров: ${m.sizes}` : '<em>нет таблицы замеров</em>'} · фактов: ${m.facts}${m.open_questions ? ` · вопросов: ${m.open_questions}` : ''}</small></button>`).join('')
        + (d.unlinked.length ? `<p class="muted" style="margin-top:12px">WB ID с вопросами без модели: ${d.unlinked.map(u => `<b>${esc(u.nm_id)}</b> (${u.questions})`).join(', ')}. Добавьте их в модель.</p>` : '');
      mp.querySelectorAll('[data-m-key]').forEach(b => b.onclick = () => openModel(b.dataset.mKey));
      if (select) openModel(select);
    } catch (e) { mp.querySelector('[data-m-list]').textContent = e.message; }
  }
  function sizeRow(row = {}) {
    return `<tr>${Object.keys(meta.size_fields).map(k => `<td><input data-s="${k}" value="${esc(row[k] || '')}"></td>`).join('')}<td><button class="btn secondary" data-s-del title="Удалить строку">×</button></td></tr>`;
  }
  function editor(m) {
    const ed = mp.querySelector('[data-m-editor]');
    ed.innerHTML = `<div class="m-grid"><label>Артикул продавца<input name="key" value="${esc(m.key || '')}"></label><label>WB ID (через запятую)<input name="nm_ids" value="${esc((m.nm_ids || []).join(', '))}"></label></div>
      <h4>Таблица замеров</h4><div class="f-scroll"><table class="m-sizes"><tr>${Object.values(meta.size_fields).map(t => `<th>${esc(t)}</th>`).join('')}<th></th></tr>${(m.sizes || []).map(sizeRow).join('')}</table></div>
      <button class="btn secondary" data-s-add>+ Строка</button>
      <h4>Факты для ответов</h4><div class="m-grid">${Object.entries(meta.fact_fields).map(([k, t]) => `<label>${esc(t)}<${k === 'notes' ? 'textarea' : 'input'} name="${k}" ${k === 'notes' ? '' : `value="${esc(m[k] || '')}"`}>${k === 'notes' ? esc(m[k] || '') + '</textarea>' : ''}</label>`).join('')}</div>
      <div class="toolbar"><button class="btn" data-m-save>Сохранить модель</button>${m.key ? '<button class="btn secondary" data-m-del>Удалить модель</button>' : ''}</div>
      ${(m.hints || []).length ? `<details class="m-hints" open><summary>Подсказки из ваших прошлых ответов (${m.hints.length}) — проверьте и перенесите нужное в таблицу</summary>${m.hints.map(h => `<div class="m-hint"><b>${esc(h.question)}</b><p>${esc(h.answer)}</p></div>`).join('')}</details>` : ''}`;
    const table = ed.querySelector('.m-sizes');
    const bindDel = () => table.querySelectorAll('[data-s-del]').forEach(b => b.onclick = () => b.closest('tr').remove());
    bindDel();
    ed.querySelector('[data-s-add]').onclick = () => { table.insertAdjacentHTML('beforeend', sizeRow()); bindDel(); };
    ed.querySelector('[data-m-save]').onclick = async () => {
      const body = {key: ed.querySelector('[name=key]').value.trim(), nm_ids: [ed.querySelector('[name=nm_ids]').value],
        sizes: [...table.querySelectorAll('tr')].slice(1).map(tr => Object.fromEntries([...tr.querySelectorAll('[data-s]')].map(i => [i.dataset.s, i.value.trim()]))).filter(r => r.size)};
      Object.keys(meta.fact_fields).forEach(k => { body[k] = ed.querySelector(`[name="${k}"]`).value; });
      try { const saved = await api('/api/models/' + encodeURIComponent(m.key || body.key || 'new'), json('PUT', body)); say('Модель сохранена'); current = saved.key; loadModels(saved.key); loadQMode(); }
      catch (e) { say(e.message); }
    };
    const del = ed.querySelector('[data-m-del]');
    if (del) del.onclick = async () => {
      if (!confirm(`Удалить модель ${m.key}?`)) return;
      try { await api('/api/models/' + encodeURIComponent(m.key), {method: 'DELETE'}); current = null; ed.innerHTML = '<p class="muted">Модель удалена.</p>'; loadModels(); loadQMode(); } catch (e) { say(e.message); }
    };
  }
  async function openModel(key) {
    current = key;
    mp.querySelectorAll('[data-m-key]').forEach(b => b.classList.toggle('active', b.dataset.mKey === key));
    try { editor(await api('/api/models/' + encodeURIComponent(key))); } catch (e) { say(e.message); }
  }
  mp.querySelector('[data-m-new]').onclick = () => { current = null; editor({key: '', nm_ids: [], sizes: [{}, {}, {}]}); };

  const style = document.createElement('style');
  style.textContent = `.m-layout{display:grid;grid-template-columns:minmax(200px,280px) minmax(0,1fr);gap:16px}.m-editor{min-width:0}#modelsPanel{overflow:hidden}.m-list{display:grid;gap:6px;align-content:start;max-height:75vh;overflow:auto}.m-item{appearance:none;text-align:left;font:inherit;border:1px solid #e5e7eb;background:#fff;border-radius:10px;padding:9px 11px;cursor:pointer;display:grid;gap:2px}.m-item span{color:#4b5563;font-size:13px}.m-item small{color:#6b7280;font-size:12px}.m-item em{color:#b45309;font-style:normal}.m-item.active{border-color:#8b5cf6;background:#faf5ff}
.m-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}.m-grid label>*{min-width:0}.m-grid label{display:grid;gap:4px;font-size:13px;color:#374151}.m-grid input,.m-grid textarea{padding:8px;border:1px solid #ddd6fe;border-radius:9px;font:inherit}.m-sizes{border-collapse:collapse;width:100%}.m-sizes th{font-size:12px;text-align:left;padding:6px;background:#f9fafb;white-space:nowrap}.m-sizes td{padding:3px}.m-sizes input{width:100%;min-width:64px;box-sizing:border-box;padding:6px;border:1px solid #e5e7eb;border-radius:7px}
.m-hints{margin-top:16px}.m-hints summary{cursor:pointer;color:#6d28d9;font-weight:700}.m-hint{border-left:3px solid #ddd6fe;padding:4px 10px;margin:8px 0}.m-hint p{margin:4px 0;color:#4b5563;font-size:13px;white-space:pre-wrap}
@media(max-width:800px){.m-layout{grid-template-columns:1fr}}`;
  document.head.appendChild(style);

  loadQMode(); loadModels();
})();
