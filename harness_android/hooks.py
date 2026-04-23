"""JavaScript injection and API hooking via CDP.

Uses ``Page.addScriptToEvaluateOnNewDocument`` to inject hooks *before*
any page JavaScript runs.  This lets you intercept, log, or modify calls
to sensitive browser APIs (fetch, XHR, cookies, WebSocket, etc.).
"""

from __future__ import annotations

import json
from typing import Any

from harness_android.console import console

from harness_android.browser import Browser


# ======================================================================
# Built-in hook scripts
# ======================================================================

# Each hook stores intercepted data in ``window.__harness_hooks__``.

_HOOK_PREAMBLE = """
(function() {
    if (window.__harness_hooks__) return;  // already injected
    window.__harness_hooks__ = {
        xhr: [],
        fetch: [],
        cookies: [],
        websocket: [],
        postMessages: [],
        console: [],
        storage: [],
        forms: [],
    };
"""

HOOK_XHR = _HOOK_PREAMBLE + """
    var _open = XMLHttpRequest.prototype.open;
    var _send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url) {
        this._harnessMethod = method;
        this._harnessUrl = url;
        return _open.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(body) {
        window.__harness_hooks__.xhr.push({
            method: this._harnessMethod,
            url: this._harnessUrl,
            body: body ? String(body).substring(0, 4096) : null,
            timestamp: Date.now()
        });
        return _send.apply(this, arguments);
    };
})();
"""

HOOK_FETCH = _HOOK_PREAMBLE + """
    var _fetch = window.fetch;
    window.fetch = function(input, init) {
        var url = (typeof input === 'string') ? input : input.url;
        var method = (init && init.method) ? init.method : 'GET';
        var body = (init && init.body) ? String(init.body).substring(0, 4096) : null;
        window.__harness_hooks__.fetch.push({
            url: url, method: method, body: body, timestamp: Date.now()
        });
        return _fetch.apply(this, arguments);
    };
})();
"""

HOOK_COOKIES = _HOOK_PREAMBLE + """
    var _cookieDesc = Object.getOwnPropertyDescriptor(Document.prototype, 'cookie') ||
                      Object.getOwnPropertyDescriptor(HTMLDocument.prototype, 'cookie');
    if (_cookieDesc) {
        Object.defineProperty(document, 'cookie', {
            get: function() { return _cookieDesc.get.call(this); },
            set: function(val) {
                window.__harness_hooks__.cookies.push({
                    action: 'set', value: val, timestamp: Date.now()
                });
                return _cookieDesc.set.call(this, val);
            },
            configurable: true
        });
    }
})();
"""

HOOK_WEBSOCKET = _HOOK_PREAMBLE + """
    var _WS = window.WebSocket;
    window.WebSocket = function(url, protocols) {
        window.__harness_hooks__.websocket.push({
            action: 'connect', url: url, timestamp: Date.now()
        });
        var ws = protocols ? new _WS(url, protocols) : new _WS(url);
        var _send = ws.send.bind(ws);
        ws.send = function(data) {
            window.__harness_hooks__.websocket.push({
                action: 'send', url: url,
                data: String(data).substring(0, 2048), timestamp: Date.now()
            });
            return _send(data);
        };
        return ws;
    };
    window.WebSocket.prototype = _WS.prototype;
})();
"""

HOOK_POSTMESSAGE = _HOOK_PREAMBLE + """
    window.addEventListener('message', function(e) {
        window.__harness_hooks__.postMessages.push({
            origin: e.origin,
            data: JSON.stringify(e.data).substring(0, 4096),
            timestamp: Date.now()
        });
    }, true);
})();
"""

HOOK_CONSOLE = _HOOK_PREAMBLE + """
    ['log', 'warn', 'error', 'info', 'debug'].forEach(function(level) {
        var _orig = console[level];
        console[level] = function() {
            var args = Array.from(arguments).map(function(a) {
                try { return JSON.stringify(a); } catch(e) { return String(a); }
            });
            window.__harness_hooks__.console.push({
                level: level,
                args: args.join(' ').substring(0, 4096),
                timestamp: Date.now()
            });
            return _orig.apply(console, arguments);
        };
    });
})();
"""

