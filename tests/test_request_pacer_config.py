from __future__ import annotations

from automation_business_scaffold.infrastructure.rate_limit import (
    RequestPacer,
    RequestPacerConfig,
    resolve_api_request_delay_range,
    resolve_api_request_pacer_config,
)


def test_api_request_pacer_config_defaults_to_half_to_one_second(monkeypatch) -> None:
    monkeypatch.delenv("MUJITASK_API_REQUEST_MIN_DELAY_SECONDS", raising=False)
    monkeypatch.delenv("MUJITASK_API_REQUEST_MAX_DELAY_SECONDS", raising=False)
    monkeypatch.delenv("MUJITASK_FASTMOSS_API_REQUEST_MIN_DELAY_SECONDS", raising=False)
    monkeypatch.delenv("MUJITASK_FASTMOSS_API_REQUEST_MAX_DELAY_SECONDS", raising=False)

    assert resolve_api_request_delay_range(provider="fastmoss") == (0.5, 1.0)
    assert resolve_api_request_pacer_config(provider="fastmoss") == RequestPacerConfig(
        min_delay_seconds=0.5,
        max_delay_seconds=1.0,
    )


def test_api_request_pacer_config_uses_provider_env_before_global_env(monkeypatch) -> None:
    monkeypatch.setenv("MUJITASK_API_REQUEST_MIN_DELAY_SECONDS", "0.1")
    monkeypatch.setenv("MUJITASK_API_REQUEST_MAX_DELAY_SECONDS", "0.2")
    monkeypatch.setenv("MUJITASK_FEISHU_API_REQUEST_MIN_DELAY_SECONDS", "0.7")
    monkeypatch.setenv("MUJITASK_FEISHU_API_REQUEST_MAX_DELAY_SECONDS", "1.3")

    assert resolve_api_request_delay_range(provider="feishu") == (0.7, 1.3)
    assert resolve_api_request_delay_range(provider="fastmoss") == (0.1, 0.2)


def test_api_request_pacer_config_uses_payload_before_env_and_normalizes(monkeypatch) -> None:
    monkeypatch.setenv("MUJITASK_TIKTOK_API_REQUEST_MIN_DELAY_SECONDS", "0.7")
    monkeypatch.setenv("MUJITASK_TIKTOK_API_REQUEST_MAX_DELAY_SECONDS", "1.3")

    assert resolve_api_request_delay_range(
        {
            "tiktok_api_request_delay_min_seconds": "2.0",
            "tiktok_api_request_delay_max_seconds": "1.0",
        },
        provider="tiktok",
    ) == (1.0, 2.0)


def test_request_pacer_sleeps_between_requests_and_emits_evidence() -> None:
    sleeps: list[float] = []
    events: list[dict[str, object]] = []
    pacer = RequestPacer(
        RequestPacerConfig(min_delay_seconds=0.5, max_delay_seconds=1.0),
        sleep_factory=sleeps.append,
        random_uniform=lambda _min, _max: 0.75,
        event_callback=events.append,
    )

    assert pacer.wait_before_request("fastmoss:search") == 0.0
    pacer.mark_request_finished("fastmoss:search")
    assert pacer.wait_before_request("fastmoss:search") == 0.75
    pacer.mark_request_finished("fastmoss:search")

    assert sleeps == [0.75]
    assert any(event["kind"] == "request_pacer_sleep" for event in events)
    assert any(event["kind"] == "request_pacer_ready" for event in events)
    assert any(event["kind"] == "request_pacer_finished" for event in events)
