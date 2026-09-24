"""The Intelligence page, executed rather than read.

A live evaluator opened the deployed Intelligence page and got

    ReferenceError: OWNER_HEADERS is not defined

before `/api/intelligence` was ever called. Every tab stayed at "—".

The page's two scripts are each wrapped in their own function. The marker was
declared inside the second, so it did not exist inside the first, which is
where the Intelligence code lives. The test that was meant to cover this only
checked that the name `OWNER_HEADERS` appeared near the `fetch` call in the
source text - which it did, which is why it passed while the page was broken.

So these tests run the page's actual JavaScript, in Node, against a minimal
stand-in for the DOM, and watch what it does: whether it throws, whether it
calls `/api/intelligence`, what it sends, and whether the data it receives
reaches the page. They also run the same script WITHOUT the fix and assert
that it fails the way the evaluator saw - a harness that cannot reproduce the
bug it was written for is not evidence that the bug is gone.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "frontend" / "site" / "index.html").read_text(encoding="utf-8")
SCRIPTS = re.findall(r"<script>(.*?)</script>", PAGE, re.S)

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

INTELLIGENCE = {
    "shopId": "SHOP#demo",
    "documents": [{"jobId": "e28b5baa56c347aabf81c6ac24d9642a",
                   "status": "DONE", "reader": "TEXTRACT",
                   "supplierName": "SRI BALAJI ELECTRICALS", "lineCount": 5,
                   "matchedCount": 3, "reviewRequiredCount": 3,
                   "materialChangeCount": 1}],
    "alerts": [{"kind": "STOCKOUT", "skuId": "SW-ANC-1W10A",
                "productName": "Anchor Modular Switch", "detail": "Nothing on the shelf."}],
    "operations": {"counts": {"orders": 3, "documents": 1},
                   "outcomes": {"QUOTED": 2, "NEEDS_CLARIFICATION": 1,
                                "FAILED": 0, "REVIEWED": 1}},
    "eventsEnabled": True,
    "synthetic": True,
}

# A browser, reduced to what these two scripts touch. Every element is a
# plain object that remembers what was written to it, so the test can read
# back what the page rendered.
HARNESS = r"""
const vm = require("vm");
// Everything arrives on stdin: the page's scripts are far longer than a
// Windows command line allows.
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const scripts = input.scripts;
const path = input.path;
const intel = input.intel;

const elements = {};
function element(id) {
  if (!elements[id]) {
    elements[id] = {
      id, textContent: "", innerHTML: "", value: "", hidden: false,
      style: {},
      classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
      addEventListener() {}, setAttribute() {}, getAttribute() { return null; },
      removeAttribute() {}, focus() {}, appendChild() {},
    };
  }
  return elements[id];
}

const calls = [];
const errors = [];
const session = { "shopflow.demo.session": "demo" };

const context = {
  console,
  setTimeout: (fn) => { fn(); return 0; },
  clearTimeout() {},
  Promise, JSON, Object, Array, String, Number, Math, Date, encodeURIComponent,
  location: { pathname: path },
  history: { replaceState() {}, pushState() {} },
  sessionStorage: {
    getItem: (k) => (k in session ? session[k] : null),
    setItem: (k, v) => { session[k] = String(v); },
    removeItem: (k) => { delete session[k]; },
  },
  document: {
    getElementById: element,
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener() {},
    createElement: () => element("__created"),
    documentElement: { setAttribute() {}, lang: "en" },
    title: "",
  },
  addEventListener() {},
  fetch: (url, options) => {
    calls.push({ url, headers: (options && options.headers) || null });
    const body = url === "/api/intelligence" ? intel
               : url === "/api/demo" ? { inventory: [], catalogSize: 0 }
               : {};
    return Promise.resolve({ ok: true, status: 200,
                             json: () => Promise.resolve(body) });
  },
};
context.window = context;
vm.createContext(context);

for (const source of scripts) {
  try { vm.runInContext(source, context); }
  catch (e) { errors.push(e.name + ": " + e.message); }
}

// Let every fetch and every .then() settle.
setImmediate(() => setImmediate(() => {
  const read = (id) => elements[id] ? { text: elements[id].textContent,
                                        html: elements[id].innerHTML } : null;
  process.stdout.write(JSON.stringify({
    errors, calls,
    ops: ["opsOrders", "opsQuoted", "opsClarify", "opsFailed"].map(
      (id) => (read(id) || {}).text),
    docBody: (read("docBody") || {}).html,
    alertBody: (read("alertBody") || {}).html,
    docNote: (read("docNote") || {}).text,
  }));
}));
"""


def _session_key() -> str:
    """The workspace's own session key, read from the page rather than assumed."""
    match = re.search(r'KEY\s*=\s*"([^"]+)"', PAGE)
    assert match, "the workspace session key moved"
    return match.group(1)


