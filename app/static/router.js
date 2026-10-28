/* WB OPS: client-side router. Each sidebar item shows its own section(s).
   Panels are created asynchronously by dashboard scripts, so visibility is
   re-applied whenever the set of top-level blocks in <main> changes. */
(() => {
  const main = document.querySelector('main.shell');
  if (!main) return;
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  const PAGES = {
    home:            {path: '/',                title: 'Главная', sub: 'Магазины по маркетплейсам: факт за период, остатки и прогноз на конец периода', show: ['homeSummary']},
    workspace:       {path: '/workspace',       title: 'Отзывы, вопросы и поставки FBS', sub: 'Рабочая панель продавца',
                      show: ['cabinet', 'syncToolbar', 'cards', 'inbox-panel', 'fbs-panel', 'complaints-panel', 'audit-panel']},
    dashboard:       {path: '/dashboard',       title: 'Дашборд WB', sub: 'Оцифровка: ключевые показатели Wildberries за период', show: ['dashboardPage']},
    'dashboard-ozon': {path: '/dashboard-ozon', title: 'Дашборд Ozon', sub: 'Оцифровка: ключевые показатели Ozon за период', show: ['ozonDashboardPage']},
    transfer:        {path: '/transfer',        title: 'Перенос карточек', sub: 'Перенос товаров между кабинетами Wildberries, Ozon и Яндекс Маркета по артикулу', show: ['transferPage']},
    'fbs-analytics': {path: '/fbs-analytics',   title: 'Аналитика FBS', sub: 'Статусы сборочных заданий, скорость отгрузки, склады, % выкупа и скидки/штрафы WB за сроки', show: ['fbsPage']},
    ab:              {path: '/ab',              title: 'A/B-тесты', sub: 'Какое главное фото даёт больше кликов и корзин: варианты по очереди на живой карточке', show: ['abPage']},
    abc:             {path: '/abc',             title: 'ABC-анализ', sub: 'Сегментация ассортимента по выручке, оборачиваемости и прибыли и контроль неликвида', show: ['abcPage']},
    planfact:        {path: '/planfact',        title: 'План-факт', sub: 'Планы на месяц по бизнесу и артикулам, отклонения и прогноз на конец месяца', show: ['planfactPage']},
    ads:             {path: '/ads',             title: 'Аналитика рекламы', sub: 'Расходы, ДРР, кампании, артикулы, поисковые запросы и списания WB за рекламу', show: ['adsPage']},
    penalties:       {path: '/penalties',       title: 'Аналитика штрафов', sub: 'Штрафы Wildberries из отчётов реализации: динамика, причины, артикулы, задания FBS и прочие удержания', show: ['penaltiesPage']},
    stocks:          {path: '/stocks',          title: 'Остатки FBS', sub: 'Быстрое пополнение остатков по размерам и оповещения в Telegram, пока товар не закончился', show: ['stocksPage']},
    repricer:        {path: '/repricer',        title: 'Репрайсер', sub: 'Управление ценами Wildberries с прогнозом маржи и лимитами безопасности', show: ['repricerPage']},
    rnp:             {path: '/rnp',             title: 'РНП', sub: 'Рука на пульсе: заказы, реклама, остатки и прогноз прибыли по дням и артикулам', show: ['rnpPage']},
    products:        {path: '/products',        title: 'Товары', sub: 'Модели, таблицы замеров и факты для ответов на вопросы', show: ['productTabs', 'modelsPanel', 'productKnowledgePanel']},
    nomenclature:    {path: '/nomenclature',    title: 'Товары', sub: 'Вся номенклатура: себестоимость и подготовка для ОПиУ', show: ['productTabs', 'nomenclature'], nav: 'products'},
    cabinets:        {path: '/cabinets',        title: 'Кабинеты маркетплейсов', sub: 'Подключение Wildberries, Ozon и Яндекс.Маркета', show: ['cabinets']},
    knowledge:       {path: '/knowledge',       title: 'База знаний', sub: 'Знания о товарах и магазине для ответов', show: ['knowledge', 'knowledgeHubPanel']},
    analytics:       {path: '/analytics',       title: 'Аналитика и отчёты', sub: 'Отзывы, вопросы и чаты: динамика, теги, тренды и отчёты по товарам', show: ['analytics']},
    reviews:         {path: '/reviews',         title: 'Все отзывы', sub: 'Ответы покупателям: проверка, правка и отправка', show: ['reviewsPage']},
    complaints:      {path: '/complaints',      title: 'Очередь жалоб', sub: 'Жалобы на отзывы 1–2★: основания, подготовка и отправка в WB', show: ['complaintsPage']},
    templates:       {path: '/templates',       title: 'Шаблоны ответов', sub: 'Индивидуальные ответы на отзывы, фразы и шаблоны', show: ['reviewRepliesPanel', 'templatePanel', 'customTemplatesPanel']},
    recommendations: {path: '/recommendations', title: 'Рекомендации товаров', sub: 'Какие товары предлагать в ответах', show: ['recommendationsPanel']},
    settings:        {path: '/settings',        title: 'Настройки ответов', sub: 'Автоответы на отзывы и правила', show: ['autoRepliesPanel']},
    ai:              {path: '/ai',              title: 'Настройки ИИ', sub: 'Подключение ИИ, подпись, тон и длина ответов', show: ['aiConnectionPanel', 'aiSettingsPanel']},
    import:          {path: '/import',          title: 'Импорт отзывов', sub: 'Загрузка истории отзывов из WB', show: ['importPanel']},
    questions:       {path: '/questions',       title: 'Все вопросы', sub: 'Ответы на вопросы покупателей', show: ['cabinet', 'syncToolbar', 'questionRepliesPanel', 'questionsPanel']},
    chats:           {path: '/chats',           title: 'Управление чатами', sub: 'Диалоги с покупателями', show: ['chats']},
    messengers:      {path: '/messengers',      title: 'Агенты в мессенджерах', sub: 'Ответы покупателям в мессенджерах', show: ['messengers']},
    profile:         {path: '/profile',         title: 'Учётная запись', sub: 'Владелец, режим работы, резервные копии и API', show: ['profile']},
    notifications:   {path: '/notifications',   title: 'Настройки уведомлений', sub: 'Telegram и email: отзывы, вопросы, чаты', show: ['notificationsSettings']},
    team:            {path: '/team',            title: 'Моя команда', sub: 'Приглашения и доступ сотрудников', show: ['team']},
    finances:        {path: '/finances',        title: 'Финансы', sub: 'ОПиУ по всем маркетплейсам и кабинетам', show: ['finances']},
  };
  const SENDS = new Set(['workspace', 'reviews', 'complaints', 'questions', 'chats', 'messengers', 'templates', 'settings', 'ai', 'import']);
  const byPath = Object.fromEntries(Object.entries(PAGES).map(([k, v]) => [v.path, k]));
  // Сводка по магазинам — только владельцу: сотрудник на «Главной» видит рабочую очередь
  const currentPage = () => { const p = byPath[location.pathname.replace(/\/+$/, '') || '/'] || 'home';
    return p === 'home' && document.body.classList.contains('wb-member') ? 'workspace' : p; };

  // --- stable ids for blocks that had none --------------------------------
  const header = document.getElementById('home');
  const syncToolbar = document.getElementById('cabinet')?.nextElementSibling;
  if (syncToolbar && !syncToolbar.id) syncToolbar.id = 'syncToolbar';

  const section = (id, html, cls = 'panel') => {
    let s = document.getElementById(id);
    if (!s) { s = document.createElement('section'); s.id = id; main.append(s); }
    s.className = cls; s.innerHTML = html; s.dataset.routerReady = '1';
    return s;
  };

  // Knowledge workspace (knowledge.js renders into #knowledge).
  if (!document.getElementById('knowledge')) {
    const k = document.createElement('section'); k.id = 'knowledge'; k.className = 'panel'; main.append(k);
  }

  // --- Analytics (analytics.js) --------------------------------------------
  function renderAnalytics() {
    const s = section('analytics', '');
    if (window.WBOPSAnalytics) window.WBOPSAnalytics.render(s);
    else s.innerHTML = '<h2>Аналитика</h2><p class="muted">Не загрузился модуль analytics.js.</p>';
  }

  // --- Import ---------------------------------------------------------------
  async function renderImport() {
    const s = section('importPanel', `<h2>Импорт истории отзывов</h2>
      <p class="muted">Загружает отзывы из WB за период (включая уже отвеченные), создаёт черновики и разметку. Ничего не публикуется.</p>
      <form class="r-form" data-import-form>
        <label>Дата от<input type="date" name="date_from"></label>
        <label>Дата до<input type="date" name="date_to"></label>
        <fieldset><legend>Кабинеты</legend><div data-import-cabinets class="muted">Загрузка…</div></fieldset>
        <label class="r-check"><input type="checkbox" name="with_markup" checked> Размечать темы и тональность</label>
        <button class="btn" type="submit">Импортировать</button>
      </form><div data-import-result role="status" aria-live="polite"></div>`);
    try {
      const cabinets = await (await fetch('/api/cabinets')).json();
      s.querySelector('[data-import-cabinets]').innerHTML = cabinets.length
        ? cabinets.map(c => `<label class="r-check"><input type="checkbox" name="cabinet" value="${esc(c.id)}" checked> ${esc(c.name)}${c.token_configured ? '' : ' <span class="muted">(нет токена)</span>'}</label>`).join('')
        : 'Кабинеты не настроены — добавьте их в разделе «Кабинеты маркетплейсов».';
    } catch (e) { s.querySelector('[data-import-cabinets]').textContent = 'Не удалось загрузить кабинеты.'; }
    s.querySelector('[data-import-form]').onsubmit = async ev => {
      ev.preventDefault();
      const f = ev.currentTarget, out = s.querySelector('[data-import-result]'), btn = f.querySelector('button');
      const cabinet_ids = [...f.querySelectorAll('[name=cabinet]:checked')].map(x => x.value);
      if (!cabinet_ids.length) { out.textContent = 'Выберите хотя бы один кабинет.'; return; }
      if (f.date_from.value && f.date_to.value && f.date_from.value > f.date_to.value) { out.textContent = 'Дата «от» позже даты «до».'; return; }
      btn.disabled = true; btn.textContent = 'Импорт…'; out.textContent = 'Загружаю отзывы из WB — это может занять время.';
      try {
        const r = await fetch('/api/import/reviews', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({cabinet_ids, date_from: f.date_from.value || null, date_to: f.date_to.value || null, with_markup: f.with_markup.checked})});
        const d = await r.json();
        out.innerHTML = r.ok
          ? `<p><b>Загружено новых: ${d.imported}</b></p>` + d.results.map(x => `<p>${esc(x.cabinet_id)}: ${x.status === 'ok' ? 'получено ' + x.count : 'ошибка — ' + esc(x.reason)}</p>`).join('')
          : `<p>Ошибка: ${esc(d.detail || r.status)}</p>`;
      } catch (e) { out.textContent = 'Запрос не выполнен. Проверьте, что сервис запущен.'; }
      btn.disabled = false; btn.textContent = 'Импортировать';
    };
  }

  // --- Profile --------------------------------------------------------------
  async function renderProfile() {
    const s = section('profile', '<h2>Личный кабинет</h2><p class="muted">Загрузка…</p>');
    let h = {};
    try { h = await (await fetch('/health')).json(); } catch (e) {}
    const mode = h.dry_run === false ? 'Публикация включена' : (h.manual_send_enabled ? 'Ручная отправка включена' : 'Только черновики');
    s.innerHTML = `<div data-account></div><h2>Режим работы</h2>
      <div class="r-kpis"><article><small>Режим публикации</small><strong class="r-small">${esc(mode)}</strong></article>
      <article><small>Окружение</small><strong class="r-small">${esc(h.environment || '—')}</strong></article>
      <article><small>Ручная отправка</small><strong class="r-small">${h.manual_send_enabled ? 'включена' : 'выключена'}</strong></article></div>
      <h3>Данные</h3><div class="toolbar"><a class="btn secondary" href="/api/admin/backup">⇩ Резервная копия базы</a>
      <a class="btn secondary" href="/api/fbs-reports/export">⇩ Excel по поставкам FBS</a><a class="btn secondary" href="/docs" target="_blank" rel="noopener">Документация API</a></div>
      <p class="muted">Сотрудников можно пригласить в разделе «Моя команда».</p>`;
    if (window.WBOPSSettings) window.WBOPSSettings.account(s.querySelector('[data-account]'));
  }

  // --- Messengers: short intro above Telegram settings ------------------------
  function renderMessengers() {
    section('messengers', '<h2>Агенты в мессенджерах</h2><p class="muted">Агенты, отвечающие покупателям в мессенджерах, появятся позже. Уведомления о новых отзывах, вопросах и чатах в Telegram и на почту настраиваются в разделе <a href="/notifications" data-nav="notifications">Настройки уведомлений</a>.</p>');
  }
  function renderFinances() {
    const s = section('finances', '');
    if (window.WBOPSFinance) window.WBOPSFinance.render(s);
    else s.innerHTML = '<h2>Финансы</h2><p class="muted">Не загрузился модуль finance.js.</p>';
  }


  // --- visibility -------------------------------------------------------------
  const settingsPage = (id, fn) => () => {
    const s = section(id, '');
    s.dataset.cabinetReady = '1';   // старый скрипт кабинетов не перерисовывает раздел
    if (window.WBOPSSettings) window.WBOPSSettings[fn](s);
    else s.innerHTML = '<p class="muted">Не загрузился модуль settings.js.</p>';
  };
  const reviewsPage = () => {
    const s = section('reviewsPage', '');
    if (window.WBOPSReviews) window.WBOPSReviews.render(s);
    else s.innerHTML = '<p class="muted">Не загрузился модуль reviews.js.</p>';
  };
  const complaintsPage = () => {
    const s = section('complaintsPage', '');
    if (window.WBOPSComplaints) window.WBOPSComplaints.render(s);
    else s.innerHTML = '<p class="muted">Не загрузился модуль complaints.js.</p>';
  };
  // Вкладки раздела «Товары» (общие для двух страниц)
  if (!document.getElementById('productTabs')) {
    const t = document.createElement('section'); t.id = 'productTabs'; t.className = 'pt-tabs';
    t.innerHTML = '<a href="/products" data-nav="products">Модели и ответы на вопросы</a><a href="/nomenclature" data-nav="nomenclature">Номенклатура и себестоимость</a>';
    main.append(t);
  }
  const nomenclaturePage = () => {
    const s = section('nomenclature', '');
    if (window.WBOPSProducts) window.WBOPSProducts.render(s);
    else s.innerHTML = '<p class="muted">Не загрузился модуль products.js.</p>';
  };
  const abcPage = () => {
    const s = section('abcPage', '');
    if (window.WBOPSAbc) window.WBOPSAbc.render(s);
    else s.innerHTML = '<p class="muted">Не загрузился модуль abc.js.</p>';
  };
  const homePage = () => {
    const s = section('homeSummary', '');
    if (window.WBOPSHome) window.WBOPSHome.render(s);
    else s.innerHTML = '<p class="muted">Не загрузился модуль home.js.</p>';
  };
  const dashboardPage = () => {
    const s = section('dashboardPage', '');
    if (window.WBOPSDashboard) window.WBOPSDashboard.render(s);
    else s.innerHTML = '<p class="muted">Не загрузился модуль dashboard.js.</p>';
  };
  const ozonDashboardPage = () => {
    const s = section('ozonDashboardPage', '');
    if (window.WBOPSDashboard) window.WBOPSDashboard.create().render(s, 'ozon');
    else s.innerHTML = '<p class="muted">Не загрузился модуль dashboard.js.</p>';
  };
  const rnpPage = () => {
    const s = section('rnpPage', '');
    if (window.WBOPSRnp) window.WBOPSRnp.render(s);
    else s.innerHTML = '<p class="muted">Не загрузился модуль rnp.js.</p>';
  };
  const transferPage = () => {
    const s = section('transferPage', '');
    if (window.WBOPSTransfer) window.WBOPSTransfer.render(s);
    else s.innerHTML = '<p class="muted">Не загрузился модуль transfer.js.</p>';
  };
  const repricerPage = () => {
    const s = section('repricerPage', '');
    if (window.WBOPSRepricer) window.WBOPSRepricer.render(s);
    else s.innerHTML = '<p class="muted">Не загрузился модуль repricer.js.</p>';
  };
  const stocksPage = () => {
    const s = section('stocksPage', '');
    if (window.WBOPSStocks) window.WBOPSStocks.render(s);
    else s.innerHTML = '<p class="muted">Не загрузился модуль stocks.js.</p>';
  };
  const fbsPage = () => {
    const s = section('fbsPage', '');
    if (window.WBOPSFbs) window.WBOPSFbs.render(s);
    else s.innerHTML = '<p class="muted">Не загрузился модуль fbs.js.</p>';
  };
  const penaltiesPage = () => {
    const s = section('penaltiesPage', '');
    if (window.WBOPSPenalties) window.WBOPSPenalties.render(s);
    else s.innerHTML = '<p class="muted">Не загрузился модуль penalties.js.</p>';
  };
  const adsPage = () => {
    const s = section('adsPage', '');
    if (window.WBOPSAds) window.WBOPSAds.render(s);
    else s.innerHTML = '<p class="muted">Не загрузился модуль ads.js.</p>';
  };
  const planfactPage = () => {
    const s = section('planfactPage', '');
    if (window.WBOPSPlanFact) window.WBOPSPlanFact.render(s);
    else s.innerHTML = '<p class="muted">Не загрузился модуль planfact.js.</p>';
  };
  const abPage = () => {
    const s = section('abPage', '');
    if (window.WBOPSAB) window.WBOPSAB.render(s);
    else s.innerHTML = '<p class="muted">Не загрузился модуль ab.js.</p>';
  };
  const rendered = {home: homePage, abc: abcPage, ab: abPage, planfact: planfactPage, ads: adsPage, penalties: penaltiesPage, 'fbs-analytics': fbsPage, stocks: stocksPage, repricer: repricerPage, transfer: transferPage, rnp: rnpPage, dashboard: dashboardPage, 'dashboard-ozon': ozonDashboardPage, nomenclature: nomenclaturePage, complaints: complaintsPage, reviews: reviewsPage, cabinets: settingsPage('cabinets', 'cabinets'), notifications: settingsPage('notificationsSettings', 'notifications'), team: settingsPage('team', 'team'), analytics: renderAnalytics, import: renderImport, profile: renderProfile, messengers: renderMessengers, finances: renderFinances};
  const done = new Set();
  let applying = false;
  function apply() {
    if (applying) return; applying = true;
    const page = currentPage(), cfg = PAGES[page];
    if (rendered[page] && !done.has(page)) { done.add(page); rendered[page](); }
    const visible = new Set(cfg.show);
    [...main.children].forEach(el => {
      if (el.tagName === 'ASIDE' || el === header || el.tagName === 'SCRIPT' || el.classList.contains('bulk-overlay') || el.id === 'toast') return;
      const show = el.id && visible.has(el.id);
      el.style.display = show ? '' : 'none';
    });
    // Keep the order declared in PAGES.
    let prev = null;
    cfg.show.forEach(id => {
      const el = document.getElementById(id);
      if (!el || el.parentElement !== main) return;
      if (prev && (prev.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_PRECEDING)) main.insertBefore(el, prev.nextSibling);
      prev = el;
    });
    if (header) {
      const t = header.querySelector('.title'), m = header.querySelector('.brand .muted');
      if (t) t.textContent = cfg.title; if (m) m.textContent = cfg.sub;
    }
    // Режим публикации важен только там, где что-то отправляется покупателям.
    const ps = document.getElementById('publishStatus');
    if (ps) ps.style.display = SENDS.has(page) ? '' : 'none';
    document.title = cfg.title + ' · WB OPS';
    document.querySelectorAll('[data-nav]').forEach(a => {
      const target = PAGES[a.dataset.nav];
      if (target) a.setAttribute('href', target.path);
      a.classList.toggle('active', a.dataset.nav === page || (a.dataset.nav === cfg.nav && !a.closest('#productTabs')));
    });
    applying = false;
    syncGroups();
  }

  // Sidebar links: navigate without reload; capture phase overrides older handlers.
  document.addEventListener('click', ev => {
    const a = ev.target.closest && ev.target.closest('[data-nav]');
    if (!a || ev.button !== 0 || ev.ctrlKey || ev.metaKey || ev.shiftKey) return;
    const target = PAGES[a.dataset.nav];
    if (!target) return;
    ev.preventDefault(); ev.stopPropagation();
    if (location.pathname !== target.path) history.pushState({}, '', target.path);
    apply(); window.scrollTo(0, 0);
  }, true);
  window.addEventListener('popstate', apply);
  new MutationObserver(apply).observe(main, {childList: true});

  // --- Сворачиваемые группы меню (+ / −) ----------------------------------
  const nav = document.querySelector('.side-nav');
  const NAV_KEY = 'wbops.nav.collapsed';
  let navState = {};
  try { navState = JSON.parse(localStorage.getItem(NAV_KEY) || '{}'); } catch (e) { navState = {}; }
  if (nav) {
    // «Аналитика» была ссылкой с «+», делаем из неё группу с подпунктом
    const an = nav.querySelector('a[data-nav="analytics"]');
    if (an && an.querySelector('.nav-plus')) {
      const g = document.createElement('div');
      g.className = 'nav-group-title';
      g.innerHTML = '▥ <span>Аналитика</span><span>+</span>';
      an.className = 'nav-link sub';
      an.innerHTML = 'Отзывы, вопросы и чаты';
      an.before(g);
      // Аналитика FBS, штрафов и рекламы — подпункты группы «Аналитика»
      [['fbs-analytics', 'Аналитика FBS'], ['penalties', 'Аналитика штрафов'], ['ads', 'Аналитика рекламы']].forEach(([key, title]) => {
        const a = nav.querySelector(`a[data-nav="${key}"]`);
        if (!a) return;
        a.className = 'nav-link sub';
        a.textContent = title;
        an.before(a);
      });
    }
    const sub = (a, title) => { a.className = 'nav-link sub'; a.textContent = title; return a; };
    // Вопросы и чаты — подпункты «Управления отзывами», сразу после «Все отзывы»
    const allReviews = nav.querySelector('a[data-nav="reviews"]');
    const qTitle = [...nav.querySelectorAll('.nav-group-title')].find(x => x.textContent.includes('вопросами'));
    if (allReviews && !allReviews.dataset.moved) {
      allReviews.dataset.moved = '1';
      let q = nav.querySelector('a[data-nav="questions"]');
      if (!q) { q = document.createElement('a'); q.href = '/questions'; q.dataset.nav = 'questions'; }
      const chats = nav.querySelector('a[data-nav="chats"]');
      allReviews.after(sub(q, 'Все вопросы'));
      if (chats) q.after(sub(chats, 'Чаты с покупателями'));
      if (qTitle) qTitle.remove();
    }
    // База знаний — подпункт «Личного кабинета», после «Моя команда»
    const team = nav.querySelector('a[data-nav="team"]'), kb = nav.querySelector('a[data-nav="knowledge"]');
    if (team && kb && !kb.classList.contains('sub')) team.after(sub(kb, 'База знаний'));
    // Кабинеты маркетплейсов — подпункт «Личного кабинета», после «Учётной записи»
    const profile = nav.querySelector('a[data-nav="profile"]'), cab = nav.querySelector('a[data-nav="cabinets"]');
    if (profile && cab && !cab.classList.contains('sub')) profile.after(sub(cab, 'Кабинеты маркетплейсов'));
  }
  const groupKey = t => (t.querySelector('span') || t).textContent.trim();
  const groupSubs = t => { const out = []; let n = t.nextElementSibling; while (n && n.classList.contains('sub')) { out.push(n); n = n.nextElementSibling; } return out; };
  function syncGroups() {
    if (!nav) return;
    nav.querySelectorAll('.nav-group-title').forEach(t => {
      const subs = groupSubs(t), key = groupKey(t);
      if (!t.dataset.navInit) {
        t.dataset.navInit = '1';
        t.setAttribute('role', 'button'); t.tabIndex = 0;
        const sign = t.querySelector('span:last-child');
        if (sign && /^[+−-]$/.test(sign.textContent.trim())) sign.classList.add('nav-sign');
        if (!(key in navState)) navState[key] = false;
        const toggle = () => { navState[key] = !navState[key]; try { localStorage.setItem(NAV_KEY, JSON.stringify(navState)); } catch (e) { /* ignore */ } syncGroups(); };
        t.addEventListener('click', toggle);
        t.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
      }
      const hasActive = subs.some(a => a.classList.contains('active') && a.dataset.nav === currentPage());
      const closed = navState[key] && !hasActive;
      subs.forEach(a => { a.style.display = closed ? 'none' : ''; });
      t.classList.toggle('collapsed', closed);
      t.setAttribute('aria-expanded', String(!closed));
      const sign = t.querySelector('.nav-sign'); if (sign) sign.textContent = closed ? '+' : '−';
    });
  }
  if (nav) new MutationObserver(() => syncGroups()).observe(nav, {childList: true});

  const style = document.createElement('style');
  style.textContent = `.r-head{display:flex;justify-content:space-between;align-items:center;gap:12px}.r-head h2{margin:0}
.r-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:16px 0}.r-kpis article{border:1px solid #e5e7eb;border-radius:12px;padding:14px}.r-kpis small{display:block;color:#6b7280}.r-kpis strong{font-size:26px;color:#6d28d9}.r-kpis .r-small{font-size:16px}
.r-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:24px}.r-bar{display:grid;grid-template-columns:120px 1fr 40px;align-items:center;gap:8px;margin:6px 0;font-size:14px}.r-bar div{background:#f3f4f6;border-radius:6px;height:10px;overflow:hidden}.r-bar i{display:block;height:100%;background:#8b5cf6;border-radius:6px}.r-bar b{text-align:right}
.r-form{display:grid;gap:12px;max-width:520px;margin:12px 0}.r-form label{display:grid;gap:4px}.r-form input[type=date]{padding:9px;border:1px solid #ddd6fe;border-radius:10px}.r-form fieldset{border:1px solid #e5e7eb;border-radius:10px;padding:10px 14px}.r-check{display:flex!important;align-items:center;gap:8px}.r-form .btn{justify-self:start}
.side-nav .nav-group-title{cursor:pointer;user-select:none;border-radius:10px}.side-nav .nav-group-title:hover{background:#f4f3f7}.side-nav .nav-group-title:focus-visible{outline:2px solid #8b5cf6;outline-offset:1px}`;
  document.head.appendChild(style);

  apply();
})();
