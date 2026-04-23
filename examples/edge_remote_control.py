"""Edge remote-control demo — attach without restart, inject on-load hook, navigate.

What this demonstrates
----------------------
* Non-destructive CDP attach (preserves the currently open tab / NTP)
* ``find_target`` to pick a page by URL substring
* ``inject_script_on_load`` — hook installed via CDP
  ``Page.addScriptToEvaluateOnNewDocument`` that runs before page scripts
  on *every* subsequent navigation.

Prerequisites
-------------
1. Emulator running (``harness-android start``).
2. Edge x86 build installed as ``com.microsoft.emmx.local``:
      harness-android install ChromePublic.apk
3. Edge already launched with CDP flags — either:
      harness-android -b edge-local browser cdp --prepare
      harness-android shell -- am force-stop com.microsoft.emmx.local
      harness-android shell -- am start -n com.microsoft.emmx.local/com.google.android.apps.chrome.Main
   or just run the script, which will ``enable_cdp()`` (restarts Edge).

Run with:
    poetry run python examples/edge_remote_control.py
"""

from __future__ import annotations

from harness_android.adb import ADB
from harness_android.browser import Browser

HOOK_JS = r"""
// Runs before any page script on every navigation in this target.
(() => {
    window.__harness = {
        navigations: [],
        fetch_calls: [],
    };
    window.__harness.navigations.push({t: Date.now(), url: location.href});

    const orig_fetch = window.fetch;
    window.fetch = function (...args) {
        window.__harness.fetch_calls.push({t: Date.now(), args});
        return orig_fetch.apply(this, args);
    };
})();
"""


def main() -> None:
    adb = ADB()  # auto-detect first running emulator

    b = Browser(adb, browser="edge-local")

    # Preferred: attach without restart so the user's current tab survives.
    # Falls back to enable_cdp() if no devtools socket is ready yet.
    try:
        b.attach_cdp()
    except Exception as exc:  # noqa: BLE001
        print(f"attach_cdp failed ({exc}); falling back to enable_cdp()")
        b.enable_cdp()

    # Pick a page target. ``connect()`` without a target_id auto-picks the
    # first page with a real URL, preferring non-blank tabs.
    # Pass e.g. ``url_substring="sapphire"`` to attach to a mini app.
    target_id = b.find_target(url_substring="http")  # first http(s) page
    b.connect(target_id=target_id) if target_id else b.connect()

    # Install an on-load hook that survives every navigation.
    script_id = b.inject_script_on_load(HOOK_JS)
    print(f"installed on-load hook id={script_id}")

    # Drive the browser.
    b.navigate("https://httpbin.org/html")
    print("title:", b.evaluate_js("document.title"))
    print("nav log:", b.evaluate_js("JSON.stringify(window.__harness.navigations)"))

    # Trigger a fetch the hook will observe.
    b.evaluate_js("fetch('/get').catch(() => {})", await_promise=False)
    b.evaluate_js("new Promise(r => setTimeout(r, 500))", await_promise=True)
    print(
        "fetch calls:",
        b.evaluate_js("JSON.stringify(window.__harness.fetch_calls)"),
    )

    b.page_screenshot("edge_remote_control.png")
    print("screenshot: edge_remote_control.png")

    # Clean up — remove the on-load script (hook stops firing on future pages).
    b.remove_injected_script(script_id)
    b.close()


if __name__ == "__main__":
    main()
