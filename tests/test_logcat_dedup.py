"""Regression tests for logcat crash detection."""

from __future__ import annotations

from harness_android.logcat import LogcatCapture


_THREADTIME = "04-23 12:{min:02d}:00.000  {pid:>5d}  {pid:>5d} E {tag}: {msg}"


def test_anrs_in_different_pids_are_not_collapsed(tmp_path):
    log = tmp_path / "logcat.txt"
    log.write_text(
        _THREADTIME.format(min=1, pid=101, tag="ActivityManager",
                           msg="ANR in com.example.app") + "\n" +
        _THREADTIME.format(min=2, pid=202, tag="ActivityManager",
                           msg="ANR in com.example.app") + "\n" +
        _THREADTIME.format(min=3, pid=303, tag="ActivityManager",
                           msg="ANR in com.example.app") + "\n",
        encoding="utf-8",
    )
    events = LogcatCapture.find_crashes(log)
    anrs = [e for e in events if e.event_type == "anr"]
    assert len(anrs) == 3, f"expected 3 ANR events from distinct pids, got {len(anrs)}"
    pids = {e.pid for e in anrs}
    assert pids == {"101", "202", "303"}


def test_same_anr_same_pid_same_time_is_deduped(tmp_path):
    log = tmp_path / "logcat.txt"
    same = _THREADTIME.format(min=5, pid=500, tag="ActivityManager",
                              msg="ANR in com.example.app")
    log.write_text(same + "\n" + same + "\n", encoding="utf-8")
    events = LogcatCapture.find_crashes(log)
    anrs = [e for e in events if e.event_type == "anr"]
    assert len(anrs) == 1
