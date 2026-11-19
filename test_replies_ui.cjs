const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

test('received-color phrase appears in the remarks editor', async () => {
  const panels = new Map();
  const element = () => {
    const children = new Map();
    return {
      style: {}, innerHTML: '',
      querySelector(selector) {
        if (!children.has(selector)) children.set(selector, element());
        return children.get(selector);
      },
      querySelectorAll: () => [],
    };
  };
  const ai = element(), form = element();
  for (const key of ['provider', 'base_url', 'model', 'api_key']) form[key] = {value: '', placeholder: ''};
  ai.querySelector = selector => selector === '[data-ai-form]' ? form : element();
  panels.set('aiConnectionPanel', ai);
  const context = {
    window: {}, setInterval() {}, clearInterval() {},
    document: {
      querySelector: selector => selector === 'main.shell' ? {append: node => panels.set(node.id, node)} : null,
      getElementById: id => panels.get(id), createElement: element, head: {appendChild() {}},
    },
    fetch: async url => ({ok: true, json: async () => {
      if (url === '/api/templates/phrases') return {phrases: [{
        key: 'wrong_color_received', title: 'Прислали не тот цвет', variants: ['Цвет не совпал с заказом.'],
      }]};
      if (url === '/api/replies/review-mode') return {mode: 'individual', ai: {}};
      if (url === '/api/ai/settings') return {provider: 'anthropic', base_url: '', model: '', has_key: false};
      throw new Error(`Unexpected API request: ${url}`);
    }}),
  };
  vm.runInNewContext(fs.readFileSync('app/static/replies.js', 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  const html = panels.get('reviewRepliesPanel').querySelector('[data-rr-phrases]').innerHTML;
  const remarks = html.match(/<h4>Замечания<\/h4>([\s\S]*?)(?=<h4>|$)/);
  assert.ok(remarks, 'remarks group is rendered');
  assert.match(remarks[1], /data-rr-key="wrong_color_received"/);
  assert.match(remarks[1], /Цвет не совпал с заказом\./);
  assert.equal((html.match(/data-rr-key="wrong_color_received"/g) || []).length, 1);
  assert.doesNotMatch(html, /wrong_color_sent/);
});
