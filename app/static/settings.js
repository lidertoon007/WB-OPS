/* WB OPS: «Параметры и настройки» — кабинеты маркетплейсов, учётная запись, уведомления, команда. */
window.WBOPSSettings = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const MP = {
    wildberries: {title: 'Wildberries', cls: 'wb', token: 'API-токен', tokenHint: 'ЛК Wildberries → Профиль → Настройки → Доступ к API → «Создать токен». Категории: «Вопросы и отзывы» (и «Чат с покупателями»), уровень доступа — чтение и запись. Для РНП добавьте категорию «Статистика».',
      adsHint: 'Отдельный токен WB с категорией «Продвижение» (достаточно «только чтение»). Нужен РНП для показов, кликов, CTR и расходов на рекламу. Если не задан — используется основной ключ.'},
    ozon: {title: 'Ozon', cls: 'ozon', token: 'API-ключ', client: 'Client ID', tokenHint: 'ЛК Ozon Seller → Настройки → API-ключи → «Сгенерировать ключ» (роль Admin или «Отзывы и вопросы»). Client ID — на той же странице.', adsHint: 'Ключ Performance API рекламного кабинета Ozon. Сохраняется заранее — подключим, когда появится загрузка Ozon.'},
    yandex: {title: 'Яндекс.Маркет', cls: 'ym', token: 'API-ключ (Api-Key)', client: 'ID кабинета', tokenHint: 'Кабинет Маркета → Настройки → API и модули → «Создать токен» с доступом к отзывам и чатам. ID кабинета — в адресе страницы кабинета.', adsHint: 'Отдельный ключ для статистики продвижения, если используете. Сохраняется заранее.'},
  };
  const COUNTRIES = {RU: ['Россия', 'RUB', 'Российский рубль'], BY: ['Беларусь', 'BYN', 'Белорусский рубль'], KZ: ['Казахстан', 'KZT', 'Казахстанский тенге'],
    AM: ['Армения', 'AMD', 'Армянский драм'], KG: ['Киргизия', 'KGS', 'Киргизский сом'], UZ: ['Узбекистан', 'UZS', 'Узбекский сум']};
  const dateTime = s => {
    if (!s) return '—';
    const d = new Date(s);
    return `${d.toLocaleDateString('ru-RU', {day: 'numeric', month: 'long', year: 'numeric'}).replace(' г.', '')}, ${d.toLocaleTimeString('ru-RU', {hour: '2-digit', minute: '2-digit'})}`;
  };
  let me = {role: 'owner'};

  async function api(url, options = {}) {
    if (options.body && typeof options.body !== 'string') {
      options = {...options, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(options.body)};
    }
    const r = await fetch(url, options);
    let d = null;
    try { d = await r.json(); } catch (e) { /* пусто */ }
    if (!r.ok) throw new Error((d && (typeof d.detail === 'string' ? d.detail : 'Проверьте поля формы')) || `Ошибка ${r.status}`);
    return d;
  }
  function toast(text) {
    const t = document.getElementById('toast');
    if (!t) return;
    t.textContent = text; t.style.display = 'block';
    clearTimeout(toast.timer); toast.timer = setTimeout(() => t.style.display = 'none', 3500);
  }
  const toggle = (attrs, on, label, disabled = false) =>
    `<button type="button" role="switch" aria-checked="${on}" aria-label="${esc(label)}" class="st-switch${on ? ' on' : ''}" ${attrs} ${disabled ? 'disabled' : ''}><i></i></button>`;
  async function copy(text, button) {
    try { await navigator.clipboard.writeText(text); }
    catch (e) {
      const ta = document.createElement('textarea'); ta.value = text; document.body.append(ta); ta.select();
      try { document.execCommand('copy'); } catch (x) { /* ignore */ } ta.remove();
    }
    if (button) { const old = button.innerHTML; button.innerHTML = '✓'; setTimeout(() => button.innerHTML = old, 1200); }
    toast('Скопировано');
  }

  // ======================================================================== Кабинеты
  const cab = {list: [], filter: 'all', open: null, adding: null};

  async function renderCabinets(section) {
    section.className = 'panel st-page';
    section.dataset.cabinetReady = '1';
    section.innerHTML = '<p class="muted">Загрузка…</p>';
    try { cab.list = await api('/api/cabinets'); } catch (e) { section.innerHTML = `<p class="st-error">${esc(e.message)}</p>`; return; }
    drawCabinets(section);
  }

  function drawCabinets(section) {
    const owner = me.role === 'owner';
    const rows = cab.list.filter(c => cab.filter === 'all' || c.marketplace === cab.filter);
    section.innerHTML = `<div class="st-head"><h2>Кабинеты маркетплейсов</h2>
        <button type="button" class="st-help-btn" data-help aria-expanded="false">ⓘ Как подключить кабинет маркетплейса <span>›</span></button></div>
      <div class="st-help" hidden>${Object.values(MP).map(m => `<p><b>${m.title}.</b> ${esc(m.tokenHint)}</p>`).join('')}
        <p class="muted">Токены и ключи хранятся только в базе WB OPS на этом компьютере и не показываются в интерфейсе. Загрузка отзывов и вопросов сейчас работает для Wildberries; кабинеты Ozon и Яндекс.Маркета можно добавить заранее.</p></div>
      ${owner ? `<p class="st-label">Добавить кабинет:</p><div class="st-add">${Object.entries(MP).map(([k, m]) => `<button type="button" class="st-add-btn ${m.cls}" data-add="${k}">+ ${m.title}</button>`).join('')}</div>
      <div data-add-form></div>` : '<p class="muted">Подключать кабинеты и менять настройки может только владелец панели.</p>'}
      <div class="st-box"><div class="st-tabs">${[['all', 'Все'], ['wildberries', 'Wildberries'], ['ozon', 'Ozon'], ['yandex', 'Yandex Market']].map(([k, t]) =>
        `<button type="button" data-filter="${k}" class="${cab.filter === k ? 'on' : ''}">${t}</button>`).join('')}</div>
      <div class="st-table-wrap"><table class="st-table"><thead><tr><th>Кабинет</th><th class="c">Отзывы</th><th class="c">Вопросы</th><th class="c">✉ Чаты</th><th></th></tr></thead><tbody>
      ${rows.map(c => `<tr data-row="${esc(c.id)}" class="${c.enabled === false ? 'st-disabled' : ''}"><td><span class="st-badge ${MP[c.marketplace]?.cls || ''}">${esc(MP[c.marketplace]?.title || c.marketplace)}</span> <b>${esc(c.name)}</b>${c.enabled === false ? ' <span class="st-off">выключен</span>' : ''}
          <div class="muted st-legal">${[c.country && c.country !== 'RU' ? esc(`${c.country_title} · ${c.currency}`) : '', c.legal_name ? esc(c.legal_name) : '', c.tax_effective != null ? `налог ${String(c.tax_effective).replace('.', ',')}%${c.tax_percent == null ? ' (как в ОПиУ)' : ''}` : '', c.token_configured ? '' : '<span class="st-warn">нет ключа</span>', c.supported ? '' : 'загрузка пока не поддерживается'].filter(Boolean).join(' · ')}</div></td>
        ${['reviews', 'questions', 'chats'].map(k => `<td class="c">${toggle(`data-channel="${k}" data-id="${esc(c.id)}"`, c[k], `${k} ${c.name}`, !owner)}</td>`).join('')}
        <td class="r">${owner ? `<button type="button" class="st-icon" data-edit="${esc(c.id)}" aria-label="Настроить ${esc(c.name)}" title="Настроить">⚙</button>` : ''}</td></tr>
        ${cab.open === c.id ? `<tr class="st-edit-row"><td colspan="5">${cabinetForm(c.marketplace, c)}</td></tr>` : ''}`).join('')
        || `<tr><td colspan="5" class="muted st-empty">${cab.list.length ? 'В этой группе кабинетов нет.' : 'Кабинеты ещё не добавлены — нажмите «+ Wildberries».'}</td></tr>`}
      </tbody></table></div></div>
      <p class="muted st-note">Отзывы и Вопросы — что загружать и обрабатывать в кабинете при синхронизации. Чаты — отмечает кабинеты, где ведём чаты с покупателями (загрузка чатов из маркетплейсов появится позже).</p>`;
    bindCabinets(section);
  }

  function taxHint(c) {
    const base = 'Налог считается от суммы, которую оплатили покупатели, — так же в «Финансах» (ОПиУ), «Дашборде» и «РНП». Пустое поле — ставка из настроек ОПиУ.';
    if (!c) return base + ' После сохранения ставка запишется в расчёт ОПиУ, если там есть этот кабинет.';
    if (c.tax_opiu_account) return `${base} Ставка записывается в расчёт ОПиУ для кабинета «${c.tax_opiu_account}».`;
    if (c.tax_opiu_found === false) return base + ' Папка расчёта ОПиУ не найдена — ставка пока хранится только в панели.';
    return base + ' В расчёте ОПиУ этот кабинет не найден — ставка хранится в панели и запишется, когда кабинет появится в ОПиУ.';
  }

  function cabinetForm(mp, c = null) {
    const m = MP[mp];
    return `<form class="st-form" data-cab-form="${esc(c ? c.id : '')}" data-mp="${mp}">
      <h3>${c ? 'Настройки кабинета' : `Новый кабинет ${m.title}`}</h3>
      <div class="st-grid">
        <label>Название кабинета<input name="name" required maxlength="160" value="${esc(c?.name || '')}" placeholder="${m.title === 'Wildberries' ? 'WB ИП' : m.title + ' ИП'}"></label>
        <label>Юрлицо<input name="legal_name" maxlength="200" value="${esc(c?.legal_name || '')}" placeholder="ООО «Ромашка»"></label>
        <div class="st-field"><span>Статус</span><div class="st-seg" role="radiogroup" aria-label="Статус кабинета">
          <label><input type="radio" name="enabled" value="1" ${c?.enabled === false ? '' : 'checked'}><span>Включен</span></label>
          <label><input type="radio" name="enabled" value="0" ${c?.enabled === false ? 'checked' : ''}><span>Выключен</span></label></div></div>
        <label>Страна регистрации<select name="country">${Object.entries(COUNTRIES).map(([k, [t, , cur]]) => `<option value="${k}" ${(c?.country || 'RU') === k ? 'selected' : ''}>${t} / ${cur}</option>`).join('')}</select></label>
        ${m.client ? `<label class="wide">${m.client}<input name="client_id" maxlength="100" value="${esc(c?.client_id || '')}" autocomplete="off"></label>` : ''}
        <label>Ключ API<input name="token" type="password" maxlength="4000" autocomplete="new-password" placeholder="${c?.token_configured ? 'Сохранён — вставьте новый, чтобы заменить' : 'Вставьте ключ'}" ${!c && mp === 'wildberries' ? 'required' : ''}></label>
        <label>Ключ API реклама<input name="ads_token" type="password" maxlength="4000" autocomplete="new-password" placeholder="${c?.ads_token_configured ? 'Сохранён — вставьте новый, чтобы заменить' : 'Необязательно'}"></label>
        <label class="wide">Текущая ставка налога, %<span class="st-tax"><input name="tax_percent" type="number" min="0" max="100" step="0.1" inputmode="decimal"
          value="${c?.tax_percent ?? ''}" placeholder="${c?.tax_opiu_rate != null ? `${String(c.tax_opiu_rate).replace('.', ',')} — как в ОПиУ` : 'например, 6'}">
          <span class="st-tax-chips">${[[0, 'Без налога'], [1, '1%'], [6, 'УСН 6%'], [15, '15%']].map(([v, t]) => `<button type="button" data-tax="${v}">${t}</button>`).join('')}</span></span></label>
      </div>
      <p class="muted st-hint"><b>Ключ API.</b> ${esc(m.tokenHint)}${mp === 'wildberries' ? '' : ' Загрузка отзывов из этой площадки появится позже — кабинет сохранится и подключится автоматически.'}</p>
      <p class="muted st-hint"><b>Ключ API реклама.</b> ${esc(m.adsHint)}</p>
      <p class="muted st-hint"><b>Налог.</b> ${esc(taxHint(c))}</p>
      <p class="muted st-hint"><b>Статус.</b> Выключенный кабинет не загружает отзывы и вопросы и не попадает в РНП; настройки и ключи сохраняются.</p>
      <div class="st-actions"><button class="btn" type="submit">${c ? 'Сохранить' : 'Добавить кабинет'}</button>
        <button class="btn secondary" type="button" data-cancel>Отмена</button>
        ${c && c.supported ? '<button class="btn secondary" type="button" data-sync>↻ Загрузить сейчас</button>' : ''}
        ${c ? '<button class="btn secondary st-danger" type="button" data-delete>Удалить кабинет</button>' : ''}
        <span class="muted" data-state role="status"></span></div></form>`;
  }

  function bindCabinets(section) {
    const help = section.querySelector('[data-help]');
    help.onclick = () => { const box = section.querySelector('.st-help'); box.hidden = !box.hidden; help.setAttribute('aria-expanded', String(!box.hidden)); };
    section.querySelectorAll('[data-filter]').forEach(b => b.onclick = () => { cab.filter = b.dataset.filter; drawCabinets(section); });
    section.querySelectorAll('[data-add]').forEach(b => b.onclick = () => {
      cab.open = null;
      const host = section.querySelector('[data-add-form]');
      host.innerHTML = cabinetForm(b.dataset.add);
      bindForm(section, host.querySelector('form'), null);
      host.querySelector('input').focus();
    });
    section.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => { cab.open = cab.open === b.dataset.edit ? null : b.dataset.edit; drawCabinets(section); });
    section.querySelectorAll('[data-channel]').forEach(sw => sw.onclick = async () => {
      const on = sw.getAttribute('aria-checked') !== 'true';
      sw.disabled = true;
      try {
        const updated = await api(`/api/cabinets/${encodeURIComponent(sw.dataset.id)}/channels`, {method: 'PATCH', body: {[sw.dataset.channel]: on}});
        cab.list = cab.list.map(c => c.id === updated.id ? updated : c);
        sw.classList.toggle('on', on); sw.setAttribute('aria-checked', String(on));
      } catch (e) { toast(e.message); }
      sw.disabled = false;
    });
    const edit = section.querySelector('.st-edit-row form');
    if (edit) bindForm(section, edit, cab.list.find(c => c.id === cab.open));
  }

  function bindForm(section, form, current) {
    const state = form.querySelector('[data-state]');
    form.querySelectorAll('[data-tax]').forEach(b => b.onclick = () => { form.elements['tax_percent'].value = b.dataset.tax; form.elements['tax_percent'].focus(); });
    form.querySelector('[data-cancel]').onclick = () => { cab.open = null; form.closest('[data-add-form]') ? form.remove() : drawCabinets(section); };
    form.onsubmit = async e => {
      e.preventDefault();
      const tax = form.elements['tax_percent'].value.trim().replace(',', '.');
      if (tax !== '' && !(Number(tax) >= 0 && Number(tax) <= 100)) { state.textContent = 'Ставка налога — число от 0 до 100'; return; }
      const body = {name: form.elements['name'].value.trim(), legal_name: form.legal_name.value.trim(), token: form.token.value.trim(), marketplace: form.dataset.mp,
                    client_id: form.client_id ? form.client_id.value.trim() : '', tax_percent: tax === '' ? null : Number(tax),
                    ads_token: form.elements['ads_token'].value.trim(), country: form.elements['country'].value,
                    enabled: form.querySelector('[name=enabled]:checked')?.value !== '0'};
      const button = form.querySelector('[type=submit]');
      button.disabled = true; state.textContent = 'Сохранение…';
      try {
        if (current) await api(`/api/cabinets/${encodeURIComponent(current.id)}`, {method: 'PUT', body: {...body, id: current.id}});
        else await api('/api/cabinets', {method: 'POST', body});
        cab.open = null;
        cab.list = await api('/api/cabinets');
        drawCabinets(section);
        toast(current ? 'Кабинет сохранён' : 'Кабинет добавлен');
      } catch (err) { state.textContent = err.message; button.disabled = false; }
    };
    const del = form.querySelector('[data-delete]');
    if (del) del.onclick = async () => {
      if (del.dataset.armed !== '1') { del.dataset.armed = '1'; del.textContent = 'Удалить? Загруженные отзывы останутся'; return; }
      try { await api(`/api/cabinets/${encodeURIComponent(current.id)}`, {method: 'DELETE'}); cab.open = null; cab.list = await api('/api/cabinets'); drawCabinets(section); toast('Кабинет удалён'); }
      catch (err) { state.textContent = err.message; }
    };
    const sync = form.querySelector('[data-sync]');
    if (sync) sync.onclick = async () => {
      sync.disabled = true; state.textContent = 'Загружаю из Wildberries…';
      try { const r = await api(`/api/sync/${encodeURIComponent(current.id)}`, {method: 'POST'}); state.textContent = `Получено: ${r.received}`; }
      catch (err) { state.textContent = err.message; }
      sync.disabled = false;
    };
  }

  // ======================================================================== Учётная запись
  async function renderAccount(host) {
    if (!host) return;
    if (me.role === 'member') {
      host.innerHTML = `<div class="st-card"><h3>Учётная запись</h3><p>Вы вошли как <b>${esc(me.member.name)}</b>${me.member.email ? ` (${esc(me.member.email)})` : ''} — сотрудник команды.</p>
        <button class="btn secondary" type="button" data-logout>Выйти</button></div>`;
      host.querySelector('[data-logout]').onclick = logout;
      return;
    }
    let a = {name: '', email: ''};
    try { a = await api('/api/account'); } catch (e) { /* пусто */ }
    host.innerHTML = `<form class="st-card st-form" data-account><h3>Учётная запись владельца</h3>
      <p class="muted">Имя и email показываются автором приглашений в команду. Email также подставляется как адрес для уведомлений.</p>
      <div class="st-grid"><label>Имя<input name="name" maxlength="80" value="${esc(a.name)}" autocomplete="name"></label>
      <label>Email<input name="email" type="email" maxlength="120" value="${esc(a.email)}" autocomplete="email"></label></div>
      <div class="st-actions"><button class="btn" type="submit">Сохранить</button><span class="muted" data-state role="status"></span></div></form>`;
    const f = host.querySelector('form');
    f.onsubmit = async e => {
      e.preventDefault();
      try { await api('/api/account', {method: 'PUT', body: {name: f.elements['name'].value, email: f.email.value}}); f.querySelector('[data-state]').textContent = 'Сохранено'; }
      catch (err) { f.querySelector('[data-state]').textContent = err.message; }
    };
  }
  async function logout() {
    try { await api('/api/team/logout', {method: 'POST'}); } catch (e) { /* ignore */ }
    location.href = '/join';
  }

  // ======================================================================== Уведомления
  let notif = null;
  async function renderNotifications(section) {
    section.className = 'panel st-page';
    section.innerHTML = '<p class="muted">Загрузка…</p>';
    try { notif = await api('/api/notifications'); } catch (e) { section.innerHTML = `<p class="st-error">${esc(e.message)}</p>`; return; }
    drawNotifications(section);
  }

  function drawNotifications(section) {
    const n = notif, tg = n.telegram, em = n.email, st = n.status || {};
    const status = (ch, title) => {
      const s = st[ch];
      if (!n[ch].enabled) return `<span class="st-chip">выключено</span>`;
      if (!s) return `<span class="st-chip">не проверено</span>`;
      return s.ok ? `<span class="st-chip ok">✓ ${title}</span>` : `<span class="st-chip bad" title="${esc(s.error)}">ошибка отправки</span>`;
    };
    const evRow = (key, title, sub) => `<div class="st-ev"><div><b>${title}</b><span>${sub}</span></div>${toggle(`data-ev="${key}"`, n.events[key], title)}</div>`;
    const banner = tg.enabled && st.telegram?.ok ? '<div class="st-banner">✓ Telegram бот успешно привязан</div>'
      : em.enabled && st.email?.ok ? '<div class="st-banner">✓ Почта для уведомлений подключена</div>' : '';
    section.innerHTML = `<h2>Настройки уведомлений</h2>
      <section class="st-card"><div class="st-card-head"><span class="st-ico">🔔</span><div><h3>Способы оповещения</h3><p>Получайте уведомления прямо в мессенджер или на почту</p></div></div>
        ${banner}
        <details class="st-channel" ${tg.enabled || !em.enabled ? 'open' : ''}><summary><b>Telegram</b> ${status('telegram', 'привязан')}</summary>
          <form class="st-form" data-channel-form="telegram">
            <ol class="st-steps"><li>Создайте бота в Telegram у <b>@BotFather</b> (команда /newbot) и скопируйте токен.</li>
              <li>Откройте своего бота и нажмите «Старт» (или добавьте его в рабочую группу).</li><li>Вставьте токен и нажмите «Найти чат».</li></ol>
            <div class="st-grid"><label>Токен бота<input name="bot_token" type="password" autocomplete="off" placeholder="${tg.bot_token_set ? 'Сохранён — вставьте новый, чтобы заменить' : '123456789:AA…'}"></label>
              <label>Chat ID<span class="st-inline"><input name="chat_id" value="${esc(tg.chat_id)}" placeholder="например, 123456789"><button type="button" class="btn secondary" data-detect>Найти чат</button></span></label></div>
            <div data-chats></div>
            <label class="st-check"><input type="checkbox" name="enabled" ${tg.enabled ? 'checked' : ''}> Отправлять уведомления в Telegram</label>
            <div class="st-actions"><button class="btn" type="submit">Сохранить</button><button class="btn secondary" type="button" data-test="telegram">Отправить тест</button><span class="muted" data-state role="status"></span></div>
          </form></details>
        <details class="st-channel" ${em.enabled ? 'open' : ''}><summary><b>Email</b> ${status('email', 'подключена')}</summary>
          <form class="st-form" data-channel-form="email">
            <p class="muted">Письма отправляются через вашу почту по SMTP. Для Яндекса, Gmail и Mail.ru нужен <b>пароль приложения</b> (создаётся в настройках безопасности почты), обычный пароль не подойдёт.</p>
            <div class="st-presets">Быстро заполнить: ${[['Яндекс', 'smtp.yandex.ru'], ['Gmail', 'smtp.gmail.com'], ['Mail.ru', 'smtp.mail.ru']].map(([t, h]) => `<button type="button" class="st-link" data-preset="${h}">${t}</button>`).join(' · ')}</div>
            <div class="st-grid"><label>Сервер SMTP<input name="host" value="${esc(em.host)}" placeholder="smtp.yandex.ru"></label>
              <label>Порт и защита<span class="st-inline"><input name="port" type="number" min="1" max="65535" value="${esc(em.port)}" style="max-width:110px">
                <select name="security">${[['ssl', 'SSL'], ['starttls', 'STARTTLS'], ['none', 'Без шифрования']].map(([v, t]) => `<option value="${v}" ${em.security === v ? 'selected' : ''}>${t}</option>`).join('')}</select></span></label>
              <label>Логин (адрес почты)<input name="username" value="${esc(em.username)}" autocomplete="off"></label>
              <label>Пароль приложения<input name="password" type="password" autocomplete="new-password" placeholder="${em.password_set ? 'Сохранён — введите новый, чтобы заменить' : ''}"></label>
              <label>Отправитель<input name="sender" value="${esc(em.sender)}" placeholder="как логин"></label>
              <label>Кому отправлять<input name="to" value="${esc(em.to)}" placeholder="адрес, можно несколько через запятую"></label></div>
            <label class="st-check"><input type="checkbox" name="enabled" ${em.enabled ? 'checked' : ''}> Отправлять уведомления на почту</label>
            <div class="st-actions"><button class="btn" type="submit">Сохранить</button><button class="btn secondary" type="button" data-test="email">Отправить тест</button><span class="muted" data-state role="status"></span></div>
          </form></details>
      </section>
      <section class="st-card"><div class="st-card-head"><span class="st-ico">⭐</span><div><h3>Отзывы</h3><p>Уведомления о новых отзывах и ответах</p></div></div>
        ${evRow('reviews_new', 'Оповещать о новых отзывах', 'Получать уведомления, когда покупатели оставляют новые отзывы.')}
        ${evRow('reviews_answered', 'Оповещать об ответах на отзывы', 'Получать уведомление, когда ИИ, сборщик или менеджер отправил ответ на отзыв.')}
        ${evRow('reviews_negative_only', 'Только негативные отзывы', 'Уведомлять только об отзывах с оценкой 1–3 звезды.')}</section>
      <section class="st-card"><div class="st-card-head"><span class="st-ico">❓</span><div><h3>Вопросы покупателей</h3><p>Уведомления о новых вопросах и статусах ответов</p></div></div>
        ${evRow('questions_new', 'Оповещать о новых вопросах', 'Получать уведомления, когда появляется новый вопрос от покупателя.')}
        ${evRow('questions_status', 'Оповещать о статусах ответов', 'Ответ на вопрос отправлен или черновик ждёт проверки.')}</section>
      <section class="st-card"><div class="st-card-head"><span class="st-ico">✉</span><div><h3>Чаты</h3><p>Сообщения покупателей</p></div></div>
        ${evRow('chats_new', 'Оповещать о новых сообщениях в чатах', 'Когда в WB OPS загружаются новые сообщения из чатов.')}</section>
      <section class="st-card"><div class="st-card-head"><span class="st-ico">▦</span><div><h3>Остатки FBS</h3><p>Чтобы товар не закончился на складе</p></div></div>
        ${evRow('stocks_low', 'Оповещать о заканчивающихся остатках', 'Когда остаток размера на складе FBS дошёл до порога (по умолчанию 5 шт.) и когда закончился. Порог — во вкладке «Остатки FBS».')}</section>
      <section class="st-card"><div class="st-card-head"><span class="st-ico">⚙</span><div><h3>Фильтры</h3><p>Ошибки и кабинеты</p></div></div>
        ${evRow('sync_errors', 'Оповещать об ошибках загрузки', 'Если автосинхронизация не смогла получить отзывы (например, истёк токен).')}
        <div class="st-ev st-ev-col"><div><b>Кабинеты</b><span>Уведомлять только по выбранным кабинетам. Ничего не выбрано — по всем.</span></div>
          <div class="st-cabs">${n.cabinet_list.map(c => `<label class="st-check"><input type="checkbox" data-cab="${esc(c.id)}" ${n.cabinets.includes(c.id) ? 'checked' : ''}> ${esc(c.name)}</label>`).join('') || '<span class="muted">Кабинетов нет</span>'}</div></div>
      </section>
      <p class="muted st-note">Новые отзывы и вопросы приходят сразу после загрузки из маркетплейса; сводка по отправленным ответам, проверке и чатам — раз в минуту.</p>`;
    bindNotifications(section);
  }

  function bindNotifications(section) {
    const save = async (patch, state) => {
      try { notif = {...notif, ...(await api('/api/notifications', {method: 'PUT', body: patch}))}; if (state) state.textContent = 'Сохранено'; return true; }
      catch (e) { if (state) state.textContent = e.message; else toast(e.message); return false; }
    };
    section.querySelectorAll('[data-ev]').forEach(sw => sw.onclick = async () => {
      const on = sw.getAttribute('aria-checked') !== 'true';
      sw.disabled = true;
      if (await save({events: {[sw.dataset.ev]: on}})) { sw.classList.toggle('on', on); sw.setAttribute('aria-checked', String(on)); }
      sw.disabled = false;
    });
    section.querySelectorAll('[data-cab]').forEach(cb => cb.onchange = () =>
      save({cabinets: [...section.querySelectorAll('[data-cab]:checked')].map(x => x.dataset.cab)}));
    section.querySelectorAll('[data-channel-form]').forEach(form => {
      const ch = form.dataset.channelForm, state = form.querySelector('[data-state]');
      form.onsubmit = async e => {
        e.preventDefault();
        const values = {enabled: form.enabled.checked};
        form.querySelectorAll('input[name]:not([type=checkbox]), select[name]').forEach(i => values[i.name] = i.value.trim());
        state.textContent = 'Сохранение…';
        if (await save({[ch]: values}, state)) drawNotifications(section);
      };
      form.querySelector('[data-test]').onclick = async ev => {
        const b = ev.currentTarget; b.disabled = true; state.textContent = 'Отправляю тест…';
        try {
          const r = await api('/api/notifications/test', {method: 'POST', body: {channel: ch}});
          notif.status = {...(notif.status || {}), [ch]: {ok: r.ok, error: r.error}};
          state.textContent = r.ok ? 'Тест отправлен — проверьте ' + (ch === 'telegram' ? 'Telegram' : 'почту') : r.error;
          if (r.ok) setTimeout(() => drawNotifications(section), 1500);
        } catch (err) { state.textContent = err.message; }
        b.disabled = false;
      };
    });
    const detect = section.querySelector('[data-detect]');
    if (detect) detect.onclick = async () => {
      const form = detect.closest('form'), state = form.querySelector('[data-state]'), box = form.querySelector('[data-chats]');
      detect.disabled = true; state.textContent = 'Ищу чаты бота…';
      try {
        const r = await api('/api/notifications/telegram/chat', {method: 'POST', body: {bot_token: form.bot_token.value.trim()}});
        if (r.chats.length === 1) { form.chat_id.value = r.chats[0].id; state.textContent = `Найден чат: ${r.chats[0].title || r.chats[0].id}. Нажмите «Сохранить».`; box.innerHTML = ''; }
        else {
          state.textContent = 'Выберите чат:';
          box.innerHTML = `<div class="st-chat-pick">${r.chats.map(c => `<button type="button" class="btn secondary" data-pick="${esc(c.id)}">${esc(c.title || c.id)}</button>`).join('')}</div>`;
          box.querySelectorAll('[data-pick]').forEach(b => b.onclick = () => { form.chat_id.value = b.dataset.pick; box.innerHTML = ''; state.textContent = 'Нажмите «Сохранить».'; });
        }
      } catch (err) { state.textContent = err.message; }
      detect.disabled = false;
    };
    section.querySelectorAll('[data-preset]').forEach(b => b.onclick = () => {
      const f = b.closest('form'); f.host.value = b.dataset.preset; f.port.value = 465; f.security.value = 'ssl';
      if (!f.to.value && f.username.value) f.to.value = f.username.value;
    });
  }

  // ======================================================================== Команда
  const teamState = {page: 0, size: 10, link: null};
  async function renderTeam(section) {
    section.className = 'panel st-page';
    section.innerHTML = '<p class="muted">Загрузка…</p>';
    let d;
    try { d = await api('/api/team'); } catch (e) { section.innerHTML = `<p class="st-error">${esc(e.message)}</p>`; return; }
    const net = d.network;
    const statusChip = s => ({active: '<span class="st-chip ok">Активен</span>', expired: '<span class="st-chip bad">Истёк</span>',
      used: '<span class="st-chip">Использован</span>', revoked: '<span class="st-chip">Отозван</span>'}[s] || esc(s));
    const total = d.invites.length, pages = Math.max(1, Math.ceil(total / teamState.size));
    teamState.page = Math.min(teamState.page, pages - 1);
    const shown = d.invites.slice(teamState.page * teamState.size, (teamState.page + 1) * teamState.size);
    section.innerHTML = `<h2>Моя команда</h2>
      <section class="st-card"><h3>Доступ по сети</h3>
        ${net.reachable ? `<p><span class="st-chip ok">✓ Сотрудники могут войти</span> Панель открыта в вашей сети по адресу <b>${esc(net.base_url)}</b>.</p>`
          : `<p><span class="st-chip warn">Только этот компьютер</span> Сейчас панель открывается только здесь. Чтобы сотрудники могли войти по приглашению, запускайте WB OPS файлом <b>start_wb_ops_team.bat</b> (при первом запуске Windows спросит разрешение для сети — разрешите для частных сетей).</p>`}
        <p class="muted">Сотрудники должны быть в той же сети (офис, Wi-Fi) или подключены к ней через VPN, а этот компьютер — включён. Без входа по приглашению панель никого не пустит; токены, ключи ИИ, финансы, кабинеты и команда доступны только вам.</p></section>
      <section class="st-card"><h3>Пригласить по ссылке</h3>
        <div class="st-actions"><button class="btn secondary" type="button" data-invite>⟳ Получить ссылку</button><span class="muted">Ссылка одноразовая и действует 7 дней.</span></div>
        <div data-link>${teamState.link ? linkBox(teamState.link) : ''}</div></section>
      <section class="st-card"><h3>Коды приглашения</h3>
        ${total ? `<div class="st-table-wrap"><table class="st-table"><thead><tr><th>Автор</th><th>Дата создания</th><th>Код</th><th>Статус</th><th></th></tr></thead><tbody>
          ${shown.map(i => `<tr><td>${esc(i.author)}</td><td class="st-dt">${dateTime(i.created_at)}</td>
            <td class="st-code"><code>${esc(i.code)}</code> <button type="button" class="st-icon" data-copy="${esc(i.code)}" aria-label="Скопировать код" title="Скопировать">⧉</button></td>
            <td>${statusChip(i.status)}${i.used_by ? `<div class="muted st-legal">${esc(i.used_by)}</div>` : ''}</td>
            <td class="r"><button type="button" class="st-icon" data-del-invite="${esc(i.code)}" aria-label="Удалить приглашение" title="Удалить">🗑</button></td></tr>`).join('')}
          </tbody></table></div>
          <div class="st-pager"><select data-size aria-label="Строк на странице">${[10, 20, 50].map(n => `<option ${n === teamState.size ? 'selected' : ''}>${n}</option>`).join('')}</select>
            <span>${teamState.page * teamState.size + 1} - ${Math.min(total, (teamState.page + 1) * teamState.size)} из ${total}</span>
            <button type="button" class="st-icon" data-prev ${teamState.page ? '' : 'disabled'} aria-label="Назад">‹</button><b>${teamState.page + 1}</b>
            <button type="button" class="st-icon" data-next ${teamState.page < pages - 1 ? '' : 'disabled'} aria-label="Вперёд">›</button></div>`
          : '<p class="muted">Приглашений пока нет.</p>'}</section>
      <section class="st-card"><h3>Сотрудники</h3>
        ${d.members.length ? `<div class="st-table-wrap"><table class="st-table"><thead><tr><th>Имя</th><th>Email</th><th>Добавлен</th><th>Последний вход</th><th class="c">Доступ</th><th></th></tr></thead><tbody>
          ${d.members.map(m => `<tr><td><b>${esc(m.name)}</b></td><td>${esc(m.email || '—')}</td><td class="st-dt">${dateTime(m.created_at)}</td><td class="st-dt">${dateTime(m.last_seen)}</td>
            <td class="c">${toggle(`data-member="${m.id}"`, m.active, `Доступ ${m.name}`)}</td>
            <td class="r"><button type="button" class="st-icon" data-del-member="${m.id}" aria-label="Удалить сотрудника" title="Удалить">🗑</button></td></tr>`).join('')}
          </tbody></table></div><p class="muted st-note">Выключенный доступ срабатывает сразу. Действия сотрудников видны в журнале на главной.</p>`
          : '<p class="muted">Сотрудников пока нет. Получите ссылку и отправьте её коллеге.</p>'}</section>`;
    bindTeam(section);
  }
  const linkBox = l => `<div class="st-linkbox"><input readonly value="${esc(l)}" aria-label="Ссылка-приглашение"><button type="button" class="btn" data-copy="${esc(l)}">Скопировать</button></div>`;

  function bindTeam(section) {
    section.querySelector('[data-invite]').onclick = async e => {
      e.currentTarget.disabled = true;
      try { const r = await api('/api/team/invites', {method: 'POST', body: {}}); teamState.link = r.link; teamState.page = 0; await renderTeam(section); }
      catch (err) { toast(err.message); e.currentTarget.disabled = false; }
    };
    section.querySelectorAll('[data-copy]').forEach(b => b.onclick = () => copy(b.dataset.copy, b.classList.contains('st-icon') ? b : null));
    section.querySelectorAll('[data-del-invite]').forEach(b => b.onclick = async () => {
      if (b.dataset.armed !== '1') { b.dataset.armed = '1'; b.textContent = 'Удалить?'; b.classList.add('armed'); return; }
      try { await api(`/api/team/invites/${b.dataset.delInvite}`, {method: 'DELETE'}); renderTeam(section); } catch (err) { toast(err.message); }
    });
    section.querySelectorAll('[data-del-member]').forEach(b => b.onclick = async () => {
      if (b.dataset.armed !== '1') { b.dataset.armed = '1'; b.textContent = 'Удалить?'; b.classList.add('armed'); return; }
      try { await api(`/api/team/members/${b.dataset.delMember}`, {method: 'DELETE'}); renderTeam(section); } catch (err) { toast(err.message); }
    });
    section.querySelectorAll('[data-member]').forEach(sw => sw.onclick = async () => {
      const on = sw.getAttribute('aria-checked') !== 'true';
      try { await api(`/api/team/members/${sw.dataset.member}`, {method: 'PUT', body: {active: on}}); sw.classList.toggle('on', on); sw.setAttribute('aria-checked', String(on)); }
      catch (err) { toast(err.message); }
    });
    const size = section.querySelector('[data-size]');
    if (size) size.onchange = () => { teamState.size = +size.value; teamState.page = 0; renderTeam(section); };
    section.querySelector('[data-prev]')?.addEventListener('click', () => { teamState.page--; renderTeam(section); });
    section.querySelector('[data-next]')?.addEventListener('click', () => { teamState.page++; renderTeam(section); });
  }

  // ======================================================================== Роль: владелец или сотрудник
  async function loadMe() {
    try { me = await api('/api/team/me'); } catch (e) { me = {role: 'owner'}; }
    if (me.role === 'member') {
      document.body.classList.add('wb-member');
      const foot = document.querySelector('.sidebar-footer');
      if (foot) {
        foot.innerHTML = `<span>${esc(me.member.name)}</span> <button type="button" class="st-link" data-logout>Выйти</button>`;
        foot.querySelector('[data-logout]').onclick = logout;
      }
    }
    return me;
  }
  const ready = loadMe();

  const style = document.createElement('style');
  style.textContent = `
.st-page h2{font-size:20px;margin:0 0 16px}.st-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:12px;margin-bottom:6px}.st-head h2{margin:0}
.st-help-btn{border:1px solid #e5e7eb;background:#fff;border-radius:10px;padding:9px 14px;font:inherit;font-size:14px;color:#374151;cursor:pointer}.st-help-btn span{margin-left:6px;color:#9ca3af}
.st-help{border:1px solid #e5e7eb;border-radius:12px;padding:12px 16px;margin:10px 0;background:#fafbfc;font-size:14px;line-height:1.5}.st-help p{margin:6px 0}
.st-label{margin:18px 0 10px;color:#4b5563}.st-add{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:18px}
.st-add-btn{min-width:170px;height:46px;padding:0 22px;border-radius:10px;font:inherit;font-size:16px;font-weight:700;cursor:pointer;border:1.5px solid}
.st-add-btn.wb{background:#fbe8f6;border-color:#d63fae;color:#b0127f}.st-add-btn.ozon{background:#e6f0fd;border-color:#5b8fd9;color:#1d5fc4}.st-add-btn.ym{background:#fdecea;border-color:#e3786a;color:#c8372a}
.st-add-btn:hover{filter:brightness(.97)}
.st-box{border:1px solid #eef0f5;border-radius:14px;overflow:hidden}.st-tabs{display:inline-flex;flex-wrap:wrap;gap:2px;background:#f1f2f6;padding:4px;border-radius:12px;margin:14px}
.st-tabs button{border:0;background:transparent;padding:8px 14px;border-radius:9px;font:inherit;font-size:14px;color:#374151;cursor:pointer}.st-tabs button.on{background:#fff;box-shadow:0 1px 3px #0000001a;font-weight:600}
.st-table-wrap{overflow-x:auto}.st-table{width:100%;border-collapse:collapse;font-size:14px;min-width:560px}.st-table th{background:#f8f9fb;text-transform:none;letter-spacing:0;font-size:13px;font-weight:600;color:#4b5563;padding:12px 14px}
.st-table td{padding:14px;border-bottom:1px solid #f0f1f5;vertical-align:middle}.st-table .c{text-align:center}.st-table .r{text-align:right;white-space:nowrap}.st-legal{font-size:13px;margin-top:3px}
.st-badge{display:inline-block;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:600;border:1px solid;margin-right:4px}
.st-badge.wb{background:#fbe8f6;border-color:#eba6d8;color:#b0127f}.st-badge.ozon{background:#e6f0fd;border-color:#a9c6f0;color:#1d5fc4}.st-badge.ym{background:#fdecea;border-color:#f0b2a9;color:#c8372a}
.st-warn{color:#b45309}.st-empty{text-align:center;padding:24px}
.st-switch{position:relative;width:44px;height:24px;border-radius:999px;border:0;background:#e5e7eb;cursor:pointer;padding:0;transition:background .15s;vertical-align:middle;flex:none}
.st-switch i{position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;box-shadow:0 1px 2px #0003;transition:left .15s}
.st-switch.on{background:#1f2328}.st-switch.on i{left:23px}.st-switch:disabled{opacity:.55;cursor:default}.st-switch:focus-visible{outline:3px solid #c4b5fd;outline-offset:2px}
.st-icon{border:0;background:transparent;font-size:16px;color:#6b7280;cursor:pointer;padding:4px 7px;border-radius:7px}.st-icon:hover{background:#f3f4f6;color:#111827}.st-icon:disabled{opacity:.35;cursor:default}.st-icon.armed{font-size:12px;font-weight:700;color:#b91c1c;background:#fef2f2}
.st-edit-row td{background:#faf9fd}.st-form h3{margin:0 0 10px;font-size:16px}.st-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.st-grid label,.st-form>label:not(.st-check){display:grid;gap:5px;font-size:13px;font-weight:600;color:#374151}.st-grid .wide{grid-column:1/-1}
.st-form input:not([type=checkbox]),.st-form select{height:40px;padding:0 11px;border:1px solid #d8d4e3;border-radius:9px;font:inherit;font-weight:400;background:#fff;min-width:0;width:100%}
.st-inline{display:flex;gap:8px}.st-inline .btn{white-space:nowrap;height:40px}.st-hint{font-size:13px;margin:10px 0}
.st-field{display:grid;gap:5px;font-size:13px;font-weight:600;color:#374151}
.st-seg{display:inline-flex;background:#eef0f4;border-radius:10px;padding:3px;gap:3px;height:40px;box-sizing:border-box;width:max-content}
.st-seg label{display:flex;cursor:pointer}.st-seg input{position:absolute;opacity:0;pointer-events:none}
.st-seg span{display:flex;align-items:center;padding:0 16px;border-radius:8px;font-weight:600;color:#4b5563}
.st-seg input:checked+span{background:#fff;color:#111827;box-shadow:0 1px 2px #0000001f}.st-seg input[value="0"]:checked+span{color:#b42323}
.st-seg input:focus-visible+span{outline:2px solid #8b5cf6}
.st-grid select{height:40px;border:1px solid #d8d4e3;border-radius:10px;padding:0 10px;font:inherit;background:#fff}
.st-off{display:inline-block;background:#f3f4f6;color:#6b7280;border-radius:6px;padding:1px 7px;font-size:11px;font-weight:700;margin-left:4px}
tr.st-disabled td>b,tr.st-disabled .st-legal{opacity:.6}
.st-tax{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.st-grid .st-tax input{width:190px;flex:none}.st-tax-chips{display:flex;gap:6px;flex-wrap:wrap}
.st-tax-chips button{border:1px solid #e5e7eb;background:#fff;border-radius:999px;padding:5px 10px;font:inherit;font-size:12px;font-weight:600;color:#4b5563;cursor:pointer}
.st-tax-chips button:hover{border-color:#8b5cf6;color:#5b21b6}
.st-actions{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:12px}.st-danger{color:#b91c1c!important;border-color:#fecaca!important}
[data-add-form] .st-form{border:1px solid #ddd6fe;border-radius:14px;padding:16px;margin-bottom:18px;background:#fcfbff}
.st-note{font-size:13px;margin-top:12px}.st-error{color:#b91c1c}
.st-card{border:1px solid #e9ebf0;border-radius:14px;padding:18px 20px;margin:0 0 16px;background:#fff}.st-card h3{font-size:16px;margin:0 0 10px}
.st-card-head{display:flex;gap:14px;align-items:center;margin-bottom:12px}.st-card-head h3{margin:0}.st-card-head p{margin:2px 0 0;color:#6b7280;font-size:14px}
.st-ico{width:40px;height:40px;border-radius:10px;background:#f3f4f6;display:grid;place-items:center;font-size:19px;flex:none}
.st-banner{border:1px solid #e5e7eb;border-radius:12px;padding:16px;text-align:center;color:#374151;margin-bottom:12px}
.st-ev{display:flex;justify-content:space-between;align-items:center;gap:16px;border:1px solid #eceef3;border-radius:12px;padding:14px 16px;margin-top:10px}
.st-ev b{display:block;font-size:15px}.st-ev span{display:block;color:#6b7280;font-size:13px;margin-top:2px}.st-ev-col{flex-direction:column;align-items:stretch}
.st-cabs{display:flex;flex-wrap:wrap;gap:8px 18px}.st-check{display:flex!important;align-items:center;gap:8px;font-size:14px;margin:12px 0 0;font-weight:500}
.st-channel{border:1px solid #eceef3;border-radius:12px;padding:12px 16px;margin-top:10px}.st-channel summary{cursor:pointer;display:flex;align-items:center;gap:10px;font-size:15px}
.st-channel[open] summary{margin-bottom:12px}.st-steps{margin:0 0 12px;padding-left:20px;color:#4b5563;font-size:14px;line-height:1.6}
.st-chip{display:inline-flex;align-items:center;padding:3px 9px;border-radius:7px;font-size:12px;font-weight:600;background:#f3f4f6;color:#4b5563;border:1px solid #e5e7eb;white-space:nowrap}
.st-chip.ok{background:#ecfdf3;color:#15803d;border-color:#86efac}.st-chip.bad{background:#fef2f2;color:#be123c;border-color:#fda4af}.st-chip.warn{background:#fffbeb;color:#b45309;border-color:#fcd34d}
.st-presets{font-size:13px;margin-bottom:10px;color:#6b7280}.st-link{border:0;background:none;padding:0;color:#5b21b6;font:inherit;cursor:pointer;text-decoration:underline}
.st-chat-pick{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.st-code{white-space:nowrap}.st-table td.st-dt{white-space:nowrap}.st-code code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;color:#374151}
.st-pager{display:flex;align-items:center;justify-content:flex-end;gap:10px;margin-top:12px;font-size:13px;color:#6b7280}.st-pager select{margin-right:auto;height:34px;border:1px solid #e5e7eb;border-radius:8px;padding:0 8px;font:inherit}
.st-pager b{display:inline-grid;place-items:center;min-width:28px;height:28px;border-radius:7px;background:#f3f4f6;color:#111827}
.st-linkbox{display:flex;gap:8px;margin-top:12px}.st-linkbox input{flex:1;min-width:0;height:40px;padding:0 11px;border:1px solid #d8d4e3;border-radius:9px;font:inherit;background:#fafafa}
.wb-member [data-nav=team],.wb-member [data-nav=notifications],.wb-member [data-nav=finances]{display:none!important}
@media(max-width:700px){.st-grid{grid-template-columns:1fr}.st-add-btn{flex:1 1 100%}.st-ev{align-items:flex-start}.st-linkbox{flex-direction:column}}`;
  document.head.appendChild(style);

  const after = fn => async section => { await ready; return fn(section); };
  return {cabinets: after(renderCabinets), notifications: after(renderNotifications), team: after(renderTeam), account: async host => { await ready; return renderAccount(host); }, me: () => ready};
})();
