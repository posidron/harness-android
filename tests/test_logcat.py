from harness_android.logcat import LogcatCapture


ASAN_FIXTURE = """\
04-16 12:00:01.123  1234  1234 I asan    : ==1234==ERROR: AddressSanitizer: heap-buffer-overflow on address 0xdeadbeef
04-16 12:00:01.124  1234  1234 I asan    :     #0 0x123 in foo()
04-16 12:00:01.125  1234  1234 I asan    : SUMMARY: AddressSanitizer: heap-buffer-overflow in foo()
"""

NATIVE_FIXTURE = """\
04-16 12:01:00.000  4321  4321 F libc    : Fatal signal 11 (SIGSEGV), code 1, fault addr 0x0
04-16 12:01:00.050  4321  4321 F DEBUG   : pid: 4321, tid: 4321, name: chrome >>> com.android.chrome <<<
04-16 12:01:00.060  4321  4321 F DEBUG   : signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x0
04-16 12:01:00.070  4321  4321 I DEBUG   : Tombstone written to: /data/tombstones/tombstone_01
"""

JAVA_FIXTURE = """\
04-16 12:02:00.000  5555  5555 E AndroidRuntime: FATAL EXCEPTION: main
04-16 12:02:00.001  5555  5555 E AndroidRuntime: java.lang.NullPointerException: Attempt to invoke virtual method
04-16 12:02:00.002  5555  5555 E AndroidRuntime:     at com.example.Foo.bar(Foo.java:42)
"""

ANR_FIXTURE = """\
04-16 12:03:00.000  6666  6666 E ActivityManager: ANR in com.example.app (com.example.app/.MainActivity)
"""

UBSAN_FIXTURE = """\
04-16 12:04:00.000  7777  7777 E ubsan   : ../../foo.cc:10:5: runtime error: signed integer overflow: 2147483647 + 1
"""

CLEAN_FIXTURE = """\
04-16 12:05:00.000  1111  1111 I chromium: nothing to see here
04-16 12:05:00.001  1111  1111 D Zygote  : Forked child process
"""


def _scan(tmp_path, text):
    p = tmp_path / "log.txt"
    p.write_text(text)
    return LogcatCapture.find_crashes(p)


def test_asan_detected(tmp_path):
    events = _scan(tmp_path, ASAN_FIXTURE)
    types = {e.event_type for e in events}
    assert "asan" in types
    assert all(e.severity == "critical" for e in events if e.event_type == "asan")
    e = next(e for e in events if e.event_type == "asan")
    assert "heap-buffer-overflow" in e.message
    assert e.pid == "1234"
    assert e.timestamp.startswith("04-16")
    assert "#0 0x123" in e.stacktrace


def test_native_crash_detected(tmp_path):
    events = _scan(tmp_path, NATIVE_FIXTURE)
    types = [e.event_type for e in events]
    assert "abort" in types or "native_crash" in types
    # tombstone path captured
    assert any("tombstone" in e.message.lower() for e in events)


def test_java_fatal_exception(tmp_path):
    events = _scan(tmp_path, JAVA_FIXTURE)
    assert any(e.event_type == "java_exception" for e in events)
    e = next(e for e in events if e.event_type == "java_exception")
    assert e.severity == "critical"
    assert "main" in e.message


def test_anr_detected(tmp_path):
    events = _scan(tmp_path, ANR_FIXTURE)
    assert len(events) == 1
    assert events[0].event_type == "anr"
    assert events[0].severity == "high"
    assert "com.example.app" in events[0].message


def test_ubsan_detected(tmp_path):
    events = _scan(tmp_path, UBSAN_FIXTURE)
    assert any(e.event_type == "ubsan" for e in events)
    assert "signed integer overflow" in events[0].message


def test_clean_log_no_findings(tmp_path):
    assert _scan(tmp_path, CLEAN_FIXTURE) == []


def test_dedup_repeated_lines(tmp_path):
    text = ASAN_FIXTURE.splitlines()[0] + "\n"
    once = _scan(tmp_path, text)
    many = _scan(tmp_path, text * 5)
    # Repeating the same line N times must not multiply events.
    assert len(many) == len(once)
    assert len(many) >= 1


def test_missing_file_returns_empty(tmp_path):
    assert LogcatCapture.find_crashes(tmp_path / "nope.txt") == []
