/* WB OPS: «Очередь жалоб» — основания, подготовка и отправка жалоб на отзывы через отдельный браузер WB. */
window.WBOPSComplaints = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const GROUPS = [['ready', 'К подаче'], ['not_eligible', 'Без явных оснований'], ['submitted', 'Поданы'], ['dismissed', 'Скрыты']];
  const ORDER = {buyout: 'Выкуп', rejected: 'Отказ', returned: 'Возврат', notSpecified: 'Не указан'};
  const BROWSER = {
    disconnected: ['', 'Не подключён'], starting: ['info', 'Запуск…'], installing: ['info', 'Установка компонентов…'], installed: ['info', 'Готово к запуску'],
    opening_browser: ['info', 'Открывается Chrome…'], waiting_login: ['warn', 'Ждёт входа в WB'], browser_connected: ['ok', 'Подключён'], error: ['bad', 'Ошибка'],
  };
  const MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
  const st = {group: 'ready', search: '', data: null, open: null, prepared: {}, busy: new Set(), owner: true, poll: null};
  let root = null, timer = null;

  async function api(url, options = {}) {
    if (options.body && typeof options.body !== 'string') options = {...options, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(options.body)};
    const r = await fetch(url, options);
    let d = null;
    try { d = await r.json(); } catch (e) { /* пусто */ }
    if (!r.ok) {
      const detail = d && d.detail;
      throw new Error(typeof detail === 'string' ? detail : (detail && detail[0] && detail[0].msg) || `Ошибка ${r.status}`);
    }
    return d;
  }
  function toast(text) {
    const t = document.getElementById('toast');
    if (!t) return;
    t.textContent = text; t.style.display = 'block';
    clearTimeout(toast.timer); toast.timer = setTimeout(() => t.style.display = 'none', 4500);
  }
  const day = s => { const d = new Date(s); return isNaN(d) ? '' : `${d.getDate()} ${MONTHS[d.getMonth()]}<br>${d.getFullYear()}<br>${d.toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit'})}`; };
  const stars = n => n ? `<span class="rv-stars">${'★'.repeat(n)}<i>${'★'.repeat(5 - n)}</i></span>` : '';

  async function render(section) {
    root = section;
    root.className = 'panel rv-page cq-page';
    try { const me = window.WBOPSSettings ? await window.WBOPSSettings.me() : {role: 'owner'}; st.owner = me.role !== 'member'; } catch (e) { st.owner = true; }
    root.innerHTML = `<div data-browser></div>
      <div class="rv-head"><div class="rv-counters" data-tabs></div>
        <div class="rv-head-actions"><button type="button" class="rv-dark" data-recheck title="Заново проверить отзывы 1–2★ на основания для жалобы">Перепроверить основания</button></div></div>
      <div class="cq-search"><label class="rv-search"><span class="sr">Поиск</span><input type="search" data-search placeholder="Поиск по тексту отзыва, товару, имени или WB ID…"><span aria-hidden="true">⌕</span></label></div>
      <div data-list><p class="muted">Загрузка…</p></div>`;
    root.querySelector('[data-search]').oninput = e => { clearTimeout(timer); timer = setTimeout(() => { st.search = e.target.value.trim(); load(); }, 300); };
    root.querySelector('[data-recheck]').onclick = async e => {
      e.currentTarget.disabled = true;
      try { const r = await api('/api/complaints-reevaluate', {method: 'POST'}); toast(r.changed ? `Обновлено карточек: ${r.changed}` : 'Новых оснований не найдено'); } catch (err) { toast(err.message); }
      e.currentTarget.disabled = false; load();
    };
    await load(true);
  }

  async function load(refresh = false) {
    try {
      st.data = await api(`/api/complaints-queue?group=${st.group}&search=${encodeURIComponent(st.search)}${refresh ? '&refresh=1' : ''}`);
    } catch (e) { root.querySelector('[data-list]').innerHTML = `<p class="rv-error">${esc(e.message)}</p>`; return; }
    drawBrowser(st.data.browser); drawTabs(); drawList();
  }

  // ---------------------------------------------------------------- браузер WB
  function drawBrowser(b) {
    const box = root.querySelector('[data-browser]');
    if (!st.owner) {
      box.innerHTML = '<div class="rv-banner">Жалобы подаются через браузер на компьютере владельца панели. Здесь можно смотреть очередь и отмечать поданные вручную.</div>';
      return;
    }
    const [cls, title] = BROWSER[b.status] || ['', b.status];
    const connected = b.status === 'browser_connected', waiting = b.status === 'waiting_login';
    box.innerHTML = `<section class="cq-browser ${cls}">
      <div class="cq-browser-main"><div class="cq-ico" aria-hidden="true">🛡️</div>
        <div><div class="cq-title">Браузер Wildberries для подачи жалоб <span class="rv-status ${cls === 'warn' ? 'warn' : cls === 'bad' ? 'bad' : cls === 'ok' ? 'ok' : cls === 'info' ? 'info' : ''}">${esc(title)}</span></div>
          <p class="muted">${esc(b.message || '')}</p>
          ${!connected && !waiting ? '<p class="cq-hint">Откроется отдельное окно Chrome (или Edge) на этом компьютере. Войдите в кабинет продавца WB — вход запомнится, повторять не нужно. Пароль и код из SMS вводите только в этом окне: панель их не видит.</p>' : ''}
          ${waiting ? '<p class="cq-hint">Войдите в WB в открывшемся окне Chrome и выберите нужный кабинет, затем нажмите «Проверить вход».</p>' : ''}
          ${b.error_detail ? `<details class="cq-err"><summary>Подробности ошибки</summary>${esc(b.error_type || '')}: ${esc(b.error_detail)}</details>` : ''}</div></div>
      <div class="cq-browser-actions">
        ${b.browser_open ? '' : `<button type="button" class="btn" data-connect ${['starting', 'installing', 'opening_browser'].includes(b.status) ? 'disabled' : ''}>Открыть браузер WB</button>`}
        ${b.browser_open ? '<button type="button" class="btn secondary" data-inspect>Проверить вход</button><button type="button" class="btn secondary" data-reviews>Открыть отзывы в WB</button>' : ''}
        ${b.complaints_submitted_by_browser ? `<span class="muted">Подано за сеанс: ${b.complaints_submitted_by_browser}</span>` : ''}</div></section>`;
    box.querySelector('[data-connect]')?.addEventListener('click', connect);
    box.querySelector('[data-inspect]')?.addEventListener('click', () => browserCall('/api/complaint-browser/inspect', 'GET'));
    box.querySelector('[data-reviews]')?.addEventListener('click', () => browserCall('/api/complaint-browser/reviews', 'POST'));
  }
  async function connect() {
    try { drawBrowser(await api('/api/complaint-browser/connect', {method: 'POST'})); } catch (e) { toast(e.message); return; }
    pollBrowser();
  }
  function pollBrowser() {
    clearTimeout(st.poll);
    st.poll = setTimeout(async () => {
      if (!root || !root.isConnected) return;
      try {
        const b = await api('/api/complaint-browser/status');
        st.data.browser = b; drawBrowser(b);
        if (['starting', 'installing', 'opening_browser'].includes(b.status)) pollBrowser();
      } catch (e) { /* ignore */ }
    }, 2000);
  }
  async function browserCall(url, method) {
    try {
      const b = await api(url, {method});
      st.data.browser = {...st.data.browser, ...b}; drawBrowser(st.data.browser);
      toast(b.status === 'browser_connected' ? 'Вход в WB подтверждён' : b.message || 'Готово');
    } catch (e) { toast(e.message); try { st.data.browser = await api('/api/complaint-browser/status'); drawBrowser(st.data.browser); } catch (x) { /* ignore */ } }
  }

  // ---------------------------------------------------------------- список
  function drawTabs() {
    const c = st.data.counts;
    root.querySelector('[data-tabs]').innerHTML = GROUPS.map(([k, t]) =>
      `<button type="button" class="rv-count ${st.group === k ? 'on' : ''}" data-group="${k}">${t} <b>${c[k] || 0}</b></button>`).join('');
    root.querySelectorAll('[data-group]').forEach(b => b.onclick = () => { st.group = b.dataset.group; st.open = null; load(); });
  }
  function drawList() {
    const items = st.data.items, list = root.querySelector('[data-list]');
    if (!items.length) {
      const empty = {ready: ['Жалоб к подаче нет', 'Панель отмечает отзывы 1–2★, где по тексту видно основание: спам, мат, оскорбления, отзыв о доставке или о другом товаре. Остальные — во вкладке «Без явных оснований».'],
        not_eligible: ['Здесь пусто', 'Все отзывы 1–2★ с основаниями уже во вкладке «К подаче».'], submitted: ['Поданных жалоб пока нет', ''], dismissed: ['Скрытых карточек нет', '']}[st.group];
      list.innerHTML = `<div class="rv-empty"><b>${empty[0]}</b><p class="muted">${empty[1]}</p></div>`;
      return;
    }
    list.innerHTML = `<div class="rv-row cq-row rv-th"><div>Дата</div><div>Товар</div><div>Отзыв</div><div>Жалоба</div></div>` + items.map(rowHtml).join('');
    items.forEach(i => bindRow(i));
  }

  function rowHtml(i) {
    const wb = i.product_id ? `https://www.wildberries.ru/catalog/${i.product_id}/detail.aspx` : '';
    const open = st.open === i.item_id;
    const statusChip = {ready_manual: ['info', 'Есть основание'], not_eligible: ['', 'Нет явных оснований'], submitted_manual: ['ok', 'Подана вручную'],
      submitted_browser: ['ok', '✓ Подана через WB OPS'], dismissed: ['muted', 'Скрыта']}[i.status] || ['', i.status];
    const uncertain = i.delivery === 'uncertain';
    return `<div class="rv-row cq-row" data-cq="${esc(i.item_id)}">
      <div class="rv-cell rv-date">${day(i.created)}</div>
      <div class="rv-cell rv-product"><div class="rv-pinfo"><div class="rv-badges"><span class="rv-mp wb">wb</span><span class="rv-cab">${esc(i.cabinet_name)}</span></div>
        <div class="rv-pname">${esc(i.product_name || 'Товар')}</div>
        <div class="rv-art"><span>WB ${esc(i.product_id || '')}</span>${wb ? `<a href="${wb}" target="_blank" rel="noopener" aria-label="Открыть товар на Wildberries">↗</a>` : ''}</div></div></div>
      <div class="rv-cell rv-review">
        <div class="rv-icons">${stars(i.rating)}<span class="rv-order ${i.order_status}">${esc(ORDER[i.order_status] || i.order_status)}</span></div>
        <div class="rv-line"><b>Имя:</b> ${esc(i.author_name || 'Аноним')}</div>
        <div class="rv-line rv-text open"><b>Текст:</b> ${i.review_text ? highlight(i.review_text, i.evidence) : '<span class="muted">без текста</span>'}</div>
        ${i.reply_published ? '<div class="muted cq-small">На отзыв уже есть ответ</div>' : ''}</div>
      <div class="rv-cell rv-answer">
        <div class="rv-ahead"><span class="rv-status ${statusChip[0]}">${statusChip[1]}</span>${uncertain ? '<span class="rv-status warn">Проверьте «Мои жалобы» в WB</span>' : ''}</div>
        ${i.reason_code ? `<div class="cq-reason"><b>Основание:</b> ${esc(i.reason_title)}</div>` : '<div class="muted cq-small">Панель не нашла в тексте нарушения правил. Жалобу на низкую оценку без нарушения WB обычно отклоняет.</div>'}
        ${open ? composer(i) : actions(i, uncertain)}
      </div></div>`;
  }
  function highlight(text, evidence) {
    const t = esc(text);
    const frag = (evidence || '').trim().slice(0, 60);
    if (!frag || frag.length < 4) return t;
    const idx = text.indexOf(frag.slice(0, 30));
    if (idx < 0) return t;
    let end = Math.min(text.length, idx + (evidence || '').trim().length);
    while (end < text.length && /\S/.test(text[end])) end++;   // не обрываем слово
    return esc(text.slice(0, idx)) + '<mark>' + esc(text.slice(idx, end)) + '</mark>' + esc(text.slice(end));
  }
  function actions(i, uncertain) {
    const busy = st.busy.has(i.item_id);
    if (i.status === 'submitted_manual' || i.status === 'submitted_browser' || i.status === 'dismissed') {
      return `<div class="rv-actions"><button type="button" class="btn secondary" data-reopen ${busy ? 'disabled' : ''}>Вернуть в очередь</button></div>`;
    }
    if (uncertain) {
      return `<p class="cq-small">Результат прошлой отправки не подтверждён. Откройте в WB «Мои жалобы»: если жалоба там есть — отметьте её поданной. Повторная отправка заблокирована, чтобы не подать дважды.</p>
        <div class="rv-actions"><button type="button" class="btn" data-mark ${busy ? 'disabled' : ''}>Жалоба есть в WB</button></div>`;
    }
    return `<div class="rv-actions">${st.owner ? `<button type="button" class="btn rv-send" data-compose>Подать жалобу</button>` : ''}
      <button type="button" class="btn secondary" data-mark ${busy ? 'disabled' : ''} title="Если подали жалобу в личном кабинете WB сами">Подана вручную</button>
      <button type="button" class="btn secondary" data-dismiss ${busy ? 'disabled' : ''}>Скрыть</button></div>`;
  }
  function composer(i) {
    const prep = st.prepared[i.item_id], busy = st.busy.has(i.item_id);
    const reasons = st.data.reasons || ['Другое'];
    const reason = prep?.reason || i._reason || reasons[0];
    const text = i._text ?? i.complaint_draft ?? '';
    if (prep && !prep.manual) {
      const left = Math.max(0, Math.round((prep.created_at + prep.expires_in) - Date.now() / 1000));
      return `<div class="cq-preview"><div class="cq-title">Форма в WB заполнена — проверьте и отправьте</div>
        <dl><dt>Кабинет WB</dt><dd>${esc(prep.cabinet)}</dd><dt>Причина</dt><dd>${esc(prep.reason)}</dd><dt>Текст</dt><dd>${prep.text ? esc(prep.text) : '<span class="muted">без пояснения</span>'}</dd></dl>
        <p class="cq-small">То же самое видно в открытом окне Chrome. Подтверждение действует 5 минут${left ? ` (осталось ~${Math.ceil(left / 60)} мин)` : ''}.</p>
        <div class="rv-actions"><button type="button" class="btn rv-send" data-submit ${busy ? 'disabled' : ''}>${busy ? 'Отправка…' : 'Отправить жалобу в WB'}</button>
          <button type="button" class="btn secondary" data-cancel>Отмена</button></div></div>`;
    }
    return `<div class="cq-compose">
      <label>Причина в WB<select data-reason>${reasons.map(r => `<option ${r === reason ? 'selected' : ''}>${esc(r)}</option>`).join('')}</select></label>
      ${reasons.length < 2 ? '<p class="cq-small muted">Остальные причины WB появятся в списке после первой подготовки жалобы. Сейчас доступна «Другое» с пояснением.</p>' : ''}
      <label>Текст жалобы<textarea data-ctext rows="6" maxlength="1000" placeholder="Опишите, какое правило нарушает отзыв (для «Другое» — от 50 символов)">${esc(text)}</textarea></label>
      <div class="rv-counter"><span data-clen>${text.length}</span> / 1000</div>
      <label class="cq-check"><input type="checkbox" data-agree> Я прочитал отзыв: основание подтверждается его текстом</label>
      ${prep && prep.manual ? `<div class="rv-warnbox">${esc(prep.error)}<br><br><b>Что сделать:</b> в окне Chrome найдите этот отзыв (WB ${esc(i.product_id || '')}${i.author_name ? `, ${esc(i.author_name)}` : ''}), откройте его и нажмите «Ещё», чтобы был виден ID отзыва. Затем нажмите «Отзыв открыт — заполнить».</div>` : ''}
      <div class="rv-actions"><button type="button" class="btn rv-send" data-prepare ${busy ? 'disabled' : ''}>${busy ? 'Открываю отзыв в WB…' : 'Заполнить форму в WB'}</button>
        ${prep && prep.manual ? `<button type="button" class="btn secondary" data-prepare-manual ${busy ? 'disabled' : ''}>Отзыв открыт — заполнить</button>` : ''}
        <button type="button" class="btn secondary" data-cancel>Отмена</button></div>
      <p class="cq-small muted">Панель откроет отзыв в окне Chrome и заполнит форму жалобы. Ничего не отправляется до вашего подтверждения.</p></div>`;
  }

  function item(id) { return st.data.items.find(x => x.item_id === id); }
  function redraw(id) {
    const el = root.querySelector(`[data-cq="${CSS.escape(id)}"]`), i = item(id);
    if (!el || !i) return;
    el.outerHTML = rowHtml(i); bindRow(i);
  }
  function bindRow(i) {
    const el = root.querySelector(`[data-cq="${CSS.escape(i.item_id)}"]`);
    if (!el) return;
    const id = i.item_id;
    el.querySelector('[data-compose]')?.addEventListener('click', () => {
      
      st.open = id; redraw(id); el.scrollIntoView({block: 'nearest'});
    });
    el.querySelector('[data-cancel]')?.addEventListener('click', () => { st.open = null; delete st.prepared[id]; redraw(id); });
    const ta = el.querySelector('[data-ctext]');
    if (ta) ta.oninput = () => { i._text = ta.value; el.querySelector('[data-clen]').textContent = ta.value.length; };
    const sel = el.querySelector('[data-reason]');
    if (sel) sel.onchange = () => { i._reason = sel.value; };
    el.querySelector('[data-prepare]')?.addEventListener('click', () => prepare(i, false));
    el.querySelector('[data-prepare-manual]')?.addEventListener('click', () => prepare(i, true));
    el.querySelector('[data-submit]')?.addEventListener('click', () => submit(i));
    el.querySelector('[data-mark]')?.addEventListener('click', () => setStatus(i, 'submitted_manual', 'Отмечено как поданная'));
    el.querySelector('[data-dismiss]')?.addEventListener('click', () => setStatus(i, 'dismissed', 'Карточка скрыта'));
    el.querySelector('[data-reopen]')?.addEventListener('click', () => setStatus(i, 'ready_manual', 'Карточка возвращена в очередь'));
  }

  async function prepare(i, useSelected) {
    const el = root.querySelector(`[data-cq="${CSS.escape(i.item_id)}"]`);
    const text = (el.querySelector('[data-ctext]')?.value || '').trim(), reason = el.querySelector('[data-reason]')?.value || 'Другое';
    i._text = text; i._reason = reason;
    if (!el.querySelector('[data-agree]')?.checked) { toast('Подтвердите, что основание есть в тексте отзыва'); return; }
    if (reason === 'Другое' && text.length < 50) { toast('Для причины «Другое» нужно пояснение от 50 символов'); return; }
    st.busy.add(i.item_id); redraw(i.item_id);
    if (!await ensureBrowser()) { st.busy.delete(i.item_id); redraw(i.item_id); return; }
    try {
      const data = await api(`/api/complaint-browser/prepare/${encodeURIComponent(i.item_id)}`, {method: 'POST', body: {text, reason, use_selected: useSelected}});
      st.prepared[i.item_id] = data;
      toast('Форма жалобы заполнена в окне WB — проверьте и отправьте');
      try { st.data.reasons = (await api(`/api/complaints-queue?group=${st.group}`)).reasons; } catch (e) { /* ignore */ }
    } catch (e) {
      if (/Автопоиск|вручную|ID открытого отзыва/.test(e.message)) st.prepared[i.item_id] = {manual: true, error: e.message};
      else toast(e.message);
    }
    st.busy.delete(i.item_id); redraw(i.item_id);
  }
  // Браузер WB открывается сам, если ещё не открыт; ждём, пока он запустится
  async function ensureBrowser() {
    let b;
    try { b = await api('/api/complaint-browser/status'); } catch (e) { toast(e.message); return false; }
    if (!b.browser_open) {
      toast('Открываю браузер WB…');
      try { b = await api('/api/complaint-browser/connect', {method: 'POST'}); } catch (e) { toast(e.message); return false; }
      for (let n = 0; n < 120 && !b.browser_open && !['error'].includes(b.status); n++) {
        await new Promise(r => setTimeout(r, 1500));
        try { b = await api('/api/complaint-browser/status'); } catch (e) { break; }
      }
    }
    st.data.browser = b; drawBrowser(b);
    if (!b.browser_open) { toast(b.status === 'error' ? `Браузер WB не открылся: ${b.error_detail || b.message}` : 'Браузер WB ещё запускается — повторите через минуту'); return false; }
    try { b = {...b, ...(await api('/api/complaint-browser/inspect'))}; } catch (e) { toast(e.message); st.data.browser = await api('/api/complaint-browser/status').catch(() => b); drawBrowser(st.data.browser); return false; }
    st.data.browser = b; drawBrowser(b);
    if (b.status === 'waiting_login') { toast('Войдите в кабинет WB в открывшемся окне браузера, затем снова нажмите «Заполнить форму в WB»'); return false; }
    return true;
  }

  async function submit(i) {
    const prep = st.prepared[i.item_id];
    if (!prep) return;
    st.busy.add(i.item_id); redraw(i.item_id);
    try {
      const r = await api('/api/complaint-browser/submit', {method: 'POST', body: {token: prep.token, confirmed: true}});
      toast(r.message);
      delete st.prepared[i.item_id]; st.open = null;
    } catch (e) { toast(e.message); if (/устарел/.test(e.message)) delete st.prepared[i.item_id]; }
    st.busy.delete(i.item_id);
    load();
  }
  async function setStatus(i, status, done) {
    st.busy.add(i.item_id); redraw(i.item_id);
    try { await api(`/api/complaints/${encodeURIComponent(i.item_id)}`, {method: 'PUT', body: {status}}); toast(done); } catch (e) { toast(e.message); }
    st.busy.delete(i.item_id);
    load();
  }

  const style = document.createElement('style');
  style.textContent = `
.cq-row{grid-template-columns:92px minmax(0,.9fr) minmax(0,1.3fr) minmax(0,1.3fr)}
.cq-browser{display:flex;flex-wrap:wrap;justify-content:space-between;gap:14px;align-items:center;background:#fff;border:1px solid #e7e9ee;border-radius:12px;padding:14px 16px;margin-bottom:14px}
.cq-browser.ok{border-color:#bbf7d0}.cq-browser.warn{border-color:#fde68a}.cq-browser.bad{border-color:#fecaca}
.cq-browser-main{display:flex;gap:12px;min-width:0;flex:1}.cq-ico{width:40px;height:40px;border-radius:10px;background:#f3f4f6;display:grid;place-items:center;font-size:20px;flex:none}
.cq-title{font-weight:600;display:flex;flex-wrap:wrap;gap:8px;align-items:center}.cq-browser p{margin:4px 0 0;font-size:13px}.cq-hint{color:#4b5563}
.cq-browser-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.cq-browser-actions .btn{height:36px;padding:0 14px;font-size:13px}
.cq-err{font-size:12px;color:#b91c1c;margin-top:6px}.cq-search{margin-bottom:14px}.cq-search .rv-search{display:block}
.cq-reason{font-size:13px}.cq-small{font-size:12px;margin:2px 0}.rv-review mark{background:#fef3c7;border-radius:3px;padding:0 2px}
.cq-compose,.cq-preview{display:flex;flex-direction:column;gap:8px;margin-top:6px}.cq-compose label{display:grid;gap:4px;font-size:12px;font-weight:600;color:#374151}
.cq-compose select{height:36px;border:1px solid #c9cdd6;border-radius:8px;padding:0 8px;font:inherit;font-size:13px;background:#fff}
.cq-compose textarea{width:100%;min-width:0;border:1px solid #c9cdd6;border-radius:8px;padding:9px;font:inherit;font-size:13px;font-weight:400;line-height:1.5;resize:vertical}
.cq-compose .cq-check{display:flex;align-items:center;gap:8px;font-weight:500}.cq-check input{width:16px;height:16px;accent-color:#1f2328}
.cq-preview{background:#f8faff;border:1px solid #c7d7fe;border-radius:10px;padding:12px}.cq-preview dl{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;margin:4px 0;font-size:13px}
.cq-preview dt{color:#6b7280}.cq-preview dd{margin:0;white-space:pre-line}
@media(max-width:1100px){.cq-row{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.cq-row .rv-date{display:none}.cq-row .rv-product{grid-column:1/-1}.cq-row .rv-review{grid-column:1}.cq-row .rv-answer{grid-column:2}.cq-row.rv-th{display:none}}
@media(max-width:760px){.cq-row{grid-template-columns:minmax(0,1fr)}.cq-row .rv-product,.cq-row .rv-review,.cq-row .rv-answer{grid-column:1}}`;
  document.head.appendChild(style);

  return {render};
})();
