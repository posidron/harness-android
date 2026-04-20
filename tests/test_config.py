import json

from harness_android.config import _deep_merge, load_config, _DEFAULT_CONFIG


def test_deep_merge_nested():
    base = {"a": 1, "b": {"x": 1, "y": 2}}
    over = {"b": {"y": 9, "z": 3}, "c": 4}
    out = _deep_merge(base, over)
    assert out == {"a": 1, "b": {"x": 1, "y": 9, "z": 3}, "c": 4}
    # base must not be mutated
    assert base == {"a": 1, "b": {"x": 1, "y": 2}}


def test_deep_merge_replaces_non_dict():
    out = _deep_merge({"a": {"x": 1}}, {"a": [1, 2]})
    assert out == {"a": [1, 2]}


def test_load_config_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("ANDROID_HARNESS_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"emulator": {"ram": 8192}}))
    monkeypatch.chdir(tmp_path)  # ensure no stray harness.json from cwd
    cfg = load_config()
    assert cfg["emulator"]["ram"] == 8192
    assert cfg["emulator"]["api_level"] == _DEFAULT_CONFIG["emulator"]["api_level"]


def test_load_config_invalid_json_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANDROID_HARNESS_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text("{not json")
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    # Falls back to defaults
    assert cfg["emulator"]["ram"] == _DEFAULT_CONFIG["emulator"]["ram"]
    err = capsys.readouterr().err
    assert "warning" in err
