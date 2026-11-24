const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// Exercise the actual send/API/state flow; omit only DOM rendering.
let code = fs.readFileSync('app/static/reviews.js', 'utf8');
function stubFunction(name) {
  const start = code.indexOf(`function ${name}(`);
  assert(start >= 0, name);
  const body = code.indexOf('{', start);
  let depth = 1, end = body + 1;
  while (depth && end < code.length) {
    if (code[end] === '{') depth++;
    if (code[end] === '}') depth--;
    end++;
  }
  code = code.slice(0, body + 1) + code.slice(end - 1);
}
for (const name of ['drawFilters', 'drawCounters', 'drawBanner', 'drawList', 'drawBulk', 'loadPhotos', 'redrawRow', 'toast']) stubFunction(name);
code = code.replace('return {render, reload: () => load(true)};', 'return {st, send, rowHtml, setRoot: value => root = value};');

async function scenario(kind, fail) {
  let published = false;
  const requests = [];
  const context = {
    window: {}, URL, URLSearchParams, setTimeout, clearTimeout,
    document: {createElement: () => ({}), getElementById: () => true},
    location: {pathname: kind === 'question' ? '/questions' : '/reviews'},
    history: {replaceState() {}},
    fetch: async (url, options = {}) => {
      requests.push({url, options});
      const response = (value, ok = true) => ({ok, status: ok ? 200 : 502, json: async () => value});
      if (url.includes('/check/')) return response({ok: true});
      if (url.includes('/send/')) {
        if (fail) return response({detail: 'Marketplace unavailable'}, false);
        published = true;
        return response({published: true});
      }
      if (url.startsWith('/api/reviews?')) {
        const p = new URLSearchParams(url.split('?')[1]);
        assert.equal(p.get('item_type'), kind);
        const row = {id: 'q1', answer: 'Original answer', status: published ? 'published' : 'draft'};
        if (p.has('search')) return response({items: [row]});
        assert.equal(p.get('answer'), 'unanswered');
        assert.equal(p.get('cabinet_id'), 'main');
        return response({items: published ? [] : [row], total: published ? 0 : 1, counts: {unanswered: published ? 0 : 1, published: published ? 1 : 0}});
      }
      return response({});
    }
  };
  vm.runInNewContext(code, context);
  const list = kind === 'question' ? context.window.WBOPSQuestions : context.window.WBOPSReviews;
  const row = {id: 'q1', product_id: 111, vendor_code: '<vendor>', tags: [], review_photos: [
    {url: 'https://feedback04.wbbasket.ru/full.webp?a=1&b=2', thumbnail_url: 'https://feedback04.wbbasket.ru/mini.webp'},
    {url: 'javascript:alert(1)', thumbnail_url: 'https://feedback04.wbbasket.ru/invalid.webp'},
  ]};
  const rowHtml = list.rowHtml(row);
  assert.match(rowHtml, /&lt;vendor&gt; · 111/);
  assert.doesNotMatch(rowHtml, /javascript:|invalid\.webp/);
  assert.doesNotMatch(list.rowHtml({...row, review_photos: []}), /rv-review-photos/);
  if (kind === 'review') {
    assert.match(rowHtml, /href="https:\/\/feedback04\.wbbasket\.ru\/full\.webp\?a=1&amp;b=2"/);
    assert.match(rowHtml, /src="https:\/\/feedback04\.wbbasket\.ru\/mini\.webp"/);
  } else {
    assert.doesNotMatch(rowHtml, /rv-review-photos/);
  }
  list.setRoot({querySelector: () => ({classList: {add() {}, remove() {}}})});
  list.st.f = {sort: 'desc', answer: 'unanswered', cabinet_id: 'main'};
  list.st.data = {items: [{id: 'q1', answer: 'Original answer', status: 'draft'}], counts: {unanswered: 1, published: 0}, total: 1};
  list.st.editing = 'q1';
  list.st.selected.add('q1');
  await list.send('q1', 'Edited answer');
  const reloads = requests.filter(r => r.url.startsWith('/api/reviews?') && new URLSearchParams(r.url.split('?')[1]).has('answer'));
  if (fail) {
    assert.equal(reloads.length, 0);
    assert.equal(list.st.data.items[0].pending, 'Edited answer');
    assert.equal(list.st.editing, 'q1');
    assert.equal(list.st.data.counts.unanswered, 1);
  } else {
    assert.equal(reloads.length, 1);
    assert.equal(list.st.data.counts.unanswered, 0);
    assert.equal(list.st.data.counts.published, 1);
    assert.equal(list.st.data.items.length, 0);
    assert.equal(list.st.editing, null);
    assert.equal(list.st.selected.has('q1'), false);
  }
  assert.equal(list.st.busy.size, 0);
  assert.equal(list.st.f.answer, 'unanswered');
}

(async () => {
  for (const kind of ['question', 'review']) for (const fail of [false, true]) await scenario(kind, fail);
  console.log('PASS: single-send reloads filtered counts/list; failed sends retain edits, for questions and reviews');
})().catch(error => { console.error(error); process.exitCode = 1; });
