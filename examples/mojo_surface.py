"""Example: Mojo IPC surface exploration.

Traces Mojo IPC while exercising Web APIs, then analyzes the results.
Run with: android-harness pentest run examples/mojo_surface.py
"""


def run(ctx):
    from android_harness.mojo import MojoTracer

    ctx.navigate("https://example.com")
    ctx.wait(2)

    tracer = MojoTracer(ctx.browser, verbose=True)

    # --- Phase 1: Trigger all Mojo-backed Web APIs ---
    print("\n=== Phase 1: Trigger Mojo Web APIs ===")
    tracer.start_trace()
    results = tracer.trigger_all_apis()
    events = tracer.stop_trace()

    messages = tracer.extract_mojo_messages(events)
    tracer.print_trigger_results(results)
    tracer.print_summary(messages)

    # Record findings for APIs that succeeded (attack surface)
    for r in results:
        if not r.error and r.result not in ("unavailable", None):
            ctx.add_finding(
                title=f"Mojo interface reachable: {r.mojo_interface}",
                severity="info",
                description=f"Web API {r.api_name} is accessible and exercises {r.mojo_interface}",
                evidence=str(r.result),
            )

    # --- Phase 2: Fuzz the Clipboard API ---
    print("\n=== Phase 2: Fuzz Clipboard.writeText ===")
    tracer.start_trace()
    fuzz_results = tracer.fuzz_api(
        "Clipboard.writeText",
        "navigator.clipboard.writeText({FUZZ}).catch(e => e.message)",
        MojoTracer.FUZZ_STRINGS,
        "blink.mojom.ClipboardHost",
    )
    fuzz_events = tracer.stop_trace()
    fuzz_messages = tracer.extract_mojo_messages(fuzz_events)
    tracer.print_summary(fuzz_messages)

    # Check for unexpected results
    for r in fuzz_results:
        if r.error:
            ctx.add_finding(
                title=f"Fuzz crash/error: {r.api_name}",
                severity="high",
                description=f"Input caused an error: {r.error}",
            )

    # --- Phase 3: Check CSP (Mojo-adjacent security) ---
    print("\n=== Phase 3: CSP analysis ===")
    csp = ctx.csp()
    for issue in csp.get("issues", []):
        ctx.add_finding(title=issue, severity="medium")

    # --- Save report ---
    tracer.dump("mojo_analysis.json", events, messages, results)
    ctx.report(path="mojo_pentest_report.json")
