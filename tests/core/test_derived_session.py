"""Derived signals evaluated into session snapshots, with caching and persistence."""

import pytest

from lbr_dashboard.core import SignalDescriptor, SignalSample, SignalSchema, TelemetrySession
from lbr_dashboard.core.derived import CycleError, DefinitionError, DerivedSignalDefinition
from lbr_dashboard.core.derived_session import DerivedSignalSet, is_derived

MS = 1_000_000


def descriptor(identifier, unit=None, kind="numeric", source="bench", domain="relative"):
    return SignalDescriptor(
        identifier,
        identifier.rsplit(".", 1)[-1],
        ("bench",),
        kind,
        source,
        canonical_unit=unit,
        timestamp_domain=domain,
    )


def make_session(*descriptors):
    return TelemetrySession(SignalSchema(1, descriptors))


def add(session, signal_id, rows):
    domain = next(d for d in session.schema.signals if d.identifier == signal_id).timestamp_domain
    session.extend(
        signal_id,
        (SignalSample(t, v, available=v is not None, timestamp_domain=domain) for t, v in rows),
    )


def define(output_id, expression, **fields):
    return DerivedSignalDefinition(
        output_id=output_id, name=output_id, expression=expression, **fields
    )


@pytest.fixture
def bench():
    session = make_session(
        descriptor("bench.force", "N"),
        descriptor("bench.arm", "m"),
        descriptor("bench.label", kind="string"),
    )
    add(session, "bench.force", [(0, 2.0), (10 * MS, None), (20 * MS, 4.0), (30 * MS, 5.0)])
    add(session, "bench.arm", [(0, 0.5), (25 * MS, 1.0)])
    add(session, "bench.label", [(0, "a")])
    return session


def test_augment_adds_a_derived_signal_without_touching_raw_data(bench):
    derived = DerivedSignalSet()
    derived.apply(
        define("bench.torque", 'sig("bench.force") * sig("bench.arm")', max_age_ns=10 * MS)
    )
    raw = bench.snapshot()
    view = derived.augment(bench, raw)

    assert view is not raw and view.revision == raw.revision
    assert view.series["bench.force"] is raw.series["bench.force"]
    assert "bench.torque" not in raw.series and "bench.torque" not in raw.schema.signals
    torque = next(d for d in view.schema.signals if d.identifier == "bench.torque")
    assert is_derived(torque) and torque.path == ("derived",)
    assert torque.canonical_unit == "N*m" and torque.source_id == "bench"
    assert torque.timestamp_domain == "relative"
    assert 'Derived: sig("bench.force") * sig("bench.arm")' in torque.description
    assert "held up to 10 ms" in torque.description
    samples = [record.sample for record in view.series["bench.torque"]]
    assert [s.timestamp_ns for s in samples] == [0, 10 * MS, 20 * MS, 30 * MS]
    # 20 ms: arm sample at 0 is 20 ms old, beyond the 10 ms hold.
    assert [s.value for s in samples] == [1.0, None, None, 5.0]
    assert [s.quality for s in samples] == [
        "derived",
        "missing: missing_input (bench.force)",
        "missing: stale_input (bench.arm)",
        "derived",
    ]
    assert view.domains["bench.torque"] == "relative"
    status = derived.statuses["bench.torque"]
    assert status.available and status.message == "4 samples, 2 missing, unit N*m"


def test_evaluation_repeats_only_when_inputs_change(bench):
    derived = DerivedSignalSet()
    derived.apply(define("bench.double", 'sig("bench.force") * 2'))
    derived.augment(bench, bench.snapshot())
    assert derived.evaluations == 1
    add(bench, "bench.arm", [(40 * MS, 2.0)])  # not an input
    derived.augment(bench, bench.snapshot())
    assert derived.evaluations == 1

    add(bench, "bench.force", [(40 * MS, 6.0)])
    stale = derived.augment(bench, bench.snapshot(), reuse_stale=True)
    assert derived.evaluations == 1 and len(stale.series["bench.double"]) == 4
    fresh = derived.augment(bench, bench.snapshot())
    assert derived.evaluations == 2 and fresh.series["bench.double"][-1].sample.value == 12.0

    other = make_session(*bench.schema.signals)
    add(other, "bench.force", [(0, 1.0)])
    derived.augment(other, other.snapshot())
    assert derived.evaluations == 3


def test_chained_definitions_follow_their_inputs(bench):
    derived = DerivedSignalSet()
    derived.apply(define("bench.double", 'sig("bench.force") * 2'))
    derived.apply(define("bench.plus", 'sig("bench.double") + 1'))
    view = derived.augment(bench, bench.snapshot())
    assert [r.sample.value for r in view.series["bench.plus"]] == [5.0, None, 9.0, 11.0]
    add(bench, "bench.force", [(40 * MS, 6.0)])
    view = derived.augment(bench, bench.snapshot())
    assert view.series["bench.plus"][-1].sample.value == 13.0
    assert derived.evaluations == 4
    with pytest.raises(CycleError, match="bench.double -> bench.plus -> bench.double"):
        derived.apply(define("bench.double", 'sig("bench.plus") * 2'), replacing="bench.double")
    with pytest.raises(DefinitionError, match="already exists"):
        derived.apply(define("bench.plus", 'sig("bench.force") * 3'))


