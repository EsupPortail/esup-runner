/** Live forms and broadcaster copying must hold refresh while awaiting user interaction. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function page(action = 'stop') {
  const listeners = {};
  const button = { disabled: false };
  const form = {
    dataset: { liveAction: action, liveSource: '<encoder>', liveId: 'session-id' },
    addEventListener: (name, callback) => { listeners[name] = callback; },
    querySelector: () => button,
  };
  let dialogOptions, settle, reloads = 0, submits = 0, interval;
  const window = {
    esupConfirm: options => {
      dialogOptions = options;
      return new Promise(resolve => { settle = resolve; });
    },
    setInterval: callback => { interval = callback; },
    location: { reload: () => { reloads++; } },
  };
  const toggle = { addEventListener() {}, setAttribute() {} };
  const document = {
    getElementById: name => name === 'live-refresh-toggle' ? toggle : {},
    querySelectorAll: selector => selector === '[data-live-action]' ? [form] : [],
    querySelector: () => null,
    activeElement: { tagName: 'BODY' }, hidden: false,
  };
  const context = {
    window, document,
    HTMLFormElement: { prototype: { submit() { assert.equal(this, form); submits++; } } },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../app/web/static/live.js'), 'utf8'), context);
  return {
    submit: () => listeners.submit({ preventDefault() {} }),
    confirm: answer => settle(answer), tick: () => interval(),
    options: () => dialogOptions, reloads: () => reloads, submits: () => submits, button,
  };
}

test('cancel keeps playback unchanged and resumes refresh after the modal closes', async () => {
  const ui = page();
  const pending = ui.submit();
  ui.tick();
  assert.equal(ui.reloads(), 0);
  assert.equal(ui.options().action, 'stop');
  assert.match(ui.options().warning, /Restart session/);
  ui.confirm(false);
  await pending;
  assert.equal(ui.submits(), 0);
  assert.equal(ui.button.disabled, false);
  ui.tick();
  assert.equal(ui.reloads(), 1);
});

test('confirm restart submits the protected form once and holds refresh during navigation', async () => {
  const ui = page('restart');
  const pending = ui.submit();
  await ui.submit(); // A second click must not open a second dialog.
  assert.equal(ui.options().action, 'restart');
  assert.equal(ui.options().confirmLabel, 'Restart session');
  assert.match(ui.options().warning, /new HLS URL/);
  assert.equal(ui.options().subject, '<encoder> · session-id');
  ui.confirm(true);
  await pending;
  assert.equal(ui.submits(), 1);
  assert.equal(ui.button.disabled, true);
  await ui.submit();
  ui.tick();
  assert.equal(ui.submits(), 1);
  assert.equal(ui.reloads(), 0);
});

function sourcePage(clipboard) {
  const sourceId = 'agroecologie';
  let copy, interval, reloads = 0, selected = false;
  const feedback = { textContent: '' };
  const button = {
    dataset: { copyLiveSource: sourceId },
    addEventListener: (name, callback) => { if (name === 'click') copy = callback; },
  };
  const source = {
    textContent: sourceId, tagName: 'STRONG',
    focus: () => { document.activeElement = source; },
  };
  const range = { selectNodeContents: element => { assert.equal(element, source); } };
  const selection = {
    removeAllRanges: () => { selected = false; },
    addRange: value => { assert.equal(value, range); selected = true; },
  };
  const toggle = { addEventListener() {}, setAttribute() {} };
  const document = {
    activeElement: { tagName: 'BODY' }, hidden: false,
    getElementById: name => ({
      'live-refresh-toggle': toggle,
      'live-refresh-status': {},
      'live-source-copy-status': feedback,
      [`live-source-id-${sourceId}`]: source,
    })[name],
    querySelectorAll: selector => selector === '[data-copy-live-source]' ? [button] : [],
    querySelector: () => null,
    createRange: () => range,
  };
  const window = {
    navigator: { clipboard },
    getSelection: () => selection,
    setInterval: callback => { interval = callback; },
    location: { reload: () => { reloads++; } },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../app/web/static/live.js'), 'utf8'), { window, document });
  return {
    sourceId, copy: () => copy(), tick: () => interval(), feedback, toggle,
    reloads: () => reloads, selected: () => selected, focused: () => document.activeElement === source,
  };
}

test('copy uses the broadcaster name and holds refresh until clipboard access completes', async () => {
  const copied = [];
  let complete;
  const ui = sourcePage({ writeText: value => {
    copied.push(value);
    return new Promise(resolve => { complete = resolve; });
  } });
  const pending = ui.copy();
  await ui.copy(); // Duplicate clicks must not start a second clipboard operation.
  ui.tick();
  assert.equal(ui.reloads(), 0);
  assert.deepEqual(copied, [ui.sourceId]);
  complete();
  await pending;
  assert.equal(ui.feedback.textContent, 'Broadcaster name copied to clipboard.');
  ui.tick();
  assert.equal(ui.reloads(), 1);
});

for (const [name, clipboard] of [
  ['unavailable', undefined],
  ['denied', { writeText: async () => { throw new Error('permission denied'); } }],
]) {
  test(`clipboard ${name} selects the visible broadcaster and pauses refresh for manual copying`, async () => {
    const ui = sourcePage(clipboard);
    await ui.copy();
    assert.equal(ui.focused(), true);
    assert.equal(ui.selected(), true);
    assert.equal(ui.toggle.textContent, 'Resume refresh');
    assert.match(ui.feedback.textContent, /copy it manually/);
    ui.tick();
    assert.equal(ui.reloads(), 0);
  });
}
