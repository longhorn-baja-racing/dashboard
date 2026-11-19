"""Offset mapping, overlap, signal checks, window queries, and save/restore."""

from __future__ import annotations

import json
from array import array
from math import isnan, nan
from typing import Any

import pytest

from lbr_dashboard.core.session_alignment import (
    FORMAT_NAME,
    FORMAT_VERSION,
    IssueKind,
    SessionAlignment,
    SessionData,
    SessionIdentity,
    Side,
    SignalMatch,
    SignalTrace,
    alignment_from_dict,
    check_matches,
    overlap,
    samples_in_window,
    window,
)

MS = 1_000_000
S = 1_000_000_000

REF_ID = SessionIdentity("run-a", "Endurance practice, lap 3")
CMP_ID = SessionIdentity("run-b", "Endurance practice, lap 7")
SPEED = SignalMatch("wheel_speed", "rear_wheel_speed")
TEMP = SignalMatch("cvt_temp", "cvt_temperature")


def _reference() -> SessionData:
    """Run A: wheel speed every 10 ms for 100 ms, CVT temperature every 50 ms."""

    return SessionData(
        REF_ID,
        (
            SignalTrace(
                "wheel_speed",
                array("q", range(0, 101 * MS, 10 * MS)),
                array("d", [float(i) for i in range(11)]),
                unit="km/h",
            ),
            SignalTrace("cvt_temp", [0, 50 * MS, 100 * MS], [60.0, 61.0, 62.0], unit="deg C"),
        ),
    )


def _compared() -> SessionData:
    """Run B: a different source with its own ids, rates, length and time base.

    Times start at 5 s (logger uptime). Speed every 25 ms for 150 ms, with a
    missing value; temperature at three uneven times.
    """

    base = 5 * S
    return SessionData(
        CMP_ID,
        [
            SignalTrace(
                "rear_wheel_speed",
                [base + i * 25 * MS for i in range(7)],
                [100.0, 101.0, None, 103.0, 104.0, nan, 106.0],
                unit="km/h",
            ),
            SignalTrace(
                "cvt_temperature",
                [base, base + 70 * MS, base + 140 * MS],
                [70.0, 71.0, 72.0],
                unit="deg C",
            ),
        ],
    )


def _bad(value: object) -> Any:
    """Pass a deliberately wrong type past the type checker."""

    return value


def _alignment(offset_ns: int = -5 * S, *matches: SignalMatch) -> SessionAlignment:
    return SessionAlignment(REF_ID, CMP_ID, offset_ns, matches or (SPEED, TEMP))


# Offset mapping -----------------------------------------------------------------


@pytest.mark.parametrize(
    "offset_ns",
    [
        pytest.param(250 * MS, id="positive"),
        pytest.param(-5 * S, id="negative"),
        pytest.param(0, id="zero"),
    ],
)
def test_mapping_round_trips_for_known_offsets(offset_ns: int) -> None:
    alignment = _alignment(offset_ns)
    for original in (0, 17, 5 * S + 3, -42):
        assert alignment.to_comparison_ns(Side.REFERENCE, original) == original
        assert alignment.to_original_ns(Side.REFERENCE, original) == original
        mapped = alignment.to_comparison_ns(Side.COMPARED, original)
        assert mapped == original + offset_ns
        assert alignment.to_original_ns(Side.COMPARED, mapped) == original
    assert alignment.offset_for(Side.REFERENCE) == 0
    assert alignment.offset_for(Side.COMPARED) == offset_ns


def test_mapping_handles_epoch_scale_integers_exactly() -> None:
    epoch_ns = 1_790_000_000_123_456_789
    alignment = _alignment(-epoch_ns + 1)
    assert alignment.to_comparison_ns(Side.COMPARED, epoch_ns) == 1
    assert alignment.to_original_ns(Side.COMPARED, 1) == epoch_ns


def test_with_offset_keeps_identities_and_matches() -> None:
    alignment = _alignment(0)
    moved = alignment.with_offset(7)
    assert (moved.reference, moved.compared, moved.matches) == (REF_ID, CMP_ID, (SPEED, TEMP))
    assert moved.offset_ns == 7
    assert alignment.offset_ns == 0
    assert alignment.identity(Side.REFERENCE) == REF_ID
    assert alignment.identity(Side.COMPARED) == CMP_ID


