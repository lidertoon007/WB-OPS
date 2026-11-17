// Dependency-free smoke test: node deploy/netdata/test_dashboard.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, 'dashboard/index.html'), 'utf8');
assert.match(html, /<title>[^<]+<\/title>/);
assert.doesNotMatch(html, /(?:src|href)\s*=\s*["']https?:\/\//i);
assert.doesNotMatch(html, /@import|url\(\s*["']?https?:\/\//i);
class Element {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.style = {}; this.value = ''; this.textContent = ''; }
  append(...children) { this.children.push(...children); if (this.tag === 'select' && !this.value) this.value = children[0]?.value || ''; }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this[key] = value; }
  addEventListener() {}
  get selectedOptions() { return this.children.filter(c => c.value === this.value); }
}
const elements = new Map();
for (const match of html.matchAll(/<(\w+)[^>]*\bid="([^"]+)"/g)) elements.set(match[2], new Element(match[1]));
const period = elements.get('period'), option = new Element('option'); option.value = '900'; option.textContent = '15 минут'; period.append(option);
const charts = {}, sample = {};
function chart(id, context, units, values) { charts[id] = {id, name: id, context, units}; sample[id] = values; }
// Deliberately renamed IDs verify that discovery uses contexts, including disk/network.
chart('renamed.cpu', 'system.cpu', 'percentage', {user:20, system:5, idle:75});
chart('renamed.ram', 'system.ram', 'MiB', {free:256, used:512, cached:128, buffers:128});
chart('renamed.load', 'system.load', 'load', {load1:0.1, load5:0.2, load15:0.3});
chart('renamed.uptime', 'system.uptime', 'seconds', {uptime:90061});
chart('renamed.disk', 'disk.io', 'KiB/s', {reads:12, writes:-24});
chart('renamed.root', 'disk.space', 'GiB', {avail:7, used:2, reserved_for_root:1});
chart('renamed.net', 'net.net', 'kilobits/s', {received:1000, sent:-2000});
let mode = 'ok';
const calls = [];
const sandbox = {
  document: {getElementById:id=>elements.get(id), createElement:tag=>new Element(tag), createElementNS:(_,tag)=>new Element(tag)},
  location: {origin:'http://localhost:19999'}, URL, AbortController, setTimeout, clearTimeout, setInterval:()=>{}, console,
  fetch:async url=>{
    calls.push(url);
    if (mode === 'offline' || (mode === 'partial' && url.searchParams.get('chart') === 'renamed.cpu')) return {ok:false,status:401};
    let body;
    if (url.pathname.endsWith('/charts')) body={hostname:'test-host',charts};
    else if (url.pathname.endsWith('/alarms')) body={alarms:{a:{active:true,status:'WARNING',summary:'Проверка сети',chart:'renamed.net',value_string:'2'},b:{active:true,status:'UNINITIALIZED'}}};
    else { const values=sample[url.searchParams.get('chart')]; body={labels:['time',...Object.keys(values)],data:[[Math.floor(Date.now()/1000)-5,...Object.values(values)]]}; }
    return {ok:true,json:async()=>body};
  }
};
vm.createContext(sandbox);
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('refresh();setInterval(', 'setInterval(');
vm.runInContext(script,sandbox);
(async()=>{
  await vm.runInContext('refresh()',sandbox);
  const get=id=>elements.get(id).textContent;
  assert.equal(get('node'),'test-host');
  assert.equal(get('cpu-value'),'25 %');
  assert.equal(get('ram-value'),'50 %');
  assert.equal(get('space-value'),'20 %');
  assert.equal(get('uptime-value'),'1 д 1 ч 1 мин');
  assert.equal(get('alerts-value'),'0 / 1');
  assert.match(get('net-value'),/1 Мбит\/с \/ 2 Мбит\/с/);
  assert.match(get('io-value'),/12 КиБ\/с \/ 24 КиБ\/с/);
  for (const key of ['cpu','ram','net','io']) assert.equal(elements.get(key+'-chart').children[0].tag,'svg');
  assert.ok(calls.every(url=>url.origin==='http://localhost:19999'));
  assert.ok(calls.filter(url=>url.pathname.endsWith('/data')).every(url=>url.searchParams.get('after')==='-900'));
  assert.equal(elements.get('disk').value,'renamed.disk');
  assert.equal(elements.get('network').value,'renamed.net');
  mode='partial'; await vm.runInContext('refresh()',sandbox);
  assert.equal(get('cpu-value'),'—'); assert.equal(get('ram-value'),'50 %'); assert.match(get('status'),/часть метрик недоступна/);
  delete charts['renamed.disk']; delete charts['renamed.net'];
  mode='ok'; await vm.runInContext('state.discoveredAt=0;refresh()',sandbox);
  assert.equal(get('io-value'),'—'); assert.match(get('io-sub'),/Метрика не найдена/);
  mode='offline'; await vm.runInContext('state.discoveredAt=0;refresh()',sandbox);
  assert.match(get('status'),/Нет связи/); assert.equal(get('ram-value'),'—');
  console.log('Dashboard smoke tests OK: discovery, metrics, charts, partial/missing/offline states');
})().catch(error=>{console.error(error);process.exitCode=1;});
