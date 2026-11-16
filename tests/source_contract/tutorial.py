"""Runnable example for the source adapter guide."""

from collections.abc import Iterator
from pathlib import Path

from lbr_dashboard.core import (
    CancellationToken,
    IteratorSource,
    SignalDescriptor,
    SignalSample,
    SignalSchema,
    SourceEvent,
    SourceEventKind,
)

SOURCE = "tutorial.bench"
SIGNAL = "bench.pressure"
DOMAIN = "device_monotonic"
UNIT = "Pa"
MAX_LINE = 64


def bench_source(path: Path) -> IteratorSource:
    """Read a local log of integer microseconds and integer pascals or a blank."""
    descriptor = SignalDescriptor(
        SIGNAL,
        "Bench pressure",
        ("bench",),
        "numeric",
        SOURCE,
        canonical_unit=UNIT,
        timestamp_domain=DOMAIN,
    )

    def records(cancel: CancellationToken) -> Iterator[SourceEvent]:
        cancel.raise_if_cancelled()
        with path.open("rb") as stream:
            for number, raw in enumerate(iter(lambda: stream.readline(MAX_LINE + 1), b""), 1):
                cancel.raise_if_cancelled()
                try:
                    if len(raw) > MAX_LINE:
                        raise ValueError("record is too long")
                    timestamp, pressure = raw.decode("ascii").strip().split(",")
                    time_us = int(timestamp)
                    if not 0 <= time_us <= (2**63 - 1) // 1_000:
                        raise ValueError("timestamp is outside the supported range")
                    value = int(pressure) if pressure else None
                except ValueError as error:
                    raise ValueError(f"Bench log line {number}: {error}") from error
                yield SourceEvent(
                    SourceEventKind.RECORD,
                    SOURCE,
                    signal_id=SIGNAL,
                    sample=SignalSample(
                        time_us * 1_000,
                        value,
                        available=value is not None,
                        source_id=SOURCE,
                        unit=UNIT,
                        timestamp_domain=DOMAIN,
                    ),
                )

    return IteratorSource(SOURCE, SignalSchema(1, (descriptor,)), records, capacity=4)


def main() -> int:
    from argparse import ArgumentParser

    from lbr_dashboard.app import (
        create_application,
        create_main_window,
        install_garbage_collector,
    )
    from lbr_dashboard.source_controller import SourceController, SourceOption

    parser = ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="explicit bench log file")
    args = parser.parse_args()

    app = create_application([])
    install_garbage_collector(app)
    window = create_main_window()
    owner = window.source_owner
    assert isinstance(owner, SourceController)
    owner.register(
        SourceOption("bench_log", "Example bench log", lambda _: lambda: bench_source(args.path))
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
