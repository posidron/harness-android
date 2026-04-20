"""Example: Mojo IPC reconnaissance plugin.

Enumerates reachable Mojo interfaces by triggering Web APIs and passively
tracing IPC messages.  This is useful for **mapping the attack surface** —
discovering which mojom interfaces are reachable from the renderer — but
does NOT directly fuzz the IPC channel.

For raw Mojo IPC fuzzing driven from the harness (MojoJS bindings), use::

    harness-android mojo enable --fuzz blink.mojom.ClipboardHost

or see ``mojo_bindings_test.html`` for an in-page variant.

Run with: harness-android pentest run examples/mojo_recon.py
"""


def run(ctx):
    from harness_android.mojo import MojoTracer

    ctx.navigate("https://example.com")

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
        if r.crashed:
            ctx.add_finding(
                title=f"Renderer crash via {r.api_name}",
                severity="critical",
                description=f"Triggering {r.api_name} crashed the renderer (Mojo interface {r.mojo_interface})",
                evidence=r.error,
            )
        elif not r.error and r.result not in ("unavailable", None):
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
        if r.crashed:
            ctx.add_finding(
                title=f"Fuzz crash: {r.api_name}",
                severity="critical",
                description=f"Renderer crashed on fuzzed input",
                evidence=r.error,
            )
        elif r.error:
            ctx.add_finding(
                title=f"Fuzz error: {r.api_name}",
                severity="high",
                description=f"Input caused an error: {r.error}",
            )

    # --- Save report ---
    tracer.dump("mojo_analysis.json", events, messages, results)
    ctx.report(path="mojo_pentest_report.json")
