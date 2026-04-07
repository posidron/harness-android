"""Reconnaissance: spider, fingerprinting, storage extraction, CSP/CORS analysis."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from rich.console import Console
from rich.table import Table

from android_harness.browser import Browser

console = Console()


# ======================================================================
# Page fingerprinting
# ======================================================================

@dataclass
class PageFingerprint:
    url: str
    title: str = ""
    server: str = ""
    powered_by: str = ""
    generator: str = ""
    frameworks: list[str] = field(default_factory=list)
    csp: str = ""
    cors: str = ""
    headers: dict[str, str] = field(default_factory=dict)


def fingerprint_page(browser: Browser) -> PageFingerprint:
    """Gather technology fingerprint from the current page."""
    fp = PageFingerprint(url=browser.get_page_url())
    fp.title = browser.get_page_title()

    # Meta tags
    fp.generator = browser.evaluate_js(
        "(() => { var m = document.querySelector('meta[name=generator]'); return m ? m.content : ''; })()"
    ) or ""

    # Detect common JS frameworks
    checks = {
        "React": "!!window.React || !!document.querySelector('[data-reactroot]')",
        "Angular": "!!window.ng || !!document.querySelector('[ng-version]')",
        "Vue": "!!window.__VUE__ || !!window.Vue",
        "jQuery": "!!window.jQuery",
        "Next.js": "!!document.querySelector('#__next')",
        "Nuxt": "!!window.__NUXT__",
        "Svelte": "!!document.querySelector('[class*=\"svelte\"]')",
        "Bootstrap": "!!document.querySelector('link[href*=\"bootstrap\"]') || typeof bootstrap !== 'undefined'",
        "Tailwind": "!!document.querySelector('[class*=\"tw-\"]') || !!document.querySelector('style[data-tw]')",
    }
    for name, expr in checks.items():
        try:
            if browser.evaluate_js(expr):
                fp.frameworks.append(name)
        except Exception:  # noqa: BLE001
            pass

    # Response headers via CDP
    try:
        browser.send("Network.enable")
        # Read from performance entries as a proxy for response headers
        headers_js = """
        (() => {
            var entries = performance.getEntriesByType('navigation');
            if (entries.length && entries[0].serverTiming) {
                return entries[0].serverTiming.map(t => t.name);
            }
            return [];
        })()
        """
        browser.evaluate_js(headers_js)
    except Exception:  # noqa: BLE001
        pass

    return fp


def print_fingerprint(fp: PageFingerprint) -> None:
    table = Table(title=f"Fingerprint: {fp.url}")
    table.add_column("Property", style="bold")
    table.add_column("Value")
    table.add_row("Title", fp.title)
    if fp.generator:
        table.add_row("Generator", fp.generator)
    if fp.frameworks:
        table.add_row("Frameworks", ", ".join(fp.frameworks))
    if fp.server:
        table.add_row("Server", fp.server)
    if fp.csp:
        table.add_row("CSP", fp.csp[:120] + ("…" if len(fp.csp) > 120 else ""))
    console.print(table)


# ======================================================================
# Link/form spider
# ======================================================================

@dataclass
class SpiderResult:
    base_url: str
    links: list[dict[str, str]] = field(default_factory=list)      # href, text
    forms: list[dict[str, Any]] = field(default_factory=list)       # action, method, fields
    scripts: list[str] = field(default_factory=list)                # src URLs
    iframes: list[str] = field(default_factory=list)


def spider_page(browser: Browser) -> SpiderResult:
    """Extract all links, forms, scripts, and iframes from the current page."""
    base_url = browser.get_page_url()
    result = SpiderResult(base_url=base_url)

    # Links
    links_raw = browser.evaluate_js("""
        Array.from(document.querySelectorAll('a[href]')).map(a => ({
            href: a.href,
            text: a.innerText.trim().substring(0, 200)
        }))
    """) or []
    result.links = links_raw

    # Forms
    forms_raw = browser.evaluate_js("""
        Array.from(document.querySelectorAll('form')).map(f => ({
            action: f.action,
            method: (f.method || 'GET').toUpperCase(),
            fields: Array.from(f.querySelectorAll('input,textarea,select')).map(el => ({
                name: el.name || '',
                type: el.type || el.tagName.toLowerCase(),
                id: el.id || '',
                value: el.value || ''
            }))
        }))
    """) or []
    result.forms = forms_raw

    # Scripts
    scripts_raw = browser.evaluate_js("""
        Array.from(document.querySelectorAll('script[src]')).map(s => s.src)
    """) or []
    result.scripts = scripts_raw

    # Iframes
    iframes_raw = browser.evaluate_js("""
        Array.from(document.querySelectorAll('iframe[src]')).map(f => f.src)
    """) or []
    result.iframes = iframes_raw

    return result


def print_spider(sp: SpiderResult) -> None:
    console.print(f"\n[bold]Spider results for {sp.base_url}\n")

    if sp.links:
        t = Table(title=f"Links ({len(sp.links)})")
        t.add_column("URL", max_width=80)
        t.add_column("Text", max_width=40)
        for link in sp.links[:50]:
            t.add_row(link.get("href", ""), link.get("text", ""))
        console.print(t)

    if sp.forms:
        t = Table(title=f"Forms ({len(sp.forms)})")
        t.add_column("Action", max_width=60)
        t.add_column("Method")
        t.add_column("Fields")
        for form in sp.forms:
            fields_str = ", ".join(
                f.get("name", "?") for f in form.get("fields", []) if f.get("name")
            )
            t.add_row(form.get("action", ""), form.get("method", ""), fields_str)
        console.print(t)

    if sp.scripts:
        t = Table(title=f"Scripts ({len(sp.scripts)})")
        t.add_column("Source")
        for src in sp.scripts[:30]:
            t.add_row(src)
        console.print(t)

    if sp.iframes:
        t = Table(title=f"Iframes ({len(sp.iframes)})")
        t.add_column("Source")
        for src in sp.iframes:
            t.add_row(src)
        console.print(t)


# ======================================================================
# Storage extraction
# ======================================================================

def extract_storage(browser: Browser) -> dict[str, Any]:
    """Dump cookies, localStorage, and sessionStorage."""
    data: dict[str, Any] = {}

    # Cookies via CDP
    data["cookies"] = browser.get_cookies()

    # localStorage
    data["localStorage"] = browser.evaluate_js("""
        (() => {
            var d = {};
            for (var i = 0; i < localStorage.length; i++) {
                var k = localStorage.key(i);
                d[k] = localStorage.getItem(k);
            }
            return d;
        })()
    """) or {}

    # sessionStorage
    data["sessionStorage"] = browser.evaluate_js("""
        (() => {
            var d = {};
            for (var i = 0; i < sessionStorage.length; i++) {
                var k = sessionStorage.key(i);
                d[k] = sessionStorage.getItem(k);
            }
            return d;
        })()
    """) or {}

    return data


def print_storage(data: dict[str, Any]) -> None:
    cookies = data.get("cookies", [])
    if cookies:
        t = Table(title=f"Cookies ({len(cookies)})")
        t.add_column("Name")
        t.add_column("Value", max_width=50)
        t.add_column("Domain")
        t.add_column("Secure")
        t.add_column("HttpOnly")
        for c in cookies:
            t.add_row(
                c.get("name", ""),
                str(c.get("value", ""))[:50],
                c.get("domain", ""),
                str(c.get("secure", False)),
                str(c.get("httpOnly", False)),
            )
        console.print(t)

    ls = data.get("localStorage", {})
    if ls:
        t = Table(title=f"localStorage ({len(ls)} keys)")
        t.add_column("Key")
        t.add_column("Value", max_width=60)
        for k, v in list(ls.items())[:30]:
            t.add_row(k, str(v)[:60])
        console.print(t)

    ss = data.get("sessionStorage", {})
    if ss:
        t = Table(title=f"sessionStorage ({len(ss)} keys)")
        t.add_column("Key")
        t.add_column("Value", max_width=60)
        for k, v in list(ss.items())[:30]:
            t.add_row(k, str(v)[:60])
        console.print(t)


# ======================================================================
# CSP analysis
# ======================================================================

def analyze_csp(browser: Browser) -> dict[str, Any]:
    """Extract and parse Content-Security-Policy from the current page."""
    # Check meta tag
    csp_meta = browser.evaluate_js(
        "(() => { var m = document.querySelector('meta[http-equiv=\"Content-Security-Policy\"]'); "
        "return m ? m.content : ''; })()"
    ) or ""

    result: dict[str, Any] = {
        "csp_meta": csp_meta,
        "directives": {},
        "issues": [],
    }

    csp = csp_meta
    if not csp:
        result["issues"].append("No CSP found (neither header nor meta tag detected)")
        return result

    # Parse directives
    for directive in csp.split(";"):
        directive = directive.strip()
        if not directive:
            continue
        parts = directive.split()
        name = parts[0]
        values = parts[1:]
        result["directives"][name] = values

        # Check for insecure patterns
        if "'unsafe-inline'" in values:
            result["issues"].append(f"{name} allows 'unsafe-inline' — XSS risk")
        if "'unsafe-eval'" in values:
            result["issues"].append(f"{name} allows 'unsafe-eval' — code injection risk")
        if "*" in values:
            result["issues"].append(f"{name} uses wildcard '*' — overly permissive")
        if "data:" in values and name == "script-src":
            result["issues"].append(f"{name} allows 'data:' — XSS bypass risk")

    if "script-src" not in result["directives"]:
        result["issues"].append("No script-src directive — falls back to default-src")
    if "default-src" not in result["directives"]:
        result["issues"].append("No default-src directive")

    return result


def print_csp(csp_data: dict[str, Any]) -> None:
    directives = csp_data.get("directives", {})
    if directives:
        t = Table(title="CSP Directives")
        t.add_column("Directive", style="bold")
        t.add_column("Values")
        for name, vals in directives.items():
            t.add_row(name, " ".join(vals))
        console.print(t)

    issues = csp_data.get("issues", [])
    if issues:
        console.print("\n[bold red]CSP Issues:")
        for issue in issues:
            console.print(f"  [red]• {issue}")
    elif directives:
        console.print("[green]No obvious CSP issues found.")
    else:
        console.print("[yellow]No CSP policy detected.")


# ======================================================================
# Full recon report
# ======================================================================

def full_recon(browser: Browser, output: str | None = None) -> dict[str, Any]:
    """Run fingerprint + spider + storage + CSP analysis."""
    console.print("[bold]Running full reconnaissance …\n")

    fp = fingerprint_page(browser)
    print_fingerprint(fp)

    sp = spider_page(browser)
    print_spider(sp)

    storage = extract_storage(browser)
    print_storage(storage)

    csp = analyze_csp(browser)
    print_csp(csp)

    report = {
        "url": fp.url,
        "fingerprint": {
            "title": fp.title,
            "generator": fp.generator,
            "frameworks": fp.frameworks,
        },
        "spider": {
            "links": len(sp.links),
            "forms": sp.forms,
            "scripts": sp.scripts,
            "iframes": sp.iframes,
        },
        "storage": {
            "cookies": len(storage.get("cookies", [])),
            "localStorage_keys": len(storage.get("localStorage", {})),
            "sessionStorage_keys": len(storage.get("sessionStorage", {})),
        },
        "csp": csp,
    }

    if output:
        with open(output, "w") as f:
            json.dump(report, f, indent=2, default=str)
        console.print(f"\n[green]Recon report saved to {output}")

    return report
