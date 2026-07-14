from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import automation_business_scaffold.infrastructure.browser.browser_bridge as browser_bridge


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_example_chrome_profile_declares_cdp_autostart_metadata() -> None:
    profiles = json.loads((REPO_ROOT / "config" / "browser_profiles.example.json").read_text(encoding="utf-8"))
    metadata = profiles["local-chrome"]["metadata"]

    assert metadata["debug_http"] == "http://127.0.0.1:9222"
    assert metadata["auto_start"] is True
    assert metadata["user_data_dir"] == "~/.mujitask/chrome-cdp/local-chrome"
    assert "--disable-blink-features=AutomationControlled" in metadata["launch_args"]


def test_chrome_cdp_autostart_launches_chrome_with_required_args(monkeypatch, tmp_path: Path) -> None:
    profile_dir = tmp_path / "chrome-profile"
    target = SimpleNamespace(
        provider="chrome_cdp",
        profile_ref="chrome-gcp",
        metadata={
            "auto_start": True,
            "debug_http": "http://127.0.0.1:9333",
            "user_data_dir": str(profile_dir),
        },
    )
    ready_results = iter([False, True])
    captured: dict[str, object] = {}

    monkeypatch.setattr(browser_bridge, "_chrome_cdp_ready", lambda _debug_http: next(ready_results))
    monkeypatch.setattr(browser_bridge, "_detect_chrome_bin", lambda _metadata: "/usr/bin/google-chrome")
    monkeypatch.setattr(browser_bridge.time, "sleep", lambda _seconds: None)

    def fake_popen(command, **kwargs):  # noqa: ANN001, ANN003
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(browser_bridge.subprocess, "Popen", fake_popen)

    browser_bridge._ensure_chrome_cdp_started(target)

    command = captured["command"]
    assert command == [
        "/usr/bin/google-chrome",
        "--remote-debugging-port=9333",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir}",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    assert captured["kwargs"]["start_new_session"] is True
    assert profile_dir.is_dir()


def test_chrome_cdp_autostart_skips_launch_when_cdp_is_ready(monkeypatch) -> None:
    target = SimpleNamespace(
        provider="chrome_cdp",
        profile_ref="chrome-gcp",
        metadata={"auto_start": True, "debug_http": "http://127.0.0.1:9222"},
    )

    monkeypatch.setattr(browser_bridge, "_chrome_cdp_ready", lambda _debug_http: True)
    monkeypatch.setattr(
        browser_bridge.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not launch Chrome")),
    )

    browser_bridge._ensure_chrome_cdp_started(target)