# Window query against the two-run fixture -----------------------------------------


def test_window_maps_known_two_run_fixture() -> None:
    # Offset -5 s puts run B's first sample at comparison time 0.
    result = window(_alignment(), _reference(), _compared(), SPEED, 20 * MS, 60 * MS)
    ref, cmp = result.reference, result.compared
    assert (result.start_ns, result.end_ns) == (20 * MS, 60 * MS)
    assert ref.comparison_times_ns == (20 * MS, 30 * MS, 40 * MS, 50 * MS, 60 * MS)
    assert ref.original_times_ns == ref.comparison_times_ns
    assert ref.values == (2.0, 3.0, 4.0, 5.0, 6.0)
    assert (ref.session, ref.side, ref.signal_id, ref.unit) == (
        REF_ID,
        Side.REFERENCE,
        "wheel_speed",
        "km/h",
    )
    assert cmp.comparison_times_ns == (25 * MS, 50 * MS)
    assert cmp.original_times_ns == (5 * S + 25 * MS, 5 * S + 50 * MS)
    assert cmp.values == (101.0, None)
    assert (cmp.session, cmp.side, cmp.signal_id) == (CMP_ID, Side.COMPARED, "rear_wheel_speed")
    assert (len(ref), len(cmp)) == (5, 2)


def test_window_bounds_are_inclusive_and_values_are_unchanged() -> None:
    result = window(_alignment(), _reference(), _compared(), SPEED, 75 * MS, 125 * MS)
    assert result.compared.comparison_times_ns == (75 * MS, 100 * MS, 125 * MS)
    values = result.compared.values
    assert values[:2] == (103.0, 104.0)
    assert values[2] is not None and isnan(values[2])
    assert result.reference.comparison_times_ns == (80 * MS, 90 * MS, 100 * MS)


@pytest.mark.parametrize(
    ("offset_ns", "expected_comparison", "expected_original"),
    [
        pytest.param(
            -5 * S + 30 * MS,
            (30 * MS, 100 * MS),
            (5 * S, 5 * S + 70 * MS),
            id="positive-shift",
        ),
        pytest.param(
            -5 * S - 70 * MS, (0, 70 * MS), (5 * S + 70 * MS, 5 * S + 140 * MS), id="negative-shift"
        ),
    ],
)
def test_window_follows_offset_for_slow_signal(
    offset_ns: int,
    expected_comparison: tuple[int, ...],
    expected_original: tuple[int, ...],
) -> None:
    result = window(_alignment(offset_ns), _reference(), _compared(), TEMP, 0, 100 * MS)
    assert result.compared.comparison_times_ns == expected_comparison
    assert result.compared.original_times_ns == expected_original
    assert result.compared.unit == "deg C"
    assert result.reference.values == (60.0, 61.0, 62.0)


def test_zero_offset_window_on_reference_timeline_misses_compared_session() -> None:
    result = window(_alignment(0), _reference(), _compared(), SPEED, 0, 100 * MS)
    assert len(result.reference) == 11
    assert len(result.compared) == 0


def test_samples_in_window_single_side_and_empty_signal() -> None:
    session = SessionData(CMP_ID, (SignalTrace("empty", (), ()),))
    samples = samples_in_window(_alignment(), Side.COMPARED, session, "empty", 0, 10)
    assert samples.original_times_ns == ()
    assert samples.values == ()