def test_unresolvable_definitions_are_unavailable_not_errors(bench):
    derived = DerivedSignalSet()
    derived.restore_configuration(
        {
            "version": 1,
            "definitions": [
                define("bench.ghost", 'sig("bench.missing") * 2').to_dict(),
                define("bench.text", 'sig("bench.label") + 1').to_dict(),
                define("bench.force", 'sig("bench.arm") * 2').to_dict(),
                define("bench.after", 'sig("bench.ghost") + 1').to_dict(),
            ],
        }
    )
    view = derived.augment(bench, bench.snapshot())
    statuses = derived.statuses
    assert "unknown signal 'bench.missing'" in statuses["bench.ghost"].message
    assert "is string; derived signals need scalar numeric inputs" in statuses["bench.text"].message
    assert "already a signal of this source" in statuses["bench.force"].message
    assert "input 'bench.ghost' is unavailable" in statuses["bench.after"].message
    ghost = next(d for d in view.schema.signals if d.identifier == "bench.ghost")
    assert not ghost.availability.available and view.series["bench.ghost"] == ()
    force = [d for d in view.schema.signals if d.identifier == "bench.force"]
    assert len(force) == 1 and not is_derived(force[0])


def test_configuration_round_trip_and_rejections():
    derived = DerivedSignalSet()
    derived.apply(define("bench.double", 'sig("bench.force") * 2', unit="N", max_age_ns=5))
    config = derived.configuration()
    restored = DerivedSignalSet()
    calls = []
    restored.subscribe(lambda: calls.append(1))
    restored.restore_configuration(config)
    assert restored.definitions == derived.definitions and calls == [1]

    for bad, message in [
        ([], "needs version and definitions"),
        ({"version": 2, "definitions": []}, "unsupported"),
        ({"version": 1, "definitions": {}}, "must be a list"),
        ({"version": 1, "definitions": [{"version": 1}]}, "keys are wrong"),
        (
            {
                "version": 1,
                "definitions": [
                    define("bench.a", 'sig("bench.b") + 1').to_dict(),
                    define("bench.b", 'sig("bench.a") + 1').to_dict(),
                ],
            },
            "cycle",
        ),
        ({"version": 1, "definitions": [define("Bad", 'sig("x") + 1').to_dict()]}, "lowercase"),
    ]:
        with pytest.raises(DefinitionError, match=message):
            restored.restore_configuration(bad)
    assert restored.definitions == derived.definitions and calls == [1]


def test_units_outside_the_notation_are_unknown_and_clocks_are_never_mixed():
    session = make_session(
        descriptor("rig.temp", chr(0xB0) + "C"),
        descriptor("rig.flow", "L/s"),
        descriptor("other.flow", "L/s", source="other"),
    )
    add(session, "rig.temp", [(0, 20.0)])
    add(session, "rig.flow", [(0, 1.0)])
    add(session, "other.flow", [(0, 1.0)])
    derived = DerivedSignalSet()
    preview = derived.preview(
        session.snapshot(),
        output_id="rig.mix",
        name="Mix",
        expression='sig("rig.temp") + sig("rig.flow")',
    )
    assert preview.ok and preview.series is not None and preview.series.unit == "unknown"
    assert "is not in the derived unit notation" in preview.notes[0]

    preview = derived.preview(
        session.snapshot(),
        output_id="rig.sum",
        name="Sum",
        expression='sig("rig.flow") + sig("other.flow")',
    )
    assert not preview.ok and "different sources or clock domains" in (preview.error or "")


def test_preview_is_bounded_and_needs_a_session(bench):
    derived = DerivedSignalSet()
    preview = derived.preview(
        None, output_id="bench.x", name="X", expression='sig("bench.force") + 1'
    )
    assert not preview.ok and "load a session" in (preview.error or "")
    preview = derived.preview(
        bench.snapshot(),
        output_id="bench.x",
        name="X",
        expression='sig("bench.force") + 1',
        limit=2,
    )
    assert preview.ok and preview.series is not None
    assert len(preview.series) == 2 and preview.total == 4 and preview.truncated
    preview = derived.preview(
        bench.snapshot(), output_id="bench.x", name="X", expression="1 +", limit=2
    )
    assert not preview.ok and "syntax error" in (preview.error or "")
    assert derived.definitions == ()


def test_remove_drops_the_signal(bench):
    derived = DerivedSignalSet()
    derived.apply(define("bench.double", 'sig("bench.force") * 2'))
    derived.remove("bench.double")
    raw = bench.snapshot()
    assert derived.augment(bench, raw) is raw and derived.statuses == {}


def test_restored_spaced_unit_never_breaks_the_descriptor(bench):
    derived = DerivedSignalSet()
    derived.restore_configuration(
        {
            "version": 1,
            "definitions": [
                define("bench.hot", 'sig("bench.force") * 2', unit="deg C").to_dict(),
                define("bench.lost", 'sig("bench.gone") * 2', unit="deg C").to_dict(),
            ],
        }
    )
    view = derived.augment(bench, bench.snapshot())
    units = {d.identifier: d.canonical_unit for d in view.schema.signals}
    assert units["bench.hot"] is None and units["bench.lost"] is None
    assert "cannot confirm declared unit" not in derived.statuses["bench.lost"].message
    assert "does not match" in derived.statuses["bench.hot"].message
