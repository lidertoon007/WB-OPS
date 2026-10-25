/* WB OPS: «Товары → Номенклатура и себестоимость». Цены пишутся в opiu/cost.csv — их же берёт ОПиУ. */
window.WBOPSProducts = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const rub = v => v == null ? '—' : Math.round(v).toLocaleString('ru-RU') + ' ₽';
  const money = v => v == null ? '' : (Math.round(v * 100) / 100).toString().replace('.', ',');
  const num = v => { const t = String(v ?? '').replace(/\s/g, '').replace(',', '.'); return t === '' ? null : Number(t); };
  const monthLabel = d => { if (!d) return 'всегда'; const [y, m] = d.split('-'); return `с ${['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'][+m - 1]} ${y}`; };
  const FILTERS = [['all', 'Все'], ['nocost', 'Без себестоимости'], ['sold', 'Продавались'], ['stock', 'Есть остаток']];
  const st = {data: null, filter: 'all', search: '', brand: '', category: '', mp: '', selected: new Set(), open: null, saved: {}, dirty: false};
  let root = null, timer = null;

  async function api(url, options = {}) {
    if (options.body && typeof options.body !== 'string') options = {...options, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(options.body)};
    const r = await fetch(url, options);
    let d = null;
    try { d = await r.json(); } catch (e) { /* пусто */ }
    if (!r.ok) throw new Error((d && (typeof d.detail === 'string' ? d.detail : 'Проверьте значения')) || `Ошибка ${r.status}`);
    return d;
  }
  function toast(text) {
    const t = document.getElementById('toast');
    if (!t) return;
    t.textContent = text; t.style.display = 'block';
    clearTimeout(toast.timer); toast.timer = setTimeout(() => t.style.display = 'none', 4000);
  }

  async function render(section) {
    root = section;
    root.className = 'panel rv-page pr-page';
    root.innerHTML = `<div class="rv-head"><div class="rv-counters" data-counters></div>
        <div class="rv-head-actions"><button type="button" class="rv-dark" data-refresh title="Загрузить названия, фото и WB ID из карточек товаров Wildberries">Обновить из WB</button>
          <div class="rv-menu-wrap"><button type="button" class="rv-dark rv-sq" data-export aria-label="Скачать" title="Скачать номенклатуру">⇩</button>
          <div class="rv-menu" role="menu" hidden><a role="menuitem" href="/api/nomenclature/export?format=xlsx">Excel (.xlsx)</a><a role="menuitem" href="/api/nomenclature/export?format=csv">CSV</a></div></div></div></div>
      <div data-banner></div>
      <div class="pr-filters"><label class="rv-search"><span class="sr">Поиск</span><input type="search" data-search placeholder="Поиск по артикулу, названию или WB ID…"><span aria-hidden="true">⌕</span></label>
        <label class="rv-select"><span class="sr">Бренд</span><select data-f="brand"></select></label>
        <label class="rv-select"><span class="sr">Категория</span><select data-f="category"></select></label>
        <label class="rv-select"><span class="sr">Маркетплейс</span><select data-f="mp"></select></label></div>
      <div data-list><p class="muted">Загрузка…</p></div>
      <div class="rv-bulk pr-bulk" data-bulk hidden></div>`;
    root.querySelector('[data-search]').oninput = e => { clearTimeout(timer); timer = setTimeout(() => { st.search = e.target.value.trim().toLowerCase(); draw(); }, 200); };
    root.querySelectorAll('[data-f]').forEach(s => s.onchange = () => { st[s.dataset.f] = s.value; draw(); });
    root.querySelector('[data-refresh]').onclick = refreshCards;
    const menu = root.querySelector('.rv-menu');
    root.querySelector('[data-export]').onclick = e => { e.stopPropagation(); menu.hidden = !menu.hidden; };
    document.addEventListener('click', e => { if (root?.isConnected && !e.target.closest('.rv-menu-wrap')) menu.hidden = true; });
    await load();
  }

  async function load() {
    try { st.data = await api('/api/nomenclature'); }
    catch (e) { root.querySelector('[data-list]').innerHTML = `<p class="rv-error">${esc(e.message)}</p>`; return; }
    fillSelects(); draw();
  }

  function fillSelects() {
    const items = st.data.items;
    const uniq = key => [...new Set(items.flatMap(i => Array.isArray(i[key]) ? i[key] : [i[key]]).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'ru'));
    const set = (name, label, values, cur) => root.querySelector(`[data-f="${name}"]`).innerHTML =
      `<option value="">${label}</option>` + values.map(v => `<option ${v === cur ? 'selected' : ''}>${esc(v)}</option>`).join('');
    set('brand', 'Все бренды', uniq('brand'), st.brand);
    set('category', 'Все категории', uniq('category'), st.category);
    set('mp', 'Все маркетплейсы', uniq('marketplaces'), st.mp);
  }

  function visible() {
    return st.data.items.filter(i =>
      (st.filter !== 'nocost' || !i.has_cost) && (st.filter !== 'sold' || i.sold > 0) && (st.filter !== 'stock' || i.stock > 0) &&
      (!st.brand || i.brand === st.brand) && (!st.category || i.category === st.category) && (!st.mp || i.marketplaces.includes(st.mp)) &&
      (!st.search || [i.article, i.name, i.brand, ...i.nm_ids].join(' ').toLowerCase().replace(/ё/g, 'е').includes(st.search.replace(/ё/g, 'е'))));
  }

  function draw() {
    const items = st.data.items;
    const counts = {all: items.length, nocost: items.filter(i => !i.has_cost).length, sold: items.filter(i => i.sold > 0).length, stock: items.filter(i => i.stock > 0).length};
    root.querySelector('[data-counters]').innerHTML = FILTERS.map(([k, t]) =>
      `<button type="button" class="rv-count ${st.filter === k ? 'on' : ''} ${k === 'nocost' && counts.nocost ? 'pr-warn' : ''}" data-filter="${k}">${t} <b>${counts[k]}</b></button>`).join('');
    root.querySelectorAll('[data-filter]').forEach(b => b.onclick = () => { st.filter = b.dataset.filter; draw(); });
    const d = st.data;
    root.querySelector('[data-banner]').innerHTML = `
      ${st.dirty ? `<div class="pr-rebuild">Себестоимость изменена. Чтобы «Финансы» учли новые цены, пересоберите ОПиУ — из кэша это быстро и без запросов к маркетплейсам.
        <button type="button" class="btn" data-rebuild ${d.finance === 'running' ? 'disabled' : ''}>${d.finance === 'running' ? 'Пересобирается…' : 'Пересобрать ОПиУ'}</button></div>` : ''}
      <p class="muted pr-sources">Источники: карточки WB — ${d.cards_at ? `обновлены ${esc(d.cards_at)}` : '<b>не загружены</b> (нажмите «Обновить из WB», чтобы появились названия и фото)'};
        продажи — ${d.report ? esc(d.report) : 'отчёт ОПиУ ещё не собран'}; остатки и цены WB — ${d.goods_at ? esc(d.goods_at) : 'нет данных'}.
        Себестоимость и подготовка хранятся в <code>opiu/cost.csv</code> — их же использует расчёт ОПиУ.</p>
      ${(d.cards_errors || []).length ? `<div class="rv-banner">Не все кабинеты отдали карточки: ${esc(d.cards_errors.join('; '))}</div>` : ''}`;
    root.querySelector('[data-rebuild]')?.addEventListener('click', rebuild);
    const list = visible();
    const box = root.querySelector('[data-list]');
    if (!list.length) { box.innerHTML = `<div class="rv-empty"><b>Ничего не найдено</b><p class="muted">Измените фильтры или поиск.</p></div>`; drawBulk(); return; }
    const allSel = list.every(i => st.selected.has(i.key));
    box.innerHTML = `<div class="pr-table" role="table">
      <div class="pr-row pr-th" role="row"><label class="rv-cb"><input type="checkbox" data-all ${allSel ? 'checked' : ''} aria-label="Выбрать все"></label>
        <div>Товар</div><div class="r">Продано</div><div class="r">Остаток WB</div><div class="r">Цена WB</div><div class="r">Себестоимость, ₽</div><div class="r">Подготовка, ₽</div><div class="r">Итого на ед.</div><div>Действует</div></div>
      ${list.map(rowHtml).join('')}</div>`;
    box.querySelector('[data-all]').onchange = e => { list.forEach(i => e.target.checked ? st.selected.add(i.key) : st.selected.delete(i.key)); draw(); };
    list.forEach(bindRow);
    drawBulk();
  }

  function rowHtml(i) {
    const total = (i.cost ?? 0) + (i.prep ?? 0);
    const saved = st.saved[i.key];
    const open = st.open === i.key;
    const wb = i.nm_ids[0] ? `https://www.wildberries.ru/catalog/${i.nm_ids[0]}/detail.aspx` : '';
    return `<div class="pr-row ${i.has_cost ? '' : 'nocost'} ${st.selected.has(i.key) ? 'sel' : ''}" data-key="${esc(i.key)}" role="row">
      <label class="rv-cb"><input type="checkbox" data-sel ${st.selected.has(i.key) ? 'checked' : ''} aria-label="Выбрать ${esc(i.article)}"></label>
      <div class="pr-prod"><div class="pr-photo">${i.photo ? `<img loading="lazy" src="${esc(i.photo)}" alt="" onerror="this.remove()">` : ''}<span aria-hidden="true">👖</span></div>
        <div class="pr-pinfo"><b>${esc(i.article)}</b>${i.name ? `<span class="pr-name">${esc(i.name)}</span>` : ''}
          <span class="muted pr-meta">${[i.brand, i.category, i.nm_ids.length ? `WB ${i.nm_ids.slice(0, 3).join(', ')}${i.nm_ids.length > 3 ? '…' : ''}` : ''].filter(Boolean).map(esc).join(' · ')}
          ${wb ? `<a href="${wb}" target="_blank" rel="noopener" aria-label="Открыть на Wildberries">↗</a>` : ''}</span></div></div>
      <div class="r"><b>${i.sold ? i.sold.toLocaleString('ru-RU') : '—'}</b>${i.revenue ? `<span class="muted pr-sub">${rub(i.revenue)}</span>` : ''}</div>
      <div class="r">${i.stock != null ? Math.round(i.stock).toLocaleString('ru-RU') : '—'}</div>
      <div class="r">${i.price ? rub(i.price) : '—'}</div>
      <div class="r"><input class="pr-in" data-cost inputmode="decimal" value="${esc(money(i.cost))}" placeholder="не задана" aria-label="Себестоимость ${esc(i.article)}"></div>
      <div class="r"><input class="pr-in" data-prep inputmode="decimal" value="${esc(money(i.prep))}" placeholder="0" aria-label="Подготовка ${esc(i.article)}"></div>
      <div class="r"><b>${i.has_cost ? rub(total) : '—'}</b>${i.multiplier && i.multiplier !== 1 ? `<span class="muted pr-sub">кратность ${i.multiplier}</span>` : ''}</div>
      <div class="pr-when"><button type="button" class="pr-link" data-open>${i.has_cost ? monthLabel(i.effective_from) : 'задать'}${i.future.length ? ` <span class="pr-future" title="Запланирована новая цена">+${i.future.length}</span>` : ''}</button>
        <span class="pr-saved" data-state>${saved ? esc(saved) : ''}</span></div>
      ${open ? `<div class="pr-detail">${detailHtml(i)}</div>` : ''}</div>`;
  }

  function detailHtml(i) {
    const month = new Date(); month.setMonth(month.getMonth() + 1);
    const next = month.toISOString().slice(0, 7);
    return `<div class="pr-hist"><b>История цен</b>${i.history.length ? `<table><thead><tr><th>Действует</th><th>Себестоимость</th><th>Подготовка</th><th>Кратность</th><th></th></tr></thead><tbody>
        ${i.history.map(h => `<tr><td>${h.from ? esc(monthLabel(h.from)) : 'базовая (всегда)'}</td><td>${rub(h.cost)}</td><td>${rub(h.prep)}</td><td>${h.multiplier ?? 1}</td>
          <td>${h.from ? `<button type="button" class="pr-link danger" data-del="${esc(h.from)}">удалить</button>` : ''}</td></tr>`).join('')}</tbody></table>` : '<p class="muted">Цены ещё не задавались.</p>'}</div>
      <form class="pr-new" data-new><b>Новая цена с месяца</b>
        <p class="muted">Прошлые месяцы в ОПиУ останутся по старой цене, с выбранного месяца — по новой.</p>
        <div class="pr-new-grid"><label>С месяца<input type="month" name="from" value="${next}" required></label>
          <label>Себестоимость, ₽<input name="cost" inputmode="decimal" value="${esc(money(i.cost))}"></label>
          <label>Подготовка, ₽<input name="prep" inputmode="decimal" value="${esc(money(i.prep))}"></label>
          <label>Кратность<input name="multiplier" inputmode="decimal" value="${esc(i.multiplier ?? 1)}"></label></div>
        <div class="rv-actions"><button class="btn" type="submit">Добавить цену</button><button type="button" class="btn secondary" data-close>Закрыть</button></div></form>`;
  }

  function bindRow(i) {
    const row = root.querySelector(`.pr-row[data-key="${CSS.escape(i.key)}"]`);
    if (!row) return;
    row.querySelector('[data-sel]').onchange = e => { e.target.checked ? st.selected.add(i.key) : st.selected.delete(i.key); row.classList.toggle('sel', e.target.checked); drawBulk(); };
    row.querySelector('[data-open]').onclick = () => { st.open = st.open === i.key ? null : i.key; draw(); };
    for (const field of ['cost', 'prep']) {
      const input = row.querySelector(`[data-${field}]`);
      const commit = () => {
        const value = num(input.value);
        const before = i[field];
        if (input.value.trim() === '' && before == null) return;
        if (value === before) return;
        if (input.value.trim() !== '' && (value == null || isNaN(value) || value < 0)) { input.classList.add('bad'); toast('Введите число, например 1050 или 85,50'); return; }
        input.classList.remove('bad');
        save(i, {[field]: value ?? 0});
      };
      input.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); input.blur(); } if (e.key === 'Escape') { input.value = money(i[field]); input.blur(); } };
      input.onchange = commit;
    }
    row.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
      if (b.dataset.armed !== '1') { b.dataset.armed = '1'; b.textContent = 'точно удалить?'; return; }
      try { await api(`/api/nomenclature/price?article=${encodeURIComponent(i.article)}&from=${encodeURIComponent(b.dataset.del)}`, {method: 'DELETE'}); st.dirty = true; toast('Цена удалена'); await load(); }
      catch (e) { toast(e.message); }
    });
    const form = row.querySelector('[data-new]');
    if (form) {
      form.querySelector('[data-close]').onclick = () => { st.open = null; draw(); };
      form.onsubmit = async e => {
        e.preventDefault();
        const body = {article: i.article, effective_from: form.from.value, cost: form.cost.value, prep: form.prep.value, multiplier: form.multiplier.value};
        try { await api('/api/nomenclature/price', {method: 'PUT', body}); st.dirty = true; toast(`Цена ${monthLabel(form.from.value + '-01')} сохранена`); await load(); }
        catch (err) { toast(err.message); }
      };
    }
  }

  async function save(i, values) {
    const row = root.querySelector(`.pr-row[data-key="${CSS.escape(i.key)}"]`);
    const state = row?.querySelector('[data-state]');
    if (state) state.textContent = 'сохраняю…';
    try {
      await api('/api/nomenclature/price', {method: 'PUT', body: {article: i.article, ...values}});
      Object.assign(i, values, {has_cost: values.cost != null ? true : i.has_cost});
      st.saved[i.key] = '✓ сохранено'; st.dirty = true;
      await load();
      setTimeout(() => { delete st.saved[i.key]; const s = root.querySelector(`.pr-row[data-key="${CSS.escape(i.key)}"] [data-state]`); if (s) s.textContent = ''; }, 2500);
    } catch (e) { if (state) state.textContent = ''; toast(e.message); }
  }

  function drawBulk() {
    const bar = root.querySelector('[data-bulk]'), n = st.selected.size;
    bar.hidden = !n;
    if (!n) return;
    bar.innerHTML = `<b>Выбрано: ${n}</b>
      <label>Себестоимость<input data-b="cost" inputmode="decimal" placeholder="не менять"></label>
      <label>Подготовка<input data-b="prep" inputmode="decimal" placeholder="не менять"></label>
      <label>С месяца<input data-b="effective_from" type="month" title="Пусто — изменить действующую цену"></label>
      <button type="button" class="btn" data-apply>Применить</button><button type="button" class="rv-link" data-clear>Снять выбор</button>`;
    bar.querySelector('[data-clear]').onclick = () => { st.selected.clear(); draw(); };
    bar.querySelector('[data-apply]').onclick = async () => {
      const body = {articles: st.data.items.filter(i => st.selected.has(i.key)).map(i => i.article)};
      bar.querySelectorAll('[data-b]').forEach(x => { if (x.value.trim()) body[x.dataset.b] = x.value.trim(); });
      if (!body.cost && !body.prep) { toast('Укажите себестоимость и/или подготовку'); return; }
      try { const r = await api('/api/nomenclature/bulk', {method: 'POST', body}); toast(`Обновлено товаров: ${r.updated}`); st.dirty = true; st.selected.clear(); await load(); }
      catch (e) { toast(e.message); }
    };
  }

  async function refreshCards(e) {
    const b = e.currentTarget; b.disabled = true; b.textContent = 'Загрузка карточек…';
    try { const r = await api('/api/nomenclature/refresh', {method: 'POST'}); toast(`Карточек загружено: ${r.cards}${r.errors.length ? `. Ошибки: ${r.errors.join('; ')}` : ''}`); await load(); }
    catch (err) { toast(err.message); }
    b.disabled = false; b.textContent = 'Обновить из WB';
  }
  async function rebuild() {
    try { await api('/api/finance/update', {method: 'POST', body: {mode: 'cache'}}); toast('ОПиУ пересобирается — прогресс в разделе «Финансы»'); st.dirty = false; st.data.finance = 'running'; draw(); }
    catch (e) { toast(e.message); }
  }

  const style = document.createElement('style');
  style.textContent = `
.pt-tabs{display:flex;gap:4px;background:#eef0f4;padding:4px;border-radius:12px;width:max-content;max-width:100%;overflow-x:auto;margin:0 0 14px}
.pt-tabs a{padding:9px 16px;border-radius:9px;color:#4b5563;text-decoration:none;font-weight:600;font-size:14px;white-space:nowrap}.pt-tabs a.active{background:#fff;color:#1f2328;box-shadow:0 1px 3px #0000001a}
.wb-member .pt-tabs a[data-nav=nomenclature]{display:none}
.pr-filters{display:grid;grid-template-columns:minmax(0,2fr) repeat(3,minmax(0,1fr));gap:10px;margin-bottom:12px}.pr-filters .rv-search{grid-column:auto}
.pr-sources{font-size:12px;margin:0 0 12px}.pr-sources code{font-size:12px}.rv-count.pr-warn:not(.on){border-color:#fecaca;color:#b91c1c}
.pr-rebuild{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;background:#eef4ff;border:1px solid #c7d7fe;color:#1e3a8a;border-radius:10px;padding:10px 14px;font-size:13px;margin-bottom:10px}
.pr-rebuild .btn{height:34px;padding:0 14px;font-size:13px}
.pr-table{background:#fff;border:1px solid #e7e9ee;border-radius:12px;overflow:hidden}
.pr-row{display:grid;grid-template-columns:40px minmax(220px,2.4fr) 90px 90px 90px 120px 110px 100px 110px;gap:10px;align-items:center;padding:10px 12px;border-bottom:1px solid #f0f1f5;font-size:13px}
.pr-row:last-child{border-bottom:0}.pr-row.sel{background:#faf8ff}.pr-row.nocost{background:#fffafa}.pr-th{background:#f8f9fb;font-weight:600;color:#4b5563;font-size:12px;position:sticky;top:0;z-index:2}
.pr-row .r{text-align:right;font-variant-numeric:tabular-nums}.pr-sub{display:block;font-size:11px}
.pr-prod{display:flex;gap:10px;align-items:center;min-width:0}.pr-photo{position:relative;flex:none;width:42px;height:56px;border-radius:6px;background:#f3f4f6;display:grid;place-items:center;overflow:hidden;font-size:18px}
.pr-photo img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}.pr-pinfo{min-width:0;display:flex;flex-direction:column;gap:2px}
.pr-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#374151}.pr-meta{font-size:12px}.pr-meta a{color:#374151;text-decoration:none;margin-left:4px}
.pr-in{width:100%;height:34px;border:1px solid #dfe2e8;border-radius:8px;padding:0 9px;font:inherit;text-align:right;background:#fff}
.pr-in:hover{border-color:#c9cdd6}.pr-in:focus{outline:3px solid #ede9fe;border-color:#8b5cf6}.pr-in.bad{border-color:#ef4444}.nocost .pr-in[data-cost]{border-color:#fca5a5;background:#fff}
.pr-in::placeholder{color:#c0c4cc}.pr-when{display:flex;flex-direction:column;gap:2px}.pr-link{border:0;background:none;padding:0;font:inherit;color:#5b21b6;cursor:pointer;text-align:left}.pr-link:hover{text-decoration:underline}.pr-link.danger{color:#b91c1c}
.pr-future{display:inline-block;background:#eef4ff;color:#1d4ed8;border-radius:5px;padding:0 5px;font-size:11px;font-weight:700}.pr-saved{font-size:11px;color:#15803d}
.pr-detail{grid-column:1/-1;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;background:#fafbfc;border:1px solid #eef0f5;border-radius:10px;padding:12px 14px;margin:4px 0}
.pr-hist table{width:100%;font-size:12px;margin-top:6px}.pr-hist th,.pr-hist td{padding:6px 8px;text-transform:none;letter-spacing:0}
.pr-new p{font-size:12px;margin:4px 0 8px}.pr-new-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.pr-new-grid label{display:grid;gap:4px;font-size:12px;font-weight:600;color:#374151}
.pr-new-grid input{height:34px;border:1px solid #dfe2e8;border-radius:8px;padding:0 8px;font:inherit;min-width:0}
.pr-bulk label{display:flex;align-items:center;gap:6px;font-size:13px}.pr-bulk input{width:110px;height:32px;border:0;border-radius:7px;padding:0 8px;font:inherit}.pr-bulk input[type=month]{width:140px}
@media(max-width:1200px){.pr-row{grid-template-columns:34px minmax(180px,2fr) 80px 80px 110px 100px 90px}.pr-row>:nth-child(4),.pr-row>:nth-child(5){display:none}}
@media(max-width:760px){.pr-filters{grid-template-columns:1fr}.pr-row{grid-template-columns:30px minmax(0,1fr) 90px 80px}.pr-row>:nth-child(3),.pr-row>:nth-child(8),.pr-row>:nth-child(9){display:none}.pr-detail{grid-template-columns:1fr}.pr-new-grid{grid-template-columns:1fr 1fr}}`;
  document.head.appendChild(style);

  return {render};
})();
