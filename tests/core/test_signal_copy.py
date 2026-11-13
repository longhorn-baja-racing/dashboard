import copy
import pickle
from dataclasses import asdict
from typing import Any, cast

import numpy as np
import pytest

from lbr_dashboard.core.signals import (
    SignalAvailability,
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    SignalSeries,
)


def test_immutable_signal_models_copy_and_pickle():
    availability = SignalAvailability(extra={"detail": {"count": 3}})
    descriptor = SignalDescriptor(
        "test.value",
        "Value",
        ("test",),
        "vector",
        "Test",
        shape=cast(Any, (np.int64(2),)),
        availability=availability,
    )
    sample = SignalSample(1, (np.int64(3), np.float32(2.5)), extra={"nested": {"valid": True}})
    series = SignalSeries(descriptor.identifier, (sample,))
    schema = SignalSchema(1, (descriptor,))
    for value in (availability, descriptor, sample, series, schema):
        assert copy.deepcopy(value) == value
        assert pickle.loads(pickle.dumps(value)) == value
        assert asdict(value)
    assert sample.value == (3, 2.5)
    assert type(sample.value[0]) is int
    assert type(sample.value[1]) is float
    with pytest.raises(TypeError):
        cast(Any, sample.extra["nested"])["valid"] = False