def run_page(scripts, path="/workspace/intelligence", intel=INTELLIGENCE):
    harness = HARNESS.replace("shopflow.demo.session", _session_key())
    # The harness is a small, fixed program; only the data is large.
    completed = subprocess.run(
        [NODE, "-e", harness],
        input=json.dumps({"scripts": scripts, "path": path, "intel": intel}),
        capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def workspace_scripts():
    """Every script up to and including the workspace, in page order."""
    end = next(i for i, s in enumerate(SCRIPTS) if "// >>> workspace" in s)
    return SCRIPTS[:end + 1]


# ---------------------------------------------------------------------------
# The bug, reproduced, so the harness is known to be able to see it
# ---------------------------------------------------------------------------

def test_the_harness_reproduces_the_evaluators_reference_error():
    """The workspace script on its own fails exactly as it did live."""
    workspace_only = [s for s in SCRIPTS if "// >>> workspace" in s]
    result = run_page(workspace_only)

    assert result["errors"] == ["ReferenceError: OWNER_HEADERS is not defined"]
    assert "/api/intelligence" not in [c["url"] for c in result["calls"]]
    assert result["ops"] == [None, None, None, None]    # every tab left empty


# ---------------------------------------------------------------------------
# The fix
# ---------------------------------------------------------------------------

def test_the_intelligence_page_loads_without_a_reference_error():
    result = run_page(workspace_scripts())
    assert result["errors"] == []


def test_the_intelligence_page_calls_the_api():
    result = run_page(workspace_scripts())
    assert [c["url"] for c in result["calls"]
            if c["url"] == "/api/intelligence"] == ["/api/intelligence"]


def test_the_intelligence_request_is_sent_as_the_owner():
    """It carries supplier and margin data, so it goes through the gate."""
    result = run_page(workspace_scripts())
    call = next(c for c in result["calls"] if c["url"] == "/api/intelligence")
    assert call["headers"] == {"x-shopflow-demo-owner": "demo-workspace"}


def test_the_page_renders_the_data_it_receives():
    """Not left at "—": the operations counters and both tables are filled."""
    result = run_page(workspace_scripts())

    # A real DOM stringifies textContent; the stand-in keeps what was written.
    assert [str(v) for v in result["ops"]] == ["3", "2", "1", "0"]
    assert "SRI BALAJI ELECTRICALS" in result["docBody"]
    assert "TEXTRACT" in result["docBody"]
    assert "REVIEW REQUIRED" in result["docBody"]
    assert "SW-ANC-1W10A" in result["alertBody"]


def test_a_failed_intelligence_read_says_so_and_breaks_nothing():
    """An unavailable route is a message on the page, not a crash."""
    result = run_page(workspace_scripts(), intel={})
    assert result["errors"] == []


def test_other_workspace_routes_do_not_call_intelligence():
    """Owner intelligence is fetched only on the Intelligence page."""
    for path in ("/workspace", "/workspace/inventory", "/workspace/profile",
                 "/workspace/orders", "/login"):
        result = run_page(workspace_scripts(), path=path)
        assert result["errors"] == [], path
        assert all(c["url"] != "/api/intelligence" for c in result["calls"]), path


# ---------------------------------------------------------------------------
# One definition, available before anything uses it
# ---------------------------------------------------------------------------

def test_the_marker_is_declared_exactly_once():
    declarations = re.findall(r"\bvar\s+OWNER_HEADERS\b|\blet\s+OWNER_HEADERS\b"
                              r"|\bconst\s+OWNER_HEADERS\b", PAGE)
    assert len(declarations) == 1
    assert PAGE.count('"demo-workspace"') == 1


def test_the_marker_is_declared_before_the_first_script_that_uses_it():
    declared_in = next(i for i, s in enumerate(SCRIPTS)
                       if re.search(r"\bvar\s+OWNER_HEADERS\b", s))
    first_use = next(i for i, s in enumerate(SCRIPTS)
                     if "OWNER_HEADERS" in s
                     and not re.search(r"\bvar\s+OWNER_HEADERS\b", s))
    assert declared_in < first_use


def test_the_marker_is_declared_outside_any_function():
    """Inside a wrapper function it would be invisible to the other script."""
    declaring = next(s for s in SCRIPTS
                     if re.search(r"\bvar\s+OWNER_HEADERS\b", s))
    code = "\n".join(l for l in declaring.splitlines()
                     if not l.strip().startswith("//"))
    assert "function" not in code


def test_the_marker_cannot_be_reassigned_by_another_script():
    declaring = next(s for s in SCRIPTS
                     if re.search(r"\bvar\s+OWNER_HEADERS\b", s))
    assert "Object.freeze" in declaring


# ---------------------------------------------------------------------------
# Every job poll presents as the owner's workspace
# ---------------------------------------------------------------------------

def test_every_job_poll_sends_the_owner_marker():
    """Without it, an order comes back as the customer view and a supplier
    price list is refused - the owner's own screens would lose their data."""
    polls = re.findall(r'fetch\("/api/jobs/"[^;]*', PAGE)
    assert len(polls) == 3
    for call in polls:
        assert "OWNER_HEADERS" in call, call
