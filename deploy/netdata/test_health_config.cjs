// Static syntax and threshold checks. Live reload-health/API verification is still required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const config = fs.readFileSync(path.join(__dirname, 'health.d/wb-ops-resources.conf'), 'utf8');
const known = new Set(['template', 'on', 'class', 'type', 'component', 'chart labels', 'lookup', 'calc', 'units', 'every', 'warn', 'crit', 'delay', 'summary', 'info', 'to']);
const rules = new Map();
let rule;
for (const [i, raw] of config.split(/\r?\n/).entries()) {
  const line = raw.trim();
  if (!line || line.startsWith('#')) continue;
  const colon = line.indexOf(':');
  assert.ok(colon > 0, `line ${i + 1}: expected key: value`);
  const key = line.slice(0, colon), value = line.slice(colon + 1).trim();
  assert.ok(known.has(key) && value, `line ${i + 1}: invalid key/value`);
  if (key === 'template') {
    assert.match(value, /^wb_ops_[a-z_]+$/);
    assert.ok(!rules.has(value), `duplicate template: ${value}`);
    rule = {}; rules.set(value, rule);
  }
  assert.ok(rule, `line ${i + 1}: entity must begin with template`);
  assert.ok(!(key in rule), `line ${i + 1}: duplicate key ${key}`);
  rule[key] = value;
}
const expected = ['wb_ops_available_memory', 'wb_ops_disk_space', 'wb_ops_system_load', 'wb_ops_oom_kills'];
assert.deepEqual([...rules.keys()].sort(), [...expected].sort());
for (const rule of rules.values()) {
  for (const key of ['on', 'every', 'units', 'delay', 'summary', 'info', 'to']) assert.ok(rule[key], `missing ${key}`);
  assert.equal(rule.to, 'silent');
  assert.ok(rule.warn || rule.crit);
  assert.match(rule.every, /^\d+[sm]$/);
  assert.match(rule.delay, /down 5m/);
  if (rule.lookup) assert.match(rule.lookup, /^(average|max|sum) -\d+[sm] unaligned(?: of \w+)?$/);
}
// The absolute GiB reserve is for application filesystems, not a tiny /boot.
const diskMountPatterns = rules.get('wb_ops_disk_space')['chart labels'].replace(/^mount_point=/, '').split(/\s+/);
assert.ok(diskMountPatterns.includes('!/boot'));
assert.ok(diskMountPatterns.includes('!/boot/*'));
assert.equal(diskMountPatterns.at(-1), '*');
assert.ok(!diskMountPatterns.some(p => ['!/', '!/opt', '!/opt/*', '!/data', '!/data/*'].includes(p)), 'application filesystems must remain monitored');
// Evaluate the configured expressions with representative real-world values.
const evaluate = (expression, values) => Function('v', `return (${expression.replace(/\$([a-zA-Z_][\w.]*)/g, (_, name) => `v[${JSON.stringify(name)}]`).replace(/\bnan\b/g, 'NaN')});`)(values);
const check = (name, key, values, result) => assert.equal(evaluate(rules.get(name)[key], {WARNING: 1, CRITICAL: 2, status: 0, ...values}), result, `${name}.${key}`);
check(expected[0], 'calc', {this: 512, 'system.ram.used': 2048, 'system.ram.cached': 1024, 'system.ram.free': 512, 'system.ram.buffers': 512}, 12.5);
check(expected[0], 'warn', {this: 14}, true);
check(expected[0], 'warn', {this: 16}, false);
check(expected[0], 'warn', {this: 19, status: 1}, true);
check(expected[0], 'crit', {this: 6}, true);
check(expected[0], 'crit', {this: 9, status: 2}, true);
check(expected[1], 'warn', {this: 60, avail: 1.9}, true);
check(expected[1], 'crit', {this: 60, avail: 0.9}, true);
check(expected[1], 'warn', {this: 86, avail: 20}, true);
check(expected[1], 'crit', {this: 96, avail: 20}, true);
check(expected[1], 'warn', {this: 50, avail: 10}, false);
check(expected[1], 'crit', {this: 50, avail: 10}, false);
check(expected[2], 'calc', {this: 4, active_processors: 2}, 200);
assert.ok(Number.isNaN(evaluate(rules.get(expected[2]).calc, {this: 4, active_processors: 0})));
check(expected[2], 'warn', {this: 200}, true);
check(expected[2], 'crit', {this: 200}, false);
check(expected[2], 'crit', {this: 301}, true);
check(expected[3], 'crit', {this: 1}, true);
check(expected[3], 'crit', {this: 0}, false);
console.log('Health config static checks OK: four templates, expressions, thresholds, hysteresis, no external recipients');
