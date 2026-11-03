const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function loadUI() {
  const document = {
    activeElement: null,
    querySelectorAll: () => [],
    getElementById: () => null,
  };
  const context = vm.createContext({
    document, URLSearchParams, Intl,
    window: {location: {search: ""}},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../../app/static/app.js"), "utf8"), context);
  return context;
}

for (const [tag, label] of [["button", "Reset filters"], ["a", "Add downloads"]]) {
  test(`polling restores focus on the empty-state ${label} control`, () => {
    const ui = loadUI();
    const oldRow = {dataset: {empty: "downloads"}};
    ui.document.activeElement = {textContent: label, closest: () => oldRow};
    let focused = false;
    const control = {textContent: label, focus() { focused = true; }};
    const newRow = {
      dataset: {empty: "downloads"},
      querySelectorAll(selector) { return selector.split(", ").includes(tag) ? [control] : []; },
    };
    const container = {
      contains: () => true,
      replaceChildren(...rows) { this.children = rows; },
      focus() { assert.fail("The replacement control should receive focus"); },
    };
    ui.replaceRows(container, [newRow]);
    assert.equal(focused, true);
  });
}

test("focus falls back to the list when the empty-state action disappears", () => {
  const ui = loadUI();
  ui.document.activeElement = {
    textContent: "Add downloads", closest: () => ({dataset: {empty: "downloads"}}),
  };
  let focused = false;
  const container = {
    contains: () => true,
    replaceChildren(...rows) { this.children = rows; },
    focus() { focused = true; },
  };
  ui.replaceRows(container, []);
  assert.equal(focused, true);
  assert.equal(container.tabIndex, -1);
});

test("feedback transitions from failure to pending to success without stale severity", () => {
  const ui = loadUI();
  const node = {dataset: {}};
  ui.formFeedback(node, "Invalid cookie file", "error");
  assert.equal(node.dataset.state, "error");
  assert.equal(node.textContent, "Invalid cookie file");
  ui.formFeedback(node, "Cookie test queued");
  assert.equal(node.dataset.state, "neutral");
  ui.formFeedback(node, "Subscription active", "success");
  assert.equal(node.dataset.state, "success");
  assert.equal(node.textContent, "Subscription active");
});
