const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let code = fs.readFileSync('app/static/reviews.js', 'utf8');
code = code.replace('return {render, reload: () => load(true)};', 'return {st, readUrl, writeUrl, query, drawFilters, bindFilters, setRoot: value => root = value};');
code = code.replace('async function load(withMeta = false) {', 'async function load(withMeta = false) { globalThis.loads = (globalThis.loads || 0) + 1; writeUrl(); return;');
const context = {window: {}, document: {createElement: () => ({}), getElementById: () => true}, URLSearchParams,
  location: {pathname: '/questions', search: '?tag=size:negative&order_status=buyout&rating=1&answer=unanswered&marketplace=wildberries&cabinet_id=main&search=jeans&from=2026-11-01'},
  history: {replaceState: (_state, _title, url) => context.url = url}, setTimeout, clearTimeout};
vm.runInNewContext(code, context);
const q = context.window.WBOPSQuestions;
q.readUrl();
const params = new URLSearchParams(q.query());
for (const key of ['tag', 'order_status', 'rating']) assert.equal(params.has(key), false, key);
for (const [key, value] of [['answer', 'unanswered'], ['marketplace', 'wildberries'], ['cabinet_id', 'main'], ['search', 'jeans'], ['from', '2026-11-01'], ['item_type', 'question']]) assert.equal(params.get(key), value);
q.writeUrl();
assert(!/tag=|order_status=|rating=/.test(context.url));
// Exercise real filter binding with a small DOM stub; no browser engine is required.
const nodes = new Map();
const node = selector => {
  if (!nodes.has(selector)) nodes.set(selector, {value: '', hidden: true, querySelector: child => node(selector + ' ' + child), querySelectorAll: () => []});
  return nodes.get(selector);
};
const box = {innerHTML: '', querySelector: selector => selector === '.rv-tag-pop' ? null : node(selector), querySelectorAll: () => []};
q.setRoot({querySelector: () => box});
q.st.meta = {marketplaces: {wildberries: 'WB'}, cabinets: [{id: 'main', name: 'Main'}], answer_statuses: {unanswered: 'Без ответа'}};
q.st.data = {total: 3};
q.drawFilters();
assert.deepEqual([...box.innerHTML.matchAll(/<select data-f="([^"]+)"/g)].map(m => m[1]), ['marketplace', 'cabinet_id', 'answer']);
assert(!box.innerHTML.includes('data-tags'));
assert(box.innerHTML.includes('value="unanswered" selected>Без ответа'));
assert.equal(typeof node('[data-reset]').onclick, 'function');
assert.equal(typeof node('.rv-range-pop [data-range-apply]').onclick, 'function');
node('[data-reset]').onclick();
assert.equal(context.loads, 1);
assert.equal(context.url, '/questions');
assert.equal(new URLSearchParams(q.query()).toString(), 'sort=desc&item_type=question');
// The review list retains all filters and old URL parameters.
context.location.pathname = '/reviews';
const r = context.window.WBOPSReviews;
r.readUrl();
for (const key of ['tag', 'order_status', 'rating']) assert(new URLSearchParams(r.query()).has(key));
// Render reviews without binding to the intentionally incomplete review-tag stub.
const renderOnly = code.replace('    bindFilters();', '');
const reviewContext = {...context, window: {}};
vm.runInNewContext(renderOnly, reviewContext);
const review = reviewContext.window.WBOPSReviews;
review.st.meta = q.st.meta; review.st.data = q.st.data; review.setRoot({querySelector: () => box}); review.drawFilters();
assert.deepEqual([...box.innerHTML.matchAll(/<select data-f="([^"]+)"/g)].map(m => m[1]), ['marketplace', 'cabinet_id', 'order_status', 'answer', 'rating']);
assert(box.innerHTML.includes('data-tags'));
console.log('PASS: question filter layout, old URL filtering, real bindings, retained filters, review regression');
