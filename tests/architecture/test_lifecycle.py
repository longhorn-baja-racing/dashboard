"""Cancellation, immutable handoff, cleanup, and owner-visible errors."""

from threading import Event

import pytest

from lbr_dashboard.core import BackgroundService, CancellationToken, ServiceEvent, ServiceFailure


def test_background_service_cancels_and_cleans_up() -> None:
    started = Event()
    cleaned_up = Event()

    def work(token: CancellationToken, _emit) -> None:
        started.set()
        try:
            while True:
                token.raise_if_cancelled()
        finally:
            cleaned_up.set()

    service = BackgroundService(work)
    service.start()
    assert started.wait(timeout=1)
    service.cancel()

    assert service.join(timeout=1)
    assert cleaned_up.is_set()
    assert service.is_finished
    assert service.failure is None


def test_event_handoff_is_copied_and_immutable() -> None:
    original = {"sample": {"values": [1, 2]}}
    event = ServiceEvent("sample", original)
    original["sample"]["values"].append(3)

    assert event.payload["sample"]["values"] == (1, 2)
    with pytest.raises(TypeError):
        event.payload["new"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        event.payload["sample"]["values"][0] = 4  # type: ignore[index]


def test_worker_events_are_drained_by_owner() -> None:
    def work(_token: CancellationToken, emit) -> None:
        emit(ServiceEvent("ready", {"count": 1}))

    service = BackgroundService(work)
    service.start()
    assert service.join(timeout=1)

    assert service.drain_events() == (ServiceEvent("ready", {"count": 1}),)
    assert service.drain_events() == ()


def test_worker_failure_is_available_to_owner() -> None:
    def work(_token: CancellationToken, _emit) -> None:
        raise ValueError("bad telemetry frame")

    service = BackgroundService(work)
    service.start()

    assert service.join(timeout=1)
    assert service.failure == ServiceFailure("ValueError", "bad telemetry frame")
