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
MAX_RECORDS = 20_000


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
            # Allow CRLF and one extra byte so a long record cannot masquerade as a full row.
            lines = iter(lambda: stream.readline(MAX_LINE + 3), b"")
            number = 0
            for number, raw in enumerate(lines, 1):
                cancel.raise_if_cancelled()
                try:
                    if number > MAX_RECORDS:
                        raise ValueError(f"log exceeds {MAX_RECORDS:,} readings")
                    if raw.endswith(b"\n"):
                        raw = raw[:-1].removesuffix(b"\r")
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
            if number == 0:
                raise ValueError("Bench log has no readings")

    return IteratorSource(SOURCE, SignalSchema(1, (descriptor,)), records, capacity=4)


def create_demo_window(path: Path):
    """Register the demo at the application boundary."""
    from lbr_dashboard.app import create_main_window
    from lbr_dashboard.source_controller import SourceController, SourceOption

    window = create_main_window()
    owner = window.source_owner
    assert isinstance(owner, SourceController)

    def prepare(token: CancellationToken):
        token.raise_if_cancelled()
        return lambda: bench_source(path)

    owner.register(SourceOption("bench_log", "Example bench log", prepare))
    return window


def main() -> int:
    from argparse import ArgumentParser

    from lbr_dashboard.app import create_application, install_garbage_collector

    parser = ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="explicit bench log file")
    args = parser.parse_args()

    app = create_application([])
    install_garbage_collector(app)
    window = create_demo_window(args.path)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
