/* WB OPS · Индивидуальные ответы на отзывы: режим, проверка на отзыве, фразы сборщика, подключение ИИ. */
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

  const MODES = [
    ['individual', 'Индивидуальные', 'Сервис разбирает отзыв — посадку, размер, ткань, цвет, кому купили, повторную покупку — и собирает ответ под него. Работает без интернета и ключей.'],
    ['ai', 'ИИ', 'Черновик пишет ИИ (Claude или совместимый сервис) по правилам магазина. Если ИИ недоступен, ответ соберёт сборщик.'],
    ['templates', 'Шаблоны', 'Прежний режим: короткие шаблоны по теме отзыва.'],
  ];
  const GROUPS = [
    ['Приветствие и благодарность', ['greet_name', 'greet', 'thanks_5', 'thanks_long', 'thanks_4', 'thanks_neutral', 'thanks_empty_5', 'empty_low']],
    ['Что понравилось', ['fit_good', 'length_ok', 'length_long', 'quality_good', 'stretch', 'warm', 'light', 'color_good', 'looks_good', 'wash_ok', 'price_good', 'delivery_fast', 'repeat', 'recommend', 'seller_thanks', 'long_wait_wear', 'size_tip']],
    ['Кому купили', ['for_husband', 'for_son', 'for_teen', 'for_grandson', 'for_father']],
    ['Отказ, возврат, размер', ['size_wrong', 'size_small', 'size_big', 'length_short', 'length_long_bad', 'size_advice', 'reorder', 'rejected_positive', 'rejected_quality']],
    ['Замечания', ['defect', 'not_as_described', 'thin', 'color_diff', 'price_bad', 'delivery_issue', 'smell', 'negative_general']],
    ['Завершение', ['wish_buyout', 'wish_other']],
  ];

  // ---- Шаблоны ответов: режим, проверка, фразы --------------------------------
  const panel = section('reviewRepliesPanel');
  panel.innerHTML = `<div class="r-head"><h2>Ответы на отзывы</h2><button class="btn secondary" data-rr-regen>Пересоздать черновики</button></div>
    <p class="muted">Выберите, как готовить черновики. Подтверждённые, отправленные и исправленные вручную черновики не меняются.</p>
    <div class="rr-modes" data-rr-modes></div><div data-rr-ai-note class="muted"></div>
    <h3>Проверить на отзыве</h3>
    <div class="rr-try"><textarea data-rr-text placeholder="Текст отзыва, например: Сели как влитые, мужу очень понравились, заказали вторую пару"></textarea>
      <div class="rr-try-row"><input data-rr-name placeholder="Имя покупателя">
        <select data-rr-rating><option value="5">5★</option><option value="4">4★</option><option value="3">3★</option><option value="2">2★</option><option value="1">1★</option><option value="">без оценки</option></select>
        <select data-rr-status><option value="buyout">Выкуп</option><option value="rejected">Отказ</option><option value="returned">Возврат</option><option value="notSpecified">Статус не указан</option></select>
        <input data-rr-product placeholder="Товар (необязательно)" value="Джинсы мужские прямые">
        <button class="btn" data-rr-try>Показать ответ</button><button class="btn secondary" data-rr-again title="Другой вариант формулировок">Другой вариант</button></div>
      <div data-rr-result class="rr-result" hidden></div></div>
    <details class="rr-phrases"><summary>Фразы для индивидуальных ответов</summary>
      <p class="muted">Одна строка — один вариант. Сервис чередует варианты, чтобы ответы не повторялись. Можно подставлять {product} (джинсы, брюки…) и {name}.</p>
      <div data-rr-phrases>Загрузка…</div></details>`;

  let mode = 'individual', aiInfo = {}, seed = 0;
  function renderModes() {
    panel.querySelector('[data-rr-modes]').innerHTML = MODES.map(([key, title, text]) =>
      `<label class="rr-mode ${mode === key ? 'active' : ''}"><input type="radio" name="rr-mode" value="${key}" ${mode === key ? 'checked' : ''}><b>${title}</b><span>${text}</span></label>`).join('');
    panel.querySelectorAll('[name=rr-mode]').forEach(r => r.onchange = async () => {
      try { const d = await api('/api/replies/review-mode', json('PUT', {mode: r.value})); mode = d.mode; aiInfo = d.ai; say('Режим ответов сохранён. Нажмите «Пересоздать черновики», чтобы обновить уже загруженные отзывы.'); }
      catch (e) { say(e.message); }
      renderModes();
    });
    panel.querySelector('[data-rr-ai-note]').innerHTML = aiInfo.has_key
      ? `ИИ подключён (${esc(aiInfo.provider === 'openai' ? 'ChatGPT / OpenAI' : 'Claude')}${aiInfo.model ? ', ' + esc(aiInfo.model) : ''}). Настройки — в разделе <a href="/ai" data-nav="ai">Настройки ИИ</a>.`
      : 'Чтобы включить режим «ИИ», укажите API-ключ в разделе <a href="/ai" data-nav="ai">Настройки ИИ</a>.';
  }
  async function loadMode() {
    try { const d = await api('/api/replies/review-mode'); mode = d.mode; aiInfo = d.ai || {}; } catch (e) {}
    renderModes();
  }

  async function tryReply(again) {
    if (again) seed += 1;
    const out = panel.querySelector('[data-rr-result]');
    out.hidden = false; out.textContent = mode === 'ai' ? 'ИИ пишет ответ…' : 'Готовлю ответ…';
    try {
      const d = await api('/api/replies/preview', json('POST', {
        text: panel.querySelector('[data-rr-text]').value, author_name: panel.querySelector('[data-rr-name]').value,
        rating: panel.querySelector('[data-rr-rating]').value, order_status: panel.querySelector('[data-rr-status]').value,
        product_name: panel.querySelector('[data-rr-product]').value, seed, mode}));
      out.innerHTML = `<p class="rr-reply">${esc(d.text)}</p>
        <p class="muted">${d.source === 'ai' ? 'Написал ИИ' : 'Собрано сборщиком'}${d.ai_error ? ' (ИИ недоступен: ' + esc(d.ai_error) + ')' : ''} ·
        распознано: ${d.details.length ? esc(d.details.join(', ')) : 'ничего конкретного'}${d.name ? ' · имя: ' + esc(d.name) : ''}</p>
        ${d.warnings.length ? `<p class="f-alert">${esc(d.warnings.join('; '))}</p>` : ''}`;
    } catch (e) { out.textContent = e.message; }
  }
  panel.querySelector('[data-rr-try]').onclick = () => tryReply(false);
  panel.querySelector('[data-rr-again]').onclick = () => tryReply(true);
  panel.querySelector('[data-rr-regen]').onclick = async e => {
    if (!confirm('Пересоздать черновики всех неотправленных отзывов в текущем режиме? Подтверждённые и исправленные вручную не изменятся.')) return;
    const b = e.currentTarget; b.disabled = true; b.textContent = 'Пересоздаю…';
    try { const d = await api('/api/replies/regenerate-reviews', {method: 'POST'}); say(`Обновлено черновиков: ${d.updated}, без изменений: ${d.kept}`); if (typeof window.load === 'function') window.load(); }
    catch (err) { say(err.message); }
    b.disabled = false; b.textContent = 'Пересоздать черновики';
  };

  async function loadPhrases() {
    const box = panel.querySelector('[data-rr-phrases]');
    try {
      const d = await api('/api/templates/phrases'), byKey = Object.fromEntries(d.phrases.map(p => [p.key, p]));
      box.innerHTML = GROUPS.map(([title, keys]) => `<h4>${esc(title)}</h4>` + keys.filter(k => byKey[k]).map(k => {
        const p = byKey[k];
        return `<div class="rr-phrase"><label><b>${esc(p.title)}</b>${p.customized ? ' <span class="status-chip">изменено</span>' : ''}
          <textarea data-rr-key="${esc(k)}" rows="${Math.min(6, p.variants.length + 1)}">${esc(p.variants.join('\n'))}</textarea></label>
          <div class="toolbar"><button class="btn secondary" data-rr-save="${esc(k)}">Сохранить</button>${p.customized ? `<button class="btn secondary" data-rr-reset="${esc(k)}">Вернуть стандартные</button>` : ''}</div></div>`;
      }).join('')).join('');
      box.querySelectorAll('[data-rr-save]').forEach(b => b.onclick = async () => {
        const k = b.dataset.rrSave, variants = box.querySelector(`[data-rr-key="${CSS.escape(k)}"]`).value.split('\n');
        try { await api('/api/templates/phrases/' + encodeURIComponent(k), json('PUT', {variants})); say('Фразы сохранены'); loadPhrases(); } catch (e) { say(e.message); }
      });
      box.querySelectorAll('[data-rr-reset]').forEach(b => b.onclick = async () => {
        try { await api('/api/templates/phrases/' + encodeURIComponent(b.dataset.rrReset), {method: 'DELETE'}); say('Возвращены стандартные фразы'); loadPhrases(); } catch (e) { say(e.message); }
      });
    } catch (e) { box.textContent = e.message; }
  }

  // ---- Настройки ИИ: подключение ----------------------------------------------
  const ai = section('aiConnectionPanel');
  ai.innerHTML = `<h2>Подключение ИИ</h2>
    <p class="muted">Ключ Claude: <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener">console.anthropic.com → API Keys</a>; ключ ChatGPT: <a href="https://platform.openai.com/api-keys" target="_blank" rel="noopener">platform.openai.com → API keys</a>. Нужен аккаунт и пополненный баланс. Нужно только для режима «ИИ» в разделе «Шаблоны ответов». Ключ хранится в локальной базе WB OPS и не показывается после сохранения. ИИ готовит только черновик — отправка, как и раньше, после вашей проверки.</p>
    <form class="r-form" data-ai-form>
      <label>Сервис<select name="provider"><option value="anthropic">Claude (Anthropic)</option><option value="openai">ChatGPT (OpenAI) или совместимый сервис</option></select></label>
      <label data-ai-base hidden style="display:none">Адрес API<input name="base_url" placeholder="Пусто — https://api.openai.com/v1 (ChatGPT)"></label>
      <label>API-ключ<input name="api_key" type="password" autocomplete="off" placeholder="sk-…"></label>
      <label>Модель<input name="model" list="aiModels" placeholder="Пусто — выбрать автоматически"><datalist id="aiModels"></datalist></label>
      <div class="toolbar"><button class="btn" type="submit">Сохранить</button><button class="btn secondary" type="button" data-ai-test>Проверить подключение</button><button class="btn secondary" type="button" data-ai-clear>Удалить ключ</button></div>
      <div data-ai-state class="muted"></div></form>`;
  const form = ai.querySelector('[data-ai-form]');
  const syncBase = () => { const base = ai.querySelector('[data-ai-base]'); base.hidden = form.provider.value !== 'openai'; base.style.display = base.hidden ? 'none' : ''; form.api_key.placeholder = form.api_key.placeholder.startsWith('Ключ сохранён') ? form.api_key.placeholder : (form.provider.value === 'openai' ? 'sk-…' : 'sk-ant-…'); };
  form.provider.onchange = syncBase;
  async function loadAi() {
    try {
      const d = await api('/api/ai/settings');
      form.provider.value = d.provider; form.base_url.value = d.base_url; form.model.value = d.model; form.api_key.value = '';
      form.api_key.placeholder = d.has_key ? `Ключ сохранён (${d.key_hint}). Введите новый, чтобы заменить` : 'sk-…';
      ai.querySelector('[data-ai-state]').textContent = d.last_error ? 'Последняя ошибка ИИ: ' + d.last_error : (d.has_key ? 'Ключ сохранён.' : 'Ключ не задан.');
      syncBase();
    } catch (e) {}
  }
  form.onsubmit = async ev => {
    ev.preventDefault();
    const body = {provider: form.provider.value, base_url: form.base_url.value, model: form.model.value};
    if (form.api_key.value.trim()) body.api_key = form.api_key.value.trim();
    try { await api('/api/ai/settings', json('PUT', body)); say('Настройки ИИ сохранены'); loadAi(); loadMode(); } catch (e) { say(e.message); }
  };
  ai.querySelector('[data-ai-clear]').onclick = async () => {
    if (!confirm('Удалить сохранённый API-ключ? Режим «ИИ» переключится на индивидуальные ответы.')) return;
    try { await api('/api/ai/settings', json('PUT', {provider: form.provider.value, base_url: form.base_url.value, model: form.model.value, api_key: ''})); say('Ключ удалён'); loadAi(); loadMode(); } catch (e) { say(e.message); }
  };
  ai.querySelector('[data-ai-test]').onclick = async e => {
    const state = ai.querySelector('[data-ai-state]'), b = e.currentTarget; b.disabled = true; state.textContent = 'Проверяю…';
    try {
      const d = await api('/api/ai/test', {method: 'POST'});
      ai.querySelector('#aiModels').innerHTML = d.models.map(m => `<option value="${esc(m)}">`).join('');
      state.textContent = `Подключение работает. Доступно моделей: ${d.models.length}. Будет использоваться: ${d.selected || '—'}.`;
    } catch (err) { state.textContent = err.message + ' Из России API Claude и ChatGPT обычно работают только через VPN.'; }
    b.disabled = false;
  };

  const style = document.createElement('style');
  style.textContent = `.rr-modes{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin:12px 0}.rr-mode{border:1px solid #e5e7eb;border-radius:12px;padding:12px 14px;cursor:pointer;display:grid;gap:4px}.rr-mode input{display:none}.rr-mode span{color:#6b7280;font-size:13px;line-height:1.45}.rr-mode.active{border-color:#8b5cf6;background:#faf5ff}
.rr-try textarea{width:100%;min-height:80px;box-sizing:border-box;padding:10px;border:1px solid #ddd6fe;border-radius:10px;font:inherit}.rr-try-row{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}.rr-try-row input,.rr-try-row select{padding:9px;border:1px solid #ddd6fe;border-radius:10px;min-width:0}
.rr-result{border:1px solid #e5e7eb;border-radius:12px;padding:12px 14px;background:#f9fafb}.rr-reply{font-size:15px;line-height:1.55;margin:0 0 6px}.rr-phrases{margin-top:16px}.rr-phrases summary{cursor:pointer;color:#6d28d9;font-weight:700}.rr-phrase{margin:10px 0}.rr-phrase textarea{width:100%;box-sizing:border-box;padding:8px;border:1px solid #e5e7eb;border-radius:10px;font:inherit;font-size:13px}.rr-phrases h4{margin:18px 0 4px;color:#5b21b6}
#aiConnectionPanel .r-form select,#aiConnectionPanel .r-form input{padding:9px;border:1px solid #ddd6fe;border-radius:10px}`;
  document.head.appendChild(style);

  // Базовые шаблоны теперь нужны режиму «Шаблоны» и ответам на вопросы — подпишем это
  let tries = 0;
  const relabel = setInterval(() => {
    const h = document.querySelector('#templatePanel h2');
    if (h) { h.textContent = 'Базовые шаблоны'; const m = h.nextElementSibling; if (m && m.classList.contains('muted')) m.textContent = 'Используются в режиме «Шаблоны» и в ответах на вопросы.'; }
    if (h || ++tries > 40) clearInterval(relabel);
  }, 250);

  loadMode(); loadPhrases(); loadAi();
})();