HOOK_STORAGE = _HOOK_PREAMBLE + """
    ['localStorage', 'sessionStorage'].forEach(function(name) {
        var _store = window[name];
        if (!_store) return;
        var _setItem = _store.setItem.bind(_store);
        _store.setItem = function(key, value) {
            window.__harness_hooks__.storage.push({
                store: name, action: 'set', key: key,
                value: String(value).substring(0, 2048), timestamp: Date.now()
            });
            return _setItem(key, value);
        };
    });
})();
"""

HOOK_FORMS = _HOOK_PREAMBLE + """
    document.addEventListener('submit', function(e) {
        var form = e.target;
        var data = {};
        var inputs = form.querySelectorAll('input, textarea, select');
        inputs.forEach(function(el) {
            if (el.name) data[el.name] = el.value;
        });
        window.__harness_hooks__.forms.push({
            action: form.action, method: form.method,
            fields: data, timestamp: Date.now()
        });
    }, true);
})();
"""

# Map of hook names to scripts
BUILTIN_HOOKS: dict[str, str] = {
    "xhr": HOOK_XHR,
    "fetch": HOOK_FETCH,
    "cookies": HOOK_COOKIES,
    "websocket": HOOK_WEBSOCKET,
    "postmessage": HOOK_POSTMESSAGE,
    "console": HOOK_CONSOLE,
    "storage": HOOK_STORAGE,
    "forms": HOOK_FORMS,
}


class Hooks:
    """Manage JavaScript hooks injected into every page load.

    Usage::

        hooks = Hooks(browser)
        hooks.install("fetch", "xhr", "cookies", "forms")
        browser.navigate("https://target.example.com")
        # … interact with the page …
        data = hooks.collect()
        print(data["fetch"])   # list of intercepted fetch() calls
        print(data["forms"])   # list of captured form submissions
    """

    def __init__(self, browser: Browser):
        self.browser = browser
        self._installed: list[str] = []
        self._script_ids: list[str] = []

    def install(self, *hook_names: str) -> None:
        """Install one or more hooks by name.

        Valid names: ``xhr``, ``fetch``, ``cookies``, ``websocket``,
        ``postmessage``, ``console``, ``storage``, ``forms``, or ``all``.
        """
        names = list(hook_names)
        if "all" in names:
            names = list(BUILTIN_HOOKS.keys())

        self.browser.send("Page.enable")

        for name in names:
            script = BUILTIN_HOOKS.get(name)
            if script is None:
                console.print(f"[red]Unknown hook: {name}")
                continue
            result = self.browser.send(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": script},
            )
            self._script_ids.append(result.get("identifier", ""))
            self._installed.append(name)

        console.print(f"[green]Hooks installed: {', '.join(self._installed)}")

    def install_custom(self, name: str, script: str) -> None:
        """Install a custom JS snippet that runs on every page load."""
        result = self.browser.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": script},
        )
        self._script_ids.append(result.get("identifier", ""))
        self._installed.append(name)
        console.print(f"[green]Custom hook '{name}' installed.")

    def remove_all(self) -> None:
        """Remove all installed hooks."""
        for sid in self._script_ids:
            if sid:
                self.browser.send(
                    "Page.removeScriptToEvaluateOnNewDocument",
                    {"identifier": sid},
                )
        self._script_ids.clear()
        self._installed.clear()
        console.print("[yellow]All hooks removed.")

    def collect(self) -> dict[str, list]:
        """Retrieve all captured data from the hooks."""
        result = self.browser.evaluate_js(
            "JSON.parse(JSON.stringify(window.__harness_hooks__ || {}))"
        )
        return result or {}

    def collect_and_clear(self) -> dict[str, list]:
        """Retrieve captured data and reset the collectors."""
        data = self.collect()
        self.browser.evaluate_js("""
            if (window.__harness_hooks__) {
                Object.keys(window.__harness_hooks__).forEach(function(k) {
                    window.__harness_hooks__[k] = [];
                });
            }
        """)
        return data

    def dump(self, path: str = "hooks_data.json") -> None:
        """Collect all hook data and write to a JSON file."""
        data = self.collect()
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        total = sum(len(v) for v in data.values() if isinstance(v, list))
        console.print(f"[green]Hook data saved to {path} ({total} entries)")