@pytest.mark.parametrize(
    ("signal_id", "start_ns", "end_ns", "message"),
    [
        pytest.param("nope", 0, 1, "signal 'nope' is not in reference session 'run-a'", id="sig"),
        pytest.param("wheel_speed", 5, 4, r"start_ns \(5\) is after end_ns \(4\)", id="order"),
        pytest.param("wheel_speed", 0.5, 4, "window bounds must be integers", id="float"),
    ],
)
def test_samples_in_window_rejects_bad_requests(
    signal_id: str, start_ns: Any, end_ns: Any, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        samples_in_window(_alignment(), Side.REFERENCE, _reference(), signal_id, start_ns, end_ns)


def test_queries_reject_a_session_not_named_in_the_alignment() -> None:
    with pytest.raises(ValueError, match="compared session is 'run-b'.*'run-a' was given"):
        window(_alignment(), _reference(), _reference(), SPEED, 0, 1)
    with pytest.raises(ValueError, match="reference session is 'run-a'"):
        overlap(_alignment(), _compared(), _compared())
    with pytest.raises(ValueError, match="reference session is 'run-a'"):
        check_matches(_alignment(), _compared(), _compared())


# Overlap --------------------------------------------------------------------------


def test_overlap_of_matched_signals() -> None:
    result = overlap(_alignment(), _reference(), _compared())
    assert result.has_overlap
    assert result.reason == ""
    assert result.reference_span == (0, 100 * MS)
    assert result.compared_span == (0, 150 * MS)
    assert (result.start_ns, result.end_ns, result.duration_ns) == (0, 100 * MS, 100 * MS)


def test_overlap_for_one_match_uses_only_that_pair() -> None:
    result = overlap(_alignment(-5 * S + 40 * MS), _reference(), _compared(), TEMP)
    assert result.compared_span == (40 * MS, 180 * MS)
    assert (result.start_ns, result.end_ns) == (40 * MS, 100 * MS)


def test_overlap_without_matches_uses_every_signal() -> None:
    alignment = SessionAlignment(REF_ID, CMP_ID, -5 * S)
    result = overlap(alignment, _reference(), _compared())
    assert (result.start_ns, result.end_ns) == (0, 100 * MS)


def test_touching_spans_overlap_at_one_instant() -> None:
    result = overlap(_alignment(-5 * S + 100 * MS), _reference(), _compared())
    assert (result.start_ns, result.end_ns, result.duration_ns) == (100 * MS, 100 * MS, 0)
    assert result.has_overlap


def test_no_overlap_when_compared_session_is_too_late() -> None:
    result = overlap(_alignment(0), _reference(), _compared())
    assert not result.has_overlap
    assert (result.start_ns, result.end_ns, result.duration_ns) == (None, None, 0)
    assert result.compared_span == (5 * S, 5 * S + 150 * MS)
    gap = 5 * S - 100 * MS
    assert result.reason == (
        f"compared session 'run-b' starts at comparison time {5 * S} ns, {gap} ns after "
        f"reference session 'run-a' ends at {100 * MS} ns; "
        f"decrease offset_ns by at least {gap} to overlap"
    )
    fixed = overlap(_alignment(-gap), _reference(), _compared())
    assert (fixed.start_ns, fixed.end_ns) == (100 * MS, 100 * MS)


def test_no_overlap_when_compared_session_is_too_early() -> None:
    result = overlap(_alignment(-6 * S), _reference(), _compared())
    assert not result.has_overlap
    gap = 1 * S - 150 * MS
    assert result.compared_span == (-1 * S, -1 * S + 150 * MS)
    assert "ends at comparison time" in result.reason
    assert f"increase offset_ns by at least {gap} to overlap" in result.reason
    fixed = overlap(_alignment(-6 * S + gap), _reference(), _compared())
    assert fixed.has_overlap


@pytest.mark.parametrize(
    ("reference_signals", "compared_signals", "reason"),
    [
        pytest.param(
            (),
            (SignalTrace("rear_wheel_speed", [1], [1.0]),),
            "reference session 'run-a' has no samples for the selected signals",
            id="ref",
        ),
        pytest.param(
            (SignalTrace("wheel_speed", [1], [1.0]),),
            (SignalTrace("rear_wheel_speed", [], []),),
            "compared session 'run-b' has no samples for the selected signals",
            id="cmp",
        ),
        pytest.param(
            (),
            (),
            "reference session 'run-a' and compared session 'run-b' have no samples",
            id="both",
        ),
    ],
)
def test_no_overlap_explains_empty_sides(
    reference_signals: tuple[SignalTrace, ...],
    compared_signals: tuple[SignalTrace, ...],
    reason: str,
) -> None:
    result = overlap(
        _alignment(0, SPEED),
        SessionData(REF_ID, reference_signals),
        SessionData(CMP_ID, compared_signals),
    )
    assert not result.has_overlap
    assert result.reason.startswith(reason)


# Matched-signal checks ------------------------------------------------------------


def test_consistent_matches_report_no_issues() -> None:
    assert check_matches(_alignment(), _reference(), _compared()) == ()


def test_check_matches_reports_missing_unit_and_frame_problems() -> None:
    reference = SessionData(
        REF_ID,
        (
            SignalTrace("wheel_speed", [0], [1.0], unit="km/h", frame="vehicle"),
            SignalTrace("accel_x", [0], [0.1], unit="g", frame="vehicle"),
            SignalTrace("rpm", [0], [3000.0]),
        ),
    )
    compared = SessionData(
        CMP_ID,
        (
            SignalTrace("rear_wheel_speed", [0], [1.0], unit="mph", frame="vehicle"),
            SignalTrace("imu_ax", [0], [0.1], unit="g", frame="sensor"),
            SignalTrace("engine_rpm", [0], [3000.0], unit="rpm"),
        ),
    )
    matches = (
        SPEED,
        SignalMatch("accel_x", "imu_ax"),
        SignalMatch("rpm", "engine_rpm"),
        SignalMatch("missing_ref", "imu_ax"),
        SignalMatch("missing_both", "also_missing"),
    )
    issues = check_matches(_alignment(0, *matches), reference, compared)
    summary = [(issue.kind, issue.match, issue.side) for issue in issues]
    assert summary == [
        (IssueKind.UNIT_MISMATCH, SPEED, None),
        (IssueKind.FRAME_MISMATCH, matches[1], None),
        (IssueKind.UNIT_MISMATCH, matches[2], None),
        (IssueKind.MISSING_SIGNAL, matches[3], Side.REFERENCE),
        (IssueKind.MISSING_SIGNAL, matches[4], Side.REFERENCE),
        (IssueKind.MISSING_SIGNAL, matches[4], Side.COMPARED),
    ]
    messages = [issue.message for issue in issues]
    assert messages[0] == (
        "units differ for 'wheel_speed' and 'rear_wheel_speed': reference 'km/h', compared 'mph'"
    )
    assert messages[1] == (
        "frames differ for 'accel_x' and 'imu_ax': reference 'vehicle', compared 'sensor'"
    )
    assert messages[2].endswith("reference (none), compared 'rpm'")
    assert messages[3] == "signal 'missing_ref' is not in reference session 'run-a'"
    assert messages[5] == "signal 'also_missing' is not in compared session 'run-b'"


def test_frame_only_on_one_side_is_a_mismatch() -> None:
    reference = SessionData(REF_ID, (SignalTrace("a", [0], [1.0], frame="vehicle"),))
    compared = SessionData(CMP_ID, (SignalTrace("b", [0], [1.0]),))
    (issue,) = check_matches(_alignment(0, SignalMatch("a", "b")), reference, compared)
    assert issue.kind is IssueKind.FRAME_MISMATCH
    assert issue.message.endswith("reference 'vehicle', compared (none)")


# Input validation -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "message"),
    [
        pytest.param(
            lambda: SignalTrace("s", [0, 1], [1.0]), "2 timestamps but 1 values", id="len"
        ),
        pytest.param(lambda: SignalTrace("s", [5, 4], [1.0, 2.0]), "non-decreasing", id="order"),
        pytest.param(lambda: SignalTrace("s", _bad([0.5]), [1.0]), "integer number of", id="float"),
        pytest.param(lambda: SignalTrace("s", _bad([True]), [1.0]), "integer number of", id="bool"),
        pytest.param(lambda: SignalTrace("", [], []), "signal_id must be non-empty", id="sig-id"),
        pytest.param(lambda: SessionIdentity(" a"), "session_id must be non-empty", id="ws"),
        pytest.param(lambda: SessionIdentity(_bad(3)), "session_id must be a string", id="id-type"),
        pytest.param(
            lambda: SessionIdentity("a", _bad(None)), "label must be a string", id="label"
        ),
        pytest.param(
            lambda: SessionData(REF_ID, (SignalTrace("s", [], []), SignalTrace("s", [], []))),
            "more than one signal 's'",
            id="dup-signal",
        ),
        pytest.param(lambda: SessionAlignment(REF_ID, REF_ID), "different sessions", id="same"),
        pytest.param(lambda: SessionAlignment(REF_ID, CMP_ID, _bad(1.5)), "integer", id="offset"),
        pytest.param(
            lambda: SessionAlignment(REF_ID, CMP_ID, 0, (SPEED, SPEED)), "twice", id="dup-match"
        ),
        pytest.param(lambda: SignalMatch("a", ""), "compared_signal_id", id="match-id"),
    ],
)
def test_invalid_inputs_raise(build: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build()


# Save and restore -----------------------------------------------------------------


def test_round_trip_through_json_keeps_identities_offset_and_matches() -> None:
    alignment = _alignment(-1_790_000_000_123_456_789)
    document = alignment.to_dict()
    assert document["format"] == FORMAT_NAME
    assert document["version"] == FORMAT_VERSION
    restored = alignment_from_dict(json.loads(json.dumps(document)))
    assert restored == alignment
    assert restored.reference.label == "Endurance practice, lap 3"
    assert restored.compared.session_id == "run-b"
    assert restored.matches == (SPEED, TEMP)


def test_restored_alignment_reproduces_the_same_window() -> None:
    restored = alignment_from_dict(json.loads(json.dumps(_alignment().to_dict())))
    expected = window(_alignment(), _reference(), _compared(), SPEED, 0, 150 * MS)
    assert window(restored, _reference(), _compared(), SPEED, 0, 150 * MS) == expected


def test_round_trip_with_no_matches() -> None:
    alignment = SessionAlignment(REF_ID, CMP_ID)
    assert alignment.to_dict()["matches"] == []
    assert alignment_from_dict(alignment.to_dict()) == alignment


def _document(**changes: Any) -> dict[str, Any]:
    document = _alignment().to_dict()
    for key, value in changes.items():
        if value is _DROP:
            del document[key]
        else:
            document[key] = value
    return document


_DROP = object()


@pytest.mark.parametrize(
    ("document", "message"),
    [
        pytest.param([], "alignment must be an object, got list", id="not-object"),
        pytest.param(_document(format="other"), "format must be 'lbr-session-alignment'", id="fmt"),
        pytest.param(_document(format=_DROP), "missing required field 'format'", id="no-fmt"),
        pytest.param(_document(version=2), "unsupported alignment version 2", id="v2"),
        pytest.param(_document(version="1"), "version must be an integer", id="v-str"),
        pytest.param(_document(version=True), "version must be an integer", id="v-bool"),
        pytest.param(_document(version=_DROP), "missing required field 'version'", id="no-v"),
        pytest.param(_document(offset_ns=_DROP), "missing required field 'offset_ns'", id="no-off"),
        pytest.param(_document(offset_ns=1.0), "offset_ns must be an integer", id="off-float"),
        pytest.param(_document(offset_ns="5"), "offset_ns must be an integer", id="off-str"),
        pytest.param(_document(extra=1), "unknown field\\(s\\): extra", id="extra"),
        pytest.param(_document(reference=_DROP), "missing required field 'reference'", id="no-ref"),
        pytest.param(_document(reference="run-a"), "reference must be an object", id="ref-str"),
        pytest.param(
            _document(compared={"session_id": "run-b"}),
            "compared is missing required field 'label'",
            id="no-label",
        ),
        pytest.param(
            _document(compared={"session_id": 7, "label": ""}),
            "compared.session_id must be a string, got int",
            id="id-int",
        ),
        pytest.param(
            _document(compared={"session_id": "", "label": ""}),
            "compared: session_id must be non-empty",
            id="id-empty",
        ),
        pytest.param(
            _document(compared={"session_id": "run-a", "label": ""}),
            "different sessions",
            id="same-session",
        ),
        pytest.param(_document(matches={}), "matches must be a list, got dict", id="matches"),
        pytest.param(
            _document(matches=[{"reference": "a"}]),
            "matches\\[0\\] is missing required field 'compared'",
            id="match-field",
        ),
        pytest.param(
            _document(matches=[{"reference": "a", "compared": None}]),
            "matches\\[0\\].compared must be a string, got NoneType",
            id="match-type",
        ),
        pytest.param(
            _document(matches=[{"reference": "a", "compared": " b"}]),
            "matches\\[0\\]: compared_signal_id must be non-empty",
            id="match-ws",
        ),
        pytest.param(_document(matches=["a"]), "matches\\[0\\] must be an object", id="match-str"),
    ],
)
def test_invalid_documents_raise_useful_errors(document: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        alignment_from_dict(document)
