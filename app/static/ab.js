/* WB OPS: «A/B-тесты» главного фото — варианты по очереди становятся главным фото, метрики считаются на трафике рекламной кампании. */
window.WBOPSAB = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const num = (v, d = 0) => v == null || v === '' || isNaN(v) ? '—' : Number(v).toLocaleString('ru-RU', {minimumFractionDigits: d, maximumFractionDigits: d});
  const pct = (v, d = 2) => v == null ? '—' : num(v, d) + '%';
  const dt = s => s ? new Date(s).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}) : '—';
  const STATUS = {draft: ['Черновик', '#868e96'], running: ['Идёт', '#2f9e44'], paused: ['На паузе', '#f59f00'], auto_paused: ['Пауза: кампания', '#f59f00'], finished: ['Завершён', '#4c6ef5']};
  const METRIC = {ctr: 'CTR', cart: 'CTR × конверсия в корзину'};
  const st = {view: 'list', id: null, data: null, test: null, form: null};
  let root = null;
  const api = async (url, opt) => { const r = await fetch(url, opt); const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.detail || 'Ошибка запроса'); return d; };
  const post = (url, body) => api(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
  const chip = s => `<span class="ab-st" style="--c:${(STATUS[s] || ['', '#adb5bd'])[1]}">${esc((STATUS[s] || [s])[0])}</span>`;
  const img = v => v.url || (v.has_file ? `/api/ab/file/${v.id}` : '');
  const change = (a, b, d = 2) => a == null || b == null ? '—' : `${pct(a, d)} → ${pct(b, d)}${a ? ` <small class="${b >= a ? 'pos' : 'neg'}">(${b >= a ? '+' : ''}${num((b / a - 1) * 100, 0)}%)</small>` : ''}`;

  async function render(section) {
    root = section;
    root.className = 'panel rv-page fb-page ab-page';
    await show();
  }
  async function show() {
    if (st.view === 'detail' && st.id) return openTest(st.id);
    if (st.view === 'new') return drawNew();
    try { st.data = await api('/api/ab'); } catch (e) { root.innerHTML = `<p class="rv-error">${esc(e.message)}</p>`; return; }
    drawList();
  }

  // ---------------------------------------------------------------- список
  function drawList() {
    const d = st.data;
    root.innerHTML = `<div class="db-head"><h2>A/B-тесты главного фото</h2><button type="button" class="btn" data-new>+ Новый тест</button></div>
      ${!d.has_tokens ? '<div class="db-note">Нет кабинета WB с ключом API. Нужен ключ с категориями «Контент» и «Продвижение» (с правом записи).</div>' : ''}
      ${!d.campaigns.length ? '<div class="db-note">Тест идёт на показах рекламы, а активных кампаний не найдено. Откройте «Аналитика рекламы» и нажмите «Обновить из WB».</div>' : ''}
      <div class="ab-how">${[['1', 'Выберите артикул', 'и фото-варианты: из карточки или свои'], ['2', 'Привяжите кампанию', 'клики и заказы считаются на живом трафике рекламы'],
        ['3', 'Запустите', 'панель сама ставит варианты главным фото по очереди'], ['4', 'Смотрите метрики', 'худшие варианты исключаются при уверенности ≥95%, победитель встаёт на карточку']]
        .map(([n, t, s]) => `<div><b>${n}</b><span><strong>${t}</strong>${s}</span></div>`).join('')}</div>
      ${d.tests.length ? `<div class="rnp-scroll"><table class="rnp-table fb-groups ab-table"><thead><tr><th class="l">Товар</th><th class="l">Статус</th><th class="l">Метрика</th>
        <th>CTR: было → стало</th><th>Корзина: было → стало</th><th>Показы</th><th>Варианты</th><th class="l">Запущен</th></tr></thead><tbody>
        ${d.tests.map(t => `<tr class="pn-click" data-open="${t.id}"><td class="l ab-prod">${t.thumb ? `<img src="${esc(t.thumb)}" alt="" loading="lazy">` : '<i></i>'}<span><b>${esc(t.article || t.nm_id)}</b><small>${t.nm_id} · ${esc(t.title || '')}</small></span></td>
          <td class="l">${chip(t.status)}${t.pause_reason ? `<small>${esc(t.pause_reason)}</small>` : ''}</td><td class="l">${esc(METRIC[t.metric])}<small>${t.mode === 'clusters' ? `по ${num(t.views_per_step)} показов на кластер` : t.mode === 'timer' ? 'смена по таймеру' : 'смена по показам'}</small></td>
          <td>${change(t.summary.control_ctr, t.summary.best_ctr)}</td><td>${change(t.summary.control_cart, t.summary.best_cart, 1)}</td><td>${num(t.summary.views)}</td>
          <td>${t.summary.alive} из ${t.variants}</td><td class="l">${dt(t.started_at)}</td></tr>`).join('')}</tbody></table></div>`
        : '<div class="rv-empty"><b>Тестов пока нет</b><p class="muted">Нажмите «Новый тест». Можно начать со второстепенного артикула.</p></div>'}
      <p class="muted rp-note">Как и в сервисах A/B-тестов, трафик — показы вашей рекламной кампании: панель снимает статистику кампании и засчитывает прирост тому фото, которое стояло главным.
        Статистика WB приходит с задержкой до часа, поэтому фото меняется не чаще раза в 2 часа. Остальные фото карточки не трогаются.</p>`;
    root.querySelector('[data-new]').onclick = () => { st.view = 'new'; st.form = null; drawNew(); };
    root.querySelectorAll('[data-open]').forEach(r => r.onclick = () => openTest(Number(r.dataset.open)));
  }

  // ---------------------------------------------------------------- новый тест
  function drawNew() {
    const F = st.form || (st.form = {nm: '', card: null, advert: '', cabinet: '', photos: [], mode: 'clusters', interval: 6, views: 100, metric: 'ctr', clusters: ['Джинсы мужские классические','Джинсы мужские прямые','Джинсы мужские прямые классические'], days: 14, busy: false, error: ''});
    const camps = st.data?.campaigns || [];
    const nm = Number(F.nm);
    const visibleCamps = camps.filter(c => !F.cabinet || c.account === F.cabinet);
    const fit = visibleCamps.filter(c => c.nms.includes(nm));
    root.innerHTML = `<div class="db-head"><h2>Новый A/B-тест</h2><button type="button" class="db-ghost" data-back>← К списку</button></div>
      <section class="tr-box"><h3>1. Артикул</h3>
        <div class="ab-row ab-product-row"><label>Артикул WB (nmID)<input data-nm inputmode="numeric" value="${esc(F.nm)}" placeholder="например, 421016586"><small class="muted">Если в компании один товар, можно оставить пустым.</small></label>
          <label>Кабинет / юрлицо<select data-f="cabinet"><option value="">Все кабинеты</option>${[...new Set(camps.map(c => c.account).filter(Boolean))].map(a => `<option value="${esc(a)}" ${F.cabinet === a ? 'selected' : ''}>${esc(a)}</option>`).join('')}</select></label>
          <label>Компания<input data-campaign-top list="ab-campaigns" inputmode="numeric" value="${esc(F.advert)}" placeholder="номер компании"><datalist id="ab-campaigns">${visibleCamps.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('')}</datalist></label>
          <label>или артикул из кампании<select data-pick><option value="">— выберите артикул —</option>${visibleCamps.map(c => `<optgroup label="${esc(c.name)} · ${c.id}">${c.nms.map(n => `<option value="${n}">${n}</option>`).join('')}</optgroup>`).join('')}</select></label>
          <button type="button" class="btn" data-load ${F.busy ? 'disabled' : ''}>${F.busy ? 'Загружаю…' : 'Загрузить карточку'}</button></div>
        ${F.error ? `<div class="ab-form-error" role="alert">${esc(F.error)}</div>` : ''}
        ${F.card ? `<p><b>${esc(F.card.article)}</b> · ${esc(F.card.title)} <span class="muted">· ${esc(F.card.account)}</span></p>
          <p class="muted fb-sub">Отметьте фото карточки, которые тоже хотите проверить в роли главного. Первое — текущее главное, оно всегда участвует как контроль. Свои фото добавите на следующем шаге.</p>
          <div class="ab-photos">${F.card.photos.map((u, i) => `<label class="ab-ph ${i === 0 || F.photos.includes(i) ? 'on' : ''}"><img src="${esc(F.card.thumbs[i] || u)}" alt="Фото ${i + 1}" loading="lazy">
            <span><input type="checkbox" data-ph="${i}" ${i === 0 || F.photos.includes(i) ? 'checked' : ''} ${i === 0 ? 'disabled' : ''}> ${i === 0 ? 'Главное (контроль)' : `Фото №${i + 1}`}</span></label>`).join('')}</div>` : ''}</section>
      ${F.card ? `<section class="tr-box"><h3>2. Рекламная кампания и настройки</h3>
        <div class="ab-row ab-settings-main"><label>Юрлицо / кабинет<select data-f="cabinet"><option value="">Все кабинеты</option>${[...new Set(camps.map(c => c.account).filter(Boolean))].map(a => `<option value="${esc(a)}" ${F.cabinet === a ? 'selected' : ''}>${esc(a)}</option>`).join('')}</select></label>
          <label>Номер компании вручную<input data-f="advert" inputmode="numeric" value="${esc(F.advert)}" placeholder="например, 12345678"></label>
          <label>Кампания из списка<select data-campaign><option value="">— выбрать —</option>${(fit.length ? fit : visibleCamps).map(c => `<option value="${c.id}" ${String(F.advert) === String(c.id) ? 'selected' : ''}>${esc(c.account)} · ${esc(c.name)} · ${c.id}${c.status === 11 ? ' (пауза)' : ''}</option>`).join('')}</select>
          ${!fit.length ? '<small class="neg">Артикула нет в активных кампаниях — выберите кампанию, где он рекламируется, иначе данных не будет</small>' : ''}</label>
          <label>Метрика победы<select data-f="metric"><option value="ctr" ${F.metric === 'ctr' ? 'selected' : ''}>CTR (клики / показы)</option><option value="cart" ${F.metric === 'cart' ? 'selected' : ''}>CTR × конверсия в корзину</option></select></label>
          <label>Смена фото<select data-f="mode"><option value="clusters" ${F.mode === 'clusters' ? 'selected' : ''}>По кластерам</option><option value="timer" ${F.mode === 'timer' ? 'selected' : ''}>По таймеру</option><option value="views" ${F.mode === 'views' ? 'selected' : ''}>По числу показов</option></select></label></div>
        <div class="ab-row ab-settings-extra"><label>${F.mode === 'timer' ? 'Каждые, часов' : F.mode === 'clusters' ? 'Показов на кластер' : 'Каждые, показов'}<input type="number" min="${F.mode === 'timer' ? 2 : 100}" ${F.mode === 'timer' ? 'max="72"' : 'step="100"'} data-f="${F.mode === 'timer' ? 'interval' : 'views'}" value="${F.mode === 'timer' ? F.interval : F.views}"></label>
          <label class="ab-clusters-field">Кластеры для теста
            <div class="ab-cluster-editor" data-clusters-editor>${F.clusters.map((cluster, i) => `<div class="ab-cluster-row"><input data-cluster-index="${i}" value="${esc(cluster)}" placeholder="Например, джинсы мужские"><button type="button" class="ab-cluster-remove" data-remove-cluster="${i}" aria-label="Удалить кластер">×</button></div>`).join('')}
              <button type="button" class="db-ghost ab-cluster-add" data-add-cluster>+ Добавить кластер</button>
            </div>
            <small class="muted">Каждый кластер — отдельной строкой. Для каждого фото будет по ${F.views} показов на каждый кластер.</small>
            ${F.mode === 'clusters' ? '<small class="ab-retro-hint">Ретро-статистика текущего главного фото используется как контроль. Если по всем кластерам уже набран объём, тест сразу перейдёт к следующему фото.</small>' : ''}
          </label>
          <label>Максимум дней<input type="number" min="1" max="90" data-f="days" value="${F.days}"></label></div>
        <button type="button" class="btn" data-create>Создать черновик →</button></section>` : ''}`;
    root.querySelector('[data-back]').onclick = () => { st.view = 'list'; show(); };
    const nmIn = root.querySelector('[data-nm]');
    nmIn.oninput = () => { F.nm = nmIn.value.replace(/\D/g, ''); F.error = ''; };
    root.querySelector('[data-pick]').onchange = e => { if (e.target.value) { F.nm = e.target.value; loadCard(); } };
    // Ручной ввод номера компании не должен перерисовывать форму и сбрасывать фильтр кабинета.
    root.querySelector('[data-campaign-top]').oninput = e => { F.advert = e.target.value.replace(/\D/g, ''); F.error = ''; };
    const campaignSelect = root.querySelector('[data-campaign]');
    if (campaignSelect) campaignSelect.onchange = e => { if (e.target.value) { F.advert = e.target.value; drawNew(); } };
    // Делегирование гарантирует работу кнопки после любой перерисовки формы.
    root.onclick = e => {
      const button = e.target.closest('[data-load]');
      if (button && !button.disabled) { e.preventDefault(); loadCard(); }
    };
    const loadBtn = root.querySelector('[data-load]');
    if (loadBtn) loadBtn.type = 'button';
    root.querySelectorAll('[data-ph]').forEach(cb => cb.onchange = () => { const i = Number(cb.dataset.ph); F.photos = cb.checked ? [...new Set([...F.photos, i])] : F.photos.filter(x => x !== i); drawNew(); });
    root.querySelectorAll('[data-cluster-index]').forEach(input => input.oninput = () => { F.clusters[Number(input.dataset.clusterIndex)] = input.value; F.error = ''; });
    root.querySelectorAll('[data-remove-cluster]').forEach(button => button.onclick = () => {
      if (F.clusters.length <= 1) { F.error = 'Оставьте хотя бы один кластер для теста.'; drawNew(); return; }
      F.clusters.splice(Number(button.dataset.removeCluster), 1); F.error = ''; drawNew();
    });
    const addCluster = root.querySelector('[data-add-cluster]');
    if (addCluster) addCluster.onclick = () => {
      if (F.clusters.length >= 50) { F.error = 'Можно добавить не больше 50 кластеров.'; drawNew(); return; }
      F.clusters.push(''); F.error = ''; drawNew();
      root.querySelector(`[data-cluster-index="${F.clusters.length - 1}"]`)?.focus();
    };
    root.querySelectorAll('[data-f]').forEach(el => el.onchange = () => { F[el.dataset.f] = el.type === 'number' ? Number(el.value) : el.value; if (el.dataset.f === 'mode') drawNew(); });
    const cr = root.querySelector('[data-create]');
    if (cr) cr.onclick = async () => {
      if (!F.advert) { alert('Выберите рекламную кампанию'); return; }
      F.clusters = F.clusters.map(x => x.trim()).filter(Boolean);
      if (!F.clusters.length) { F.error = 'Добавьте хотя бы один кластер.'; drawNew(); return; }
      cr.disabled = true;
      try { const t = await post('/api/ab/tests', {nm_id: Number(F.nm), advert_id: Number(F.advert), photos: F.photos, mode: F.mode, interval_hours: F.interval, views_per_step: F.views, metric: F.metric, max_days: F.days, clusters: F.clusters});
        st.form = null; openTest(t.id); }
      catch (e) { alert(e.message); cr.disabled = false; }
    };
    async function loadCard() {
      // Берём значение из DOM непосредственно перед запросом: браузер может
      // восстановить поле автозаполнением, не вызвав input-событие.
      const currentNm = root.querySelector('[data-nm]')?.value || F.nm;
      F.nm = String(currentNm).replace(/\D/g, '');
      if (!F.nm && F.advert) {
        const selected = visibleCamps.find(c => String(c.id) === String(F.advert));
        if (selected?.nms?.length === 1) F.nm = String(selected.nms[0]);
        else if (selected?.nms?.length > 1) {
          F.error = 'В компании несколько товаров — выберите артикул в списке справа или введите nmID вручную.';
          drawNew();
          return;
        }
      }
      if (!F.nm) { F.error = 'Введите nmID или укажите компанию с одним товаром.'; drawNew(); return; }
      F.error = '';
      F.busy = true; drawNew();
      try {
        F.card = await api('/api/ab/card?nm=' + F.nm); F.photos = [];
        const f2 = camps.filter(c => (!F.cabinet || c.account === F.cabinet) && c.nms.includes(Number(F.nm)));
        // Не перезаписываем выбранную/введённую компанию. Автовыбор — только если поле ещё пустое.
        if (!F.advert && f2.length) F.advert = f2[0].id;
      }
      catch (e) { alert(e.message); }
      F.busy = false; drawNew();
    }
  }

  // ---------------------------------------------------------------- тест
  async function openTest(id) {
    st.view = 'detail'; st.id = id;
    try { st.test = await api('/api/ab/tests/' + id); } catch (e) { root.innerHTML = `<p class="rv-error">${esc(e.message)}</p>`; return; }
    drawTest();
  }
  function drawTest() {
    const t = st.test, s = t.summary, best = Math.max(0.0001, ...t.variants.map(v => (t.metric === 'cart' ? v.ctr_cart : v.ctr) || 0));
    const modeText = t.mode === 'clusters' ? `по ${num(t.target_views_per_cluster || t.views_per_step)} показов на кластер` : t.mode === 'timer' ? `смена каждые ${num(t.interval_hours, 0)} ч` : `смена каждые ${num(t.views_per_step)} показов`;
    const actions = {draft: ['start', 'Запустить тест', 'btn'], running: ['pause', 'Пауза', 'db-ghost'], paused: ['start', 'Продолжить', 'btn'], auto_paused: ['pause', 'Пауза', 'db-ghost']};
    const a = actions[t.status];
    root.innerHTML = `<div class="db-head"><h2>${esc(t.article || t.nm_id)} <small class="muted">${t.nm_id}</small></h2><button type="button" class="db-ghost" data-back>← К списку</button></div>
      <div class="ab-top"><div>${chip(t.status)} <span class="muted">${esc(t.title || '')} · кампания ${t.advert_id} · ${esc(METRIC[t.metric])} · ${modeText} · до ${num(t.max_days)} дней</span>
          ${t.mode === 'clusters' ? `<div class="ab-cluster-note"><b>Кластеры:</b> ${(t.clusters || []).map(esc).join(' · ')}<br><span class="muted">Фото меняются по очереди. Тест завершится, когда каждый вариант получит целевой объём показов по каждому кластеру.</span></div>` : ''}
          ${t.pause_reason ? `<div class="db-note">${esc(t.pause_reason)}</div>` : ''}${t.next_switch ? `<div class="muted fb-sub">Следующая смена фото ${esc(t.next_switch)}</div>` : ''}</div>
        <div class="ab-actions">${a ? `<button type="button" class="${a[2]}" data-act="${a[0]}">${a[1]}</button>` : ''}
          ${['running', 'paused', 'auto_paused'].includes(t.status) ? '<button type="button" class="db-ghost" data-refresh>Обновить статистику</button>' : ''}
          ${t.status !== 'finished' && t.status !== 'draft' ? '<button type="button" class="db-ghost" data-finish>Завершить и поставить лучшее</button>' : ''}
          ${t.status !== 'running' ? '<button type="button" class="db-ghost ab-del" data-del>Удалить</button>' : ''}</div></div>
      ${t.status === 'draft' ? `<section class="tr-box ab-upload"><h3>Свои варианты фото</h3><p class="muted fb-sub">JPG, PNG или WEBP до 10 МБ, всего до 20 вариантов. При запуске они загрузятся в конец карточки, а во время теста по очереди станут главным фото.</p>
        <label class="btn ab-file">+ Добавить фото<input type="file" accept=".jpg,.jpeg,.png,.webp" multiple data-files hidden></label></section>` : ''}
      <div class="db-grid fb-grid">${[['Показы', num(s.views)], ['Клики', num(s.clicks)], ['CTR теста', pct(s.ctr)], ['Корзины', num(s.atbs)], ['Заказы', num(s.orders)]]
        .map(([k, v]) => `<article class="db-tile neutral"><div class="db-title">${k}</div><div class="db-value">${v}</div></article>`).join('')}</div>
      ${s.lift != null ? `<div class="pn-check ${s.lift > 0 ? 'ok' : 'warn'}">${s.lift > 0 ? '↑' : '•'} CTR контроля ${pct(s.control_ctr)} → лучший вариант ${pct(s.best_ctr)} (${s.lift > 0 ? '+' : ''}${num(s.lift, 1)}%)</div>` : ''}
      <div class="ab-variants">${t.variants.map(v => { const val = (t.metric === 'cart' ? v.ctr_cart : v.ctr) || 0; return `<article class="ab-var ${v.excluded ? 'out' : ''} ${v.is_current ? 'cur' : ''} ${v.is_winner ? 'win' : ''}">
        <div class="ab-img">${img(v) ? `<img src="${esc(img(v))}" alt="${esc(v.label)}" loading="lazy">` : '<i>нет фото</i>'}
          ${v.is_winner ? '<em class="w">Победитель</em>' : v.is_current && t.status !== 'finished' ? '<em>Сейчас главное</em>' : v.excluded ? '<em class="x">Исключён</em>' : ''}</div>
        <b>${esc(v.label)}</b>
        <div class="ab-bar"><i style="width:${val / best * 100}%"></i></div>
        <table class="fb-mini"><tr><td>Показы</td><td class="r">${num(v.views)}</td></tr><tr><td>Клики · CTR</td><td class="r">${num(v.clicks)} · <b>${pct(v.ctr)}</b></td></tr>
          <tr><td>Корзины · конверсия</td><td class="r">${num(v.atbs)} · ${pct(v.cart_cr, 1)}</td></tr><tr><td>CTR × корзина</td><td class="r">${pct(v.ctr_cart, 3)}</td></tr>
          <tr><td>Заказы</td><td class="r">${num(v.orders)}</td></tr><tr><td>Был главным</td><td class="r">${num(v.shown_hours, 1)} ч · ${v.turns} раз</td></tr></table>
        <div class="ab-cluster-progress"><b>Кластеры и ключевые фразы</b><small class="ab-cluster-help">Показы, клики и CTR по всем фразам, которые получили показы${t.mode === 'clusters' ? '; выбранные для смены фото идут первыми.' : '.'}</small>${(v.cluster_progress || []).length ? (v.cluster_progress || []).map(p => `<div class="ab-cluster-line"><span title="${esc(p.cluster)}">${esc(p.cluster)}${p.configured ? '' : ' <em class="ab-cluster-observed">получил показы</em>'}</span><span>${p.configured ? `${num(p.views)} / ${num(p.target)}${p.done ? ' ✓' : ''}` : `${num(p.views)} показов`}</span><small>${num(p.clicks)} кликов · ${p.ctr == null ? 'CTR —' : `CTR ${pct(p.ctr)}`}</small></div>`).join('') : '<small class="ab-cluster-empty">Пока нет данных по кластерам. Нажмите «Обновить статистику» после появления показов.</small>'}</div>
        <small class="muted">${v.excluded ? esc(v.excluded_reason || '') : v.confidence_worse != null ? `Вероятность, что хуже лидера: ${num(v.confidence_worse, 0)}%` : v.needs_views ? `До вердикта ещё ${num(v.needs_views)} показов` : ''}</small>
        <div class="ab-vact">${t.status === 'draft' && v.position > 0 ? `<button type="button" class="db-ghost" data-rm="${v.id}">Убрать</button>` : ''}
          ${['running', 'paused', 'auto_paused'].includes(t.status) && !v.excluded ? `<button type="button" class="db-ghost" data-ex="${v.id}">Исключить</button><button type="button" class="db-ghost" data-win="${v.id}">Сделать победителем</button>` : ''}</div>
      </article>`; }).join('')}</div>
      <section class="tr-box"><h3>Журнал</h3><div class="ab-log">${t.log.map(l => `<div><span class="muted">${dt(l.at)}</span> ${esc(l.text)}</div>`).join('') || '<p class="muted">Пусто.</p>'}</div></section>
      <p class="muted rp-note">${t.mode === 'clusters' ? 'По каждому кластеру сохраняются отдельные показы, клики и CTR. После завершения целевого объёма победитель определяется по суммарному CTR выбранных кластеров.' : 'Вариант исключается, когда с уверенностью ≥95% видно, что он хуже лидера, и у него не меньше 1000 показов.'} Если кампания остановится, тест встанет на паузу и продолжится с того же места.
        По завершении лучшее фото станет главным, а ваши загруженные фото-проигравшие будут убраны из карточки.</p>`;
    root.querySelector('[data-back]').onclick = () => { st.view = 'list'; st.id = null; show(); };
    const act = (url, body, confirmText) => async e => {
      if (confirmText && !confirm(confirmText)) return;
      e.currentTarget.disabled = true;
      try { st.test = await post(url, body); drawTest(); } catch (err) { alert(err.message); e.currentTarget.disabled = false; }
    };
    root.querySelectorAll('[data-act]').forEach(b => b.onclick = act(`/api/ab/tests/${t.id}/${b.dataset.act}`, {},
      b.dataset.act === 'start' && t.status === 'draft' ? 'Запустить тест? Панель начнёт менять главное фото этой карточки на WB, свои фото будут загружены в карточку.' : null));
    const refresh = root.querySelector('[data-refresh]');
    if (refresh) refresh.onclick = act(`/api/ab/tests/${t.id}/refresh`, {}, null);
    const fin = root.querySelector('[data-finish]');
    if (fin) fin.onclick = act(`/api/ab/tests/${t.id}/finish`, {remove_uploaded: true}, 'Завершить тест и поставить главным лучший вариант?');
    root.querySelectorAll('[data-win]').forEach(b => b.onclick = act(`/api/ab/tests/${t.id}/finish`, {winner_id: Number(b.dataset.win), remove_uploaded: true}, 'Завершить тест и поставить это фото главным?'));
    root.querySelectorAll('[data-ex]').forEach(b => b.onclick = act(`/api/ab/tests/${t.id}/variants/${b.dataset.ex}/exclude`, {}, 'Исключить вариант из теста?'));
    root.querySelectorAll('[data-rm]').forEach(b => b.onclick = async () => { try { st.test = await api(`/api/ab/tests/${t.id}/variants/${b.dataset.rm}`, {method: 'DELETE'}); drawTest(); } catch (e) { alert(e.message); } });
    const del = root.querySelector('[data-del]');
    if (del) del.onclick = async () => { if (!confirm('Удалить тест и его статистику? Фото в карточке не изменятся.')) return;
      try { await api(`/api/ab/tests/${t.id}`, {method: 'DELETE'}); st.view = 'list'; st.id = null; show(); } catch (e) { alert(e.message); } };
    const files = root.querySelector('[data-files]');
    if (files) files.onchange = async () => {
      for (const f of files.files) {
        const fd = new FormData(); fd.append('file', f);
        try { st.test = await api(`/api/ab/tests/${t.id}/upload`, {method: 'POST', body: fd}); } catch (e) { alert(`${f.name}: ${e.message}`); }
      }
      drawTest();
    };
  }

  const style = document.createElement('style');
  style.textContent = `
.ab-page{container-type:inline-size}.ab-how{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:6px 0 16px}.ab-how div{display:flex;gap:10px;border:1px solid #e5e7eb;border-radius:12px;padding:12px;background:#fff}
.ab-form-error{margin:-4px 0 12px;padding:10px 12px;border-radius:9px;background:#fff1f2;border:1px solid #fecdd3;color:#b42318;font-size:13px;line-height:1.45}
.ab-how b{flex:none;width:28px;height:28px;border-radius:50%;background:#ede9fe;color:#5b21b6;display:grid;place-items:center}.ab-how span{font-size:13px;color:#4b5563}.ab-how strong{display:block;color:#111827}
.ab-st{display:inline-flex;align-items:center;gap:5px;font-weight:700;font-size:12px;color:var(--c)}.ab-st::before{content:'';width:8px;height:8px;border-radius:50%;background:var(--c)}
.ab-table td small{display:block;font-size:11px;color:#6b7280;font-weight:400}.ab-prod{display:flex;gap:10px;align-items:center;min-width:240px}
.ab-prod img,.ab-prod i{width:42px;height:56px;object-fit:cover;border-radius:6px;background:#f1f2f4;flex:none}
.ab-row{display:grid;gap:12px;align-items:start;margin-bottom:12px}.ab-row label{display:grid;gap:5px;min-width:0;font-size:13px;font-weight:600}.ab-row input,.ab-row select{width:100%;min-width:0;height:40px;border:1px solid #e3e5ea;border-radius:9px;padding:0 10px;font:inherit}.ab-row label small{font-weight:400;line-height:1.3}.ab-product-row{grid-template-columns:minmax(210px,1.1fr) minmax(170px,.85fr) minmax(170px,.85fr) minmax(230px,1.2fr) auto}.ab-product-row .btn{height:40px;margin-top:22px;white-space:nowrap}.ab-settings-main{grid-template-columns:minmax(170px,.85fr) minmax(170px,.85fr) minmax(290px,1.7fr) minmax(170px,.95fr) minmax(150px,.85fr)}.ab-settings-extra{grid-template-columns:minmax(170px,.75fr) minmax(360px,2fr) minmax(150px,.75fr)}
.ab-cluster-editor{display:grid;gap:7px}.ab-cluster-row{display:grid;grid-template-columns:minmax(0,1fr) 32px;gap:7px}.ab-cluster-row input{height:36px}.ab-cluster-remove{height:36px;border:1px solid #e3e5ea;border-radius:9px;background:#fff;color:#9f1239;font-size:20px;line-height:1;cursor:pointer}.ab-cluster-remove:hover{background:#fff1f2;border-color:#fda4af}.ab-cluster-add{justify-self:start;height:32px;padding:5px 10px;font-size:12px}
.ab-cluster-help{display:block;margin:3px 0 5px;color:#6b7280;font-weight:400;line-height:1.35}.ab-cluster-observed{font-style:normal;color:#7c3aed;font-size:10px;font-weight:600;white-space:nowrap}
.ab-retro-hint{display:block;margin-top:2px;color:#7c3aed!important}
.ab-photos{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px}.ab-ph{border:2px solid #e5e7eb;border-radius:12px;overflow:hidden;cursor:pointer;background:#fff}
.ab-ph.on{border-color:#7c3aed}.ab-ph img{width:100%;aspect-ratio:3/4;object-fit:cover;display:block}.ab-ph span{display:flex;gap:6px;align-items:center;padding:6px 8px;font-size:12px}
.ab-top{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:12px}.ab-actions{display:flex;gap:8px;flex-wrap:wrap}.ab-del{color:#b42323}
.ab-file{display:inline-flex;cursor:pointer}
.ab-variants{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px;margin:14px 0}
.ab-var{border:1px solid #e5e7eb;border-radius:14px;padding:10px;background:#fff;display:flex;flex-direction:column;gap:8px}
.ab-var.cur{border-color:#7c3aed;box-shadow:0 0 0 2px #ede9fe}.ab-var.win{border-color:#2f9e44;box-shadow:0 0 0 2px #e3f3e5}.ab-var.out{opacity:.55}
.ab-img{position:relative}.ab-img img{width:100%;aspect-ratio:3/4;object-fit:cover;border-radius:10px;display:block;background:#f1f2f4}.ab-img i{display:grid;place-items:center;aspect-ratio:3/4;background:#f1f2f4;border-radius:10px;color:#9ca3af}
.ab-img em{position:absolute;left:8px;top:8px;background:#7c3aed;color:#fff;font-style:normal;font-size:11px;font-weight:700;padding:3px 8px;border-radius:999px}
.ab-img em.w{background:#2f9e44}.ab-img em.x{background:#868e96}
.ab-bar{height:6px;background:#f1f2f4;border-radius:3px;overflow:hidden}.ab-bar i{display:block;height:100%;background:#7c3aed}
.ab-vact{display:flex;gap:6px;flex-wrap:wrap}.ab-vact .db-ghost{font-size:12px;padding:5px 9px}
.ab-log{max-height:260px;overflow:auto;font-size:13px;display:grid;gap:4px}
.ab-cluster-note{margin-top:8px;padding:9px 11px;border-radius:10px;background:#f8f7ff;border:1px solid #e9e4ff;font-size:12px;line-height:1.55}.ab-cluster-progress{margin-top:4px;padding-top:8px;border-top:1px solid #f0f1f5;display:grid;gap:5px}.ab-cluster-progress>b{font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em}.ab-cluster-line{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:3px 8px;font-size:11px;line-height:1.25}.ab-cluster-line span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#374151}.ab-cluster-line span:nth-child(2){font-variant-numeric:tabular-nums;color:#111827}.ab-cluster-line small{grid-column:1/-1;color:#6b7280}
@container (max-width:1100px){.ab-product-row,.ab-settings-main{grid-template-columns:repeat(2,minmax(0,1fr))}.ab-product-row .btn{justify-self:start}.ab-settings-extra{grid-template-columns:minmax(150px,.8fr) minmax(260px,1.5fr) minmax(130px,.7fr)}}@media(max-width:900px){.ab-how{grid-template-columns:1fr 1fr}.ab-top{flex-direction:column}}@container (max-width:650px){.ab-product-row,.ab-settings-main,.ab-settings-extra{grid-template-columns:1fr}.ab-product-row .btn{width:100%;margin-top:0}}`;
  document.head.appendChild(style);

  return {render};
})();
