"""Reconnaissance: spider, fingerprinting, security headers, storage extraction, CSP/CORS analysis."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from rich.table import Table

from harness_android.browser import Browser
from harness_android.console import console



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
    technologies: list[str] = field(default_factory=list)
    meta_tags: dict[str, str] = field(default_factory=dict)
    csp: str = ""
    cors: str = ""
    headers: dict[str, str] = field(default_factory=dict)


_FRAMEWORK_CHECKS: dict[str, str] = {
    "React": "!!window.React || !!window.__REACT_DEVTOOLS_GLOBAL_HOOK__ || !!document.querySelector('[data-reactroot],[data-reactid]')",
    "Angular": "!!window.ng || !!window.getAllAngularRootElements || !!document.querySelector('[ng-version],[ng-app]')",
    "Vue 2": "!!window.Vue && !window.__VUE__",
    "Vue 3": "!!window.__VUE__",
    "jQuery": "!!window.jQuery",
    "Next.js": "!!window.__NEXT_DATA__ || !!document.querySelector('#__next')",
    "Nuxt": "!!window.__NUXT__ || !!window.$nuxt",
    "Svelte": "!!document.querySelector('[class*=\"svelte\"]')",
    "Ember": "!!window.Ember",
    "Backbone": "!!window.Backbone",
    "Alpine.js": "!!window.Alpine",
    "Lit": "!!window.litElementVersions",
    "Preact": "!!window.preact",
    "Solid": "!!window._$HY",
    "Remix": "!!window.__remixContext",
    "Gatsby": "!!document.querySelector('#___gatsby')",
    "Astro": "!!document.querySelector('[data-astro-cid]')",
    "Bootstrap": "!!document.querySelector('link[href*=\"bootstrap\"]') || typeof bootstrap !== 'undefined'",
    "Tailwind": "(()=>{ var s=document.querySelectorAll('[class]'); for(var i=0;i<Math.min(s.length,50);i++){if(/\\b(flex|grid|mt-|px-|py-|text-|bg-|rounded|shadow)/.test(s[i].className)) return true;} return false; })()",
    "Material UI": "!!document.querySelector('[class*=\"MuiButton\"],[class*=\"css-\"]')",
    "Ant Design": "!!document.querySelector('[class*=\"ant-\"]')",
    "WordPress": "!!document.querySelector('link[href*=\"wp-content\"]')",
    "Shopify": "!!window.Shopify",
    "Wix": "!!window.wixBiSession",
    "Webflow": "!!document.querySelector('html[data-wf-site]')",
    "Firebase": "!!window.firebase || !!window.__FIREBASE_DEFAULTS__",
    "Sentry": "!!window.__SENTRY__ || !!window.Sentry",
    "Google Analytics": "!!window.ga || !!window.gtag || !!window.dataLayer",
    "Stripe": "!!window.Stripe",
    "reCAPTCHA": "!!window.grecaptcha",
    "Socket.io": "!!window.io && typeof window.io === 'function'",
}

_SCRIPT_URL_TECH: dict[str, str] = {
    "Google Analytics": r"google-analytics\.com|googletagmanager\.com|gtag/js",
    "Facebook Pixel": r"connect\.facebook\.net/.*fbevents",
    "Hotjar": r"static\.hotjar\.com",
    "Sentry": r"browser\.sentry-cdn\.com|sentry\.io",
    "Stripe": r"js\.stripe\.com",
    "Segment": r"cdn\.segment\.com",
    "Amplitude": r"cdn\.amplitude\.com",
    "Firebase": r"firebase.*\.js|firebaseapp\.com",
    "reCAPTCHA": r"google\.com/recaptcha|gstatic\.com/recaptcha",
    "hCaptcha": r"hcaptcha\.com",
    "Cloudflare": r"cdnjs\.cloudflare\.com",
    "TikTok Pixel": r"analytics\.tiktok\.com",
    "LinkedIn Insight": r"snap\.licdn\.com",
}


def fingerprint_page(browser: Browser) -> PageFingerprint:
    """Gather technology fingerprint from the current page."""
    fp = PageFingerprint(url=browser.get_page_url())
    fp.title = browser.get_page_title()

    # All meta tags
    fp.meta_tags = browser.evaluate_js("""
        (() => {
            var tags = {};
            document.querySelectorAll('meta[name],meta[property],meta[http-equiv]').forEach(m => {
                var key = m.getAttribute('name') || m.getAttribute('property') || m.getAttribute('http-equiv');
                if (key) tags[key] = m.content || '';
            });
            return tags;
        })()
    """) or {}
    fp.generator = fp.meta_tags.get("generator", "")

    # JS framework detection
    for name, expr in _FRAMEWORK_CHECKS.items():
        try:
            if browser.evaluate_js(expr):
                fp.frameworks.append(name)
        except Exception:  # noqa: BLE001
            pass

    # Tech from script URLs
    scripts = browser.evaluate_js(
        "Array.from(document.querySelectorAll('script[src]')).map(s => s.src)"
    ) or []
    for script_url in scripts:
        for tech_name, pattern in _SCRIPT_URL_TECH.items():
            if re.search(pattern, script_url, re.IGNORECASE):
                if tech_name not in fp.technologies:
                    fp.technologies.append(tech_name)

    # Network protocol info
    try:
        browser.send("Network.enable")
        perf = browser.evaluate_js("""
            (() => {
                var e = performance.getEntriesByType('navigation');
                return e.length ? {protocol: e[0].nextHopProtocol || ''} : {};
            })()
        """)
        if perf:
            fp.headers["_protocol"] = perf.get("protocol", "")
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
        table.add_row("JS Frameworks", ", ".join(fp.frameworks))
    if fp.technologies:
        table.add_row("3rd-party Tech", ", ".join(fp.technologies))
    if fp.server:
        table.add_row("Server", fp.server)
    if fp.headers.get("_protocol"):
        table.add_row("Protocol", fp.headers["_protocol"])
    interesting = {k: v for k, v in fp.meta_tags.items()
                   if k in ("viewport", "robots", "description", "csrf-token", "og:title", "twitter:card")}
    for k, v in interesting.items():
        table.add_row(f"meta:{k}", v[:80])
    console.print(table)


# ======================================================================
# Security headers analysis
# ======================================================================

@dataclass
class SecurityHeadersResult:
    headers: dict[str, str] = field(default_factory=dict)
    present: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    issues: list[dict[str, str]] = field(default_factory=list)


_SECURITY_HEADERS = {
    "Strict-Transport-Security": ("HSTS — forces HTTPS", "high"),
    "X-Content-Type-Options": ("Prevents MIME sniffing", "medium"),
    "X-Frame-Options": ("Clickjacking protection", "medium"),
    "Referrer-Policy": ("Controls referrer leakage", "medium"),
    "Permissions-Policy": ("Controls browser features", "medium"),
    "Content-Security-Policy": ("XSS/injection protection", "high"),
    "Cross-Origin-Opener-Policy": ("COOP — isolates context", "medium"),
    "Cross-Origin-Embedder-Policy": ("COEP — cross-origin embedding", "medium"),
}


def _capture_main_frame_headers(browser: Browser) -> dict[str, str]:
    """Return the main-frame response headers captured during the last
    :meth:`Browser.navigate` call.

    A page-side ``fetch(url, {method:'HEAD'})`` would be a *second* request
    subject to CORS filtering and potentially a different cache/middleware
    path, so the result wouldn't necessarily match what the browser
    actually received for the document.  :class:`Browser` subscribes to
    ``Network.responseReceived`` during navigation and stashes the
    Document-typed response headers on ``browser.main_frame_response_headers``
    \u2014 we just read them back here.

    If the caller never navigated through the harness (``attach_cdp`` +
    manual driving), the cache will be empty; we return ``{}`` and callers
    surface a clear "no CSP / headers found" message instead of lying
    with CORS-stripped data.
    """
    return dict(browser.main_frame_response_headers or {})


def analyze_security_headers(browser: Browser) -> SecurityHeadersResult:
    """Audit the main-frame response headers captured at navigation time.

    Requires that the page was loaded via :meth:`Browser.navigate` in the
    current session (that populates ``browser.main_frame_response_headers``).
    If no navigation has happened yet, the report will flag every security
    header as missing \u2014 that is the honest answer, rather than fabricating
    results from a fresh HEAD request that the server might answer
    differently.
    """
    result = SecurityHeadersResult()
    result.headers = _capture_main_frame_headers(browser)

    for header_name, (desc, severity) in _SECURITY_HEADERS.items():
        found = any(k.lower() == header_name.lower() for k in result.headers)
        if found:
            result.present.append(header_name)
        else:
            result.missing.append(header_name)
            result.issues.append({"header": header_name, "severity": severity, "issue": f"Missing {header_name} — {desc}"})

    # Info disclosure
    for leak in ("Server", "X-Powered-By", "X-AspNet-Version"):
        for k, v in result.headers.items():
            if k.lower() == leak.lower():
                result.issues.append({"header": leak, "severity": "low", "issue": f"Info disclosure: {leak}: {v}"})

    return result


def print_security_headers(sh: SecurityHeadersResult) -> None:
    if sh.present:
        t = Table(title=f"Security Headers Present ({len(sh.present)})")
        t.add_column("Header", style="green bold")
        t.add_column("Value", max_width=60)
        for h in sh.present:
            val = next((v for k, v in sh.headers.items() if k.lower() == h.lower()), "")
            t.add_row(h, val[:60])
        console.print(t)
    if sh.missing:
        t = Table(title=f"[red]Missing Security Headers ({len(sh.missing)})")
        t.add_column("Header", style="red bold")
        t.add_column("Why it matters")
        for h in sh.missing:
            desc = _SECURITY_HEADERS.get(h, ("", ""))[0]
            t.add_row(h, desc)
        console.print(t)


# ======================================================================
# Cookie security analysis
# ======================================================================

def analyze_cookies(browser: Browser) -> list[dict[str, Any]]:
    """Analyze cookies for security issues."""
    cookies = browser.get_cookies()
    issues: list[dict[str, Any]] = []
    for c in cookies:
        name = c.get("name", "")
        ci: list[str] = []
        if not c.get("secure", False):
            ci.append("Missing Secure flag")
        if not c.get("httpOnly", False):
            if any(kw in name.lower() for kw in ("session", "token", "auth", "csrf", "jwt", "sid")):
                ci.append("Session cookie missing HttpOnly — XSS accessible")
        if c.get("sameSite") == "None":
            ci.append("SameSite=None — sent on cross-site requests")
        if ci:
            issues.append({"name": name, "domain": c.get("domain", ""), "issues": ci})
    return issues


def print_cookie_issues(issues: list[dict[str, Any]]) -> None:
    if not issues:
        console.print("[green]No cookie security issues found.")
        return
    t = Table(title=f"Cookie Security Issues ({len(issues)})")
    t.add_column("Cookie", style="bold", max_width=30)
    t.add_column("Domain", max_width=25)
    t.add_column("Issues")
    for ci in issues:
        t.add_row(ci["name"], ci["domain"], "\n".join(ci["issues"]))
    console.print(t)


# ======================================================================
# Link/form spider
# ======================================================================

@dataclass
class SpiderResult:
    base_url: str
    links: list[dict[str, str]] = field(default_factory=list)
    external_links: list[dict[str, str]] = field(default_factory=list)
    forms: list[dict[str, Any]] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    iframes: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    api_endpoints: list[str] = field(default_factory=list)


def spider_page(browser: Browser) -> SpiderResult:
    """Extract links, forms, scripts, iframes, comments, emails, API endpoints."""
    base_url = browser.get_page_url()
    base_domain = urlparse(base_url).netloc
    result = SpiderResult(base_url=base_url)

    data = browser.evaluate_js("""
        (() => {
            var allLinks = Array.from(document.querySelectorAll('a[href]')).map(a => ({
                href: a.href, text: a.innerText.trim().substring(0, 200), rel: a.rel || ''
            }));
            var forms = Array.from(document.querySelectorAll('form')).map(f => ({
                action: f.action,
                method: (f.method || 'GET').toUpperCase(),
                hasFileUpload: !!f.querySelector('input[type=file]'),
                fields: Array.from(f.querySelectorAll('input,textarea,select')).map(el => ({
                    name: el.name || '', type: el.type || el.tagName.toLowerCase(),
                    id: el.id || '', required: el.required,
                    autocomplete: el.autocomplete || '', placeholder: el.placeholder || ''
                }))
            }));
            var scripts = Array.from(document.querySelectorAll('script[src]')).map(s => s.src);
            var iframes = Array.from(document.querySelectorAll('iframe[src]')).map(f => f.src);
            // HTML comments
            var comments = [];
            var tw = document.createTreeWalker(document, NodeFilter.SHOW_COMMENT);
            while (tw.nextNode()) {
                var t = tw.currentNode.textContent.trim();
                if (t.length > 3 && t.length < 500) comments.push(t);
            }
            // Emails
            var bodyText = document.body ? document.body.innerText : '';
            var emails = [...new Set((bodyText.match(/[a-zA-Z0-9._%+\\-]+@[a-zA-Z0-9.\\-]+\\.[a-zA-Z]{2,}/g) || []))];
            // API endpoints from inline scripts
            var apiEndpoints = [];
            var inlineScripts = Array.from(document.querySelectorAll('script:not([src])')).map(s => s.textContent);
            var apiRe = /["']((?:\\/api\\/|\\/v[0-9]+\\/|\\/graphql|\\/rest\\/)[^"'\\s]{2,})["']/g;
            inlineScripts.forEach(s => { var m; while ((m = apiRe.exec(s)) !== null) apiEndpoints.push(m[1]); });
            return {links: allLinks, forms: forms, scripts: scripts, iframes: iframes,
                    comments: comments.slice(0, 50), emails: emails.slice(0, 30),
                    apiEndpoints: [...new Set(apiEndpoints)].slice(0, 50)};
        })()
    """) or {}

    for link in data.get("links", []):
        href = link.get("href", "")
        try:
            ld = urlparse(href).netloc
            if ld and ld != base_domain:
                result.external_links.append(link)
            else:
                result.links.append(link)
        except Exception:  # noqa: BLE001
            result.links.append(link)
    result.forms = data.get("forms", [])
    result.scripts = data.get("scripts", [])
    result.iframes = data.get("iframes", [])
    result.comments = data.get("comments", [])
    result.emails = data.get("emails", [])
    result.api_endpoints = data.get("apiEndpoints", [])
    return result


def print_spider(sp: SpiderResult) -> None:
    console.print(f"\n[bold]Spider results for {sp.base_url}\n")

    if sp.links:
        t = Table(title=f"Internal Links ({len(sp.links)})")
        t.add_column("URL", max_width=80)
        t.add_column("Text", max_width=40)
        for link in sp.links[:50]:
            t.add_row(link.get("href", ""), link.get("text", ""))
        console.print(t)

    if sp.external_links:
        t = Table(title=f"[yellow]External Links ({len(sp.external_links)})")
        t.add_column("URL", max_width=80)
        t.add_column("Text", max_width=30)
        for link in sp.external_links[:30]:
            t.add_row(link.get("href", ""), link.get("text", ""))
        console.print(t)

    if sp.forms:
        t = Table(title=f"Forms ({len(sp.forms)})")
        t.add_column("Action", max_width=50)
        t.add_column("Method")
        t.add_column("Upload?")
        t.add_column("Fields")
        for form in sp.forms:
            fields_str = ", ".join(
                f"{f.get('name', '?')}({f.get('type', '?')})"
                for f in form.get("fields", []) if f.get("name")
            )
            t.add_row(form.get("action", ""), form.get("method", ""),
                      "Yes" if form.get("hasFileUpload") else "", fields_str[:60])
        console.print(t)

    if sp.api_endpoints:
        t = Table(title=f"[cyan]API Endpoints ({len(sp.api_endpoints)})")
        t.add_column("Endpoint")
        for ep in sp.api_endpoints:
            t.add_row(ep)
        console.print(t)

    if sp.emails:
        console.print(f"\n[bold]Emails found: [cyan]{', '.join(sp.emails)}")

    if sp.comments:
        t = Table(title=f"HTML Comments ({len(sp.comments)})")
        t.add_column("Comment", max_width=80)
        for c in sp.comments[:20]:
            t.add_row(c[:80])
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

    console.print(f"\n[dim]Summary: {len(sp.links)} internal, {len(sp.external_links)} external, "
                  f"{len(sp.forms)} forms, {len(sp.scripts)} scripts, {len(sp.iframes)} iframes, "
                  f"{len(sp.api_endpoints)} API endpoints, {len(sp.emails)} emails, {len(sp.comments)} comments")


# ======================================================================
# Storage extraction
# ======================================================================

def extract_storage(browser: Browser) -> dict[str, Any]:
    """Dump cookies, localStorage, sessionStorage, IndexedDB names, ServiceWorkers, CacheStorage."""
    data: dict[str, Any] = {}
    data["cookies"] = browser.get_cookies()
    data["localStorage"] = browser.evaluate_js("""
        (() => { var d = {}; for (var i = 0; i < localStorage.length; i++) { var k = localStorage.key(i); d[k] = localStorage.getItem(k); } return d; })()
    """) or {}
    data["sessionStorage"] = browser.evaluate_js("""
        (() => { var d = {}; for (var i = 0; i < sessionStorage.length; i++) { var k = sessionStorage.key(i); d[k] = sessionStorage.getItem(k); } return d; })()
    """) or {}
    data["indexedDB"] = browser.evaluate_js("""
        (async () => { try { var dbs = await indexedDB.databases(); return dbs.map(db => ({name: db.name, version: db.version})); } catch(e) { return []; } })()
    """) or []
    data["serviceWorkers"] = browser.evaluate_js("""
        (async () => { try { var r = await navigator.serviceWorker.getRegistrations(); return r.map(x => ({scope: x.scope, active: !!x.active})); } catch(e) { return []; } })()
    """) or []
    data["cacheStorage"] = browser.evaluate_js("""
        (async () => { try { return await caches.keys(); } catch(e) { return []; } })()
    """) or []
    return data


def print_storage(data: dict[str, Any]) -> None:
    cookies = data.get("cookies", [])
    if cookies:
        t = Table(title=f"Cookies ({len(cookies)})")
        t.add_column("Name")
        t.add_column("Value", max_width=40)
        t.add_column("Domain")
        t.add_column("Secure")
        t.add_column("HttpOnly")
        t.add_column("SameSite")
        for c in cookies:
            t.add_row(c.get("name", ""), str(c.get("value", ""))[:40],
                      c.get("domain", ""), str(c.get("secure", False)),
                      str(c.get("httpOnly", False)), c.get("sameSite", ""))
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
    idb = data.get("indexedDB", [])
    if idb:
        t = Table(title=f"IndexedDB ({len(idb)})")
        t.add_column("Name")
        t.add_column("Version")
        for db in idb:
            t.add_row(str(db.get("name", "")), str(db.get("version", "")))
        console.print(t)
    sw = data.get("serviceWorkers", [])
    if sw:
        console.print(f"[bold]Service Workers: {len(sw)}")
    cache = data.get("cacheStorage", [])
    if cache:
        console.print(f"[bold]Cache Storage: [cyan]{', '.join(cache)}")


# ======================================================================
# CSP analysis
# ======================================================================

def analyze_csp(browser: Browser) -> dict[str, Any]:
    """Extract and parse Content-Security-Policy from the current page.

    CSP in the real world is almost always delivered via the response
    ``Content-Security-Policy`` header; only a minority of sites use the
    ``<meta http-equiv>`` form.  We inspect both, and if the header has
    a ``Content-Security-Policy-Report-Only`` counterpart we surface it
    as a separate entry instead of silently ignoring it.
    """
    headers = _capture_main_frame_headers(browser)
    csp_header = ""
    csp_report_only = ""
    for k, v in headers.items():
        if k.lower() == "content-security-policy":
            csp_header = v
        elif k.lower() == "content-security-policy-report-only":
            csp_report_only = v

    csp_meta = browser.evaluate_js(
        "(() => { var m = document.querySelector('meta[http-equiv=\"Content-Security-Policy\"]'); "
        "return m ? m.content : ''; })()"
    ) or ""

    result: dict[str, Any] = {
        "csp_header": csp_header,
        "csp_report_only": csp_report_only,
        "csp_meta": csp_meta,
        "directives": {},
        "issues": [],
    }

    # Prefer the enforcing header; fall back to meta; then report-only.
    csp = csp_header or csp_meta or csp_report_only
    if not csp:
        result["issues"].append("No CSP found (neither header nor meta tag detected)")
        return result
    if not csp_header and csp_meta:
        result["issues"].append(
            "CSP delivered only via <meta http-equiv> — header form is stronger "
            "(ignored by some UAs for e.g. frame-ancestors, report-uri)"
        )
    if not csp_header and csp_report_only:
        result["issues"].append(
            "CSP is report-only — violations are reported but NOT blocked"
        )

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
    if "form-action" not in result["directives"]:
        result["issues"].append("No form-action directive — forms can submit anywhere")
    if "frame-ancestors" not in result["directives"]:
        result["issues"].append("No frame-ancestors directive — clickjacking risk")
    if "base-uri" not in result["directives"]:
        result["issues"].append("No base-uri directive — base tag injection risk")

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
    """Run fingerprint + spider + security headers + cookies + storage + CSP."""
    console.print("[bold]Running full reconnaissance …\n")

    fp = fingerprint_page(browser)
    print_fingerprint(fp)

    sp = spider_page(browser)
    print_spider(sp)

    sh = analyze_security_headers(browser)
    print_security_headers(sh)

    cookie_issues = analyze_cookies(browser)
    print_cookie_issues(cookie_issues)

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
            "technologies": fp.technologies,
            "meta_tags": fp.meta_tags,
        },
        "spider": {
            "internal_links": len(sp.links),
            "external_links": len(sp.external_links),
            "forms": sp.forms,
            "scripts": sp.scripts,
            "iframes": sp.iframes,
            "api_endpoints": sp.api_endpoints,
            "emails": sp.emails,
            "comments_count": len(sp.comments),
        },
        "security_headers": {
            "present": sh.present,
            "missing": sh.missing,
            "issues": sh.issues,
        },
        "cookie_issues": cookie_issues,
        "storage": {
            "cookies": len(storage.get("cookies", [])),
            "localStorage_keys": len(storage.get("localStorage", {})),
            "sessionStorage_keys": len(storage.get("sessionStorage", {})),
            "indexedDB": storage.get("indexedDB", []),
            "serviceWorkers": storage.get("serviceWorkers", []),
            "cacheStorage": storage.get("cacheStorage", []),
        },
        "csp": csp,
    }

    if output:
        with open(output, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        console.print(f"\n[green]Recon report saved to {output}")

    return report
