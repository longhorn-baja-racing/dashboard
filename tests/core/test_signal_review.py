import json

import numpy as np
import pytest

from lbr_dashboard.core.lifecycle import (
    BackgroundService,
    CancellationRequested,
    ServiceEvent,
    ServiceFailure,
)
from lbr_dashboard.core.signals import SignalDescriptor, SignalSample, SignalSchema, SignalSeries


def test_worker_passes_typed_series_and_snapshots_bytearray():
    sample = SignalSample(1, 3.0)
    series = SignalSeries("lab.x", (sample,))
    descriptor = SignalDescriptor("lab.x", "X", ("lab",), "numeric", "adapter")
    raw = bytearray(b"ab")

    def work(token, emit):
        emit(
            ServiceEvent(
                "samples",
                {
                    "series": series,
                    "sample": sample,
                    "descriptor": descriptor,
                    "failure": ServiceFailure("ValueError", "x"),
                    "raw": raw,
                },
            )
        )
        raw.clear()

    worker = BackgroundService(work)
    worker.start()
    assert worker.join(2)
    assert worker.failure is None
    (event,) = worker.drain_events()
    assert event.payload["series"] is series
    assert event.payload["sample"] is sample
    assert event.payload["descriptor"] is descriptor
    assert event.payload["raw"] == b"ab"
    assert event.payload["failure"] == ServiceFailure("ValueError", "x")


def test_opaque_source_and_device_ids_and_numpy_timestamp():
    descriptor = SignalDescriptor(
        "lab.x", "X", ("lab",), "numeric", "COM3", device_id="AA:00:A10KZ3"
    )
    sample = SignalSample(np.int64(2**53 + 1), 5, source_id="COM3")  # pyright: ignore[reportArgumentType]
    descriptor.validate_sample(sample)
    assert descriptor.device_id == "AA:00:A10KZ3"
    assert type(sample.timestamp_ns) is int
    assert sample.timestamp_ns == 2**53 + 1
    assert SignalSample.from_dict(sample.to_dict()).source_id == "COM3"


def test_schema_dictionary_is_plain_json_and_models_explicitly_unhashable():
    descriptor = SignalDescriptor("lab.x", "X", ("lab",), "numeric", "adapter")
    schema = SignalSchema(1, (descriptor,))
    assert schema.to_dict() == json.loads(schema.to_json())
    for model in (descriptor, schema, SignalSample(1, 0)):
        assert type(model).__hash__ is None


@pytest.mark.parametrize("error", [SystemExit(1), CancellationRequested("unsolicited")])
def test_unexpected_worker_exit_is_observable(error):
    def work(token, emit):
        raise error

    worker = BackgroundService(work)
    worker.start()
    assert worker.join(2)
    assert worker.failure == ServiceFailure(type(error).__name__, str(error))
