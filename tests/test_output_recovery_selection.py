"""Unit tests for `_select_best_candidate` priority in output recovery.

These tests cover the RDP <-> physical-display switch behavior where the
previously cached output becomes detached and recovery must prefer an output
that is currently `AttachedToDesktop=1`, optionally on a different adapter.
"""

from __future__ import annotations

import pytest

pytest.importorskip("comtypes")

from dxcam.core.output_recovery import _OutputCandidate, _select_best_candidate


class _FakeDesc:
    """Stand-in for `DXGI_OUTPUT_DESC` with only the fields recovery looks at."""

    def __init__(
        self,
        *,
        device_name: str,
        attached: bool,
        monitor: int,
    ) -> None:
        self.DeviceName = device_name
        self.AttachedToDesktop = 1 if attached else 0
        self.Monitor = monitor


def _make_candidate(
    *,
    label: str,
    adapter_luid: tuple[int, int],
    device_name: str = "\\\\.\\DISPLAY1",
    attached: bool = True,
    monitor: int = 0,
) -> _OutputCandidate:
    return _OutputCandidate(
        output_ptr=label,
        adapter_ptr=f"adapter:{adapter_luid}",
        adapter_luid=adapter_luid,
        desc=_FakeDesc(
            device_name=device_name,
            attached=attached,
            monitor=monitor,
        ),
    )


def test_returns_none_when_no_candidates() -> None:
    selected = _select_best_candidate(
        [],
        previous_monitor=0,
        previous_name="",
        previous_luid=None,
    )
    assert selected is None


def test_prefers_attached_over_detached_with_matching_name() -> None:
    """Reproduces the original RDP bug.

    The detached candidate has the same DeviceName/adapter as before (which the
    old code would have selected); recovery must skip it and pick the only
    attached output, even on a different adapter.
    """
    rdp_luid = (1, 0)
    gpu_luid = (2, 0)

    detached_rdp = _make_candidate(
        label="rdp",
        adapter_luid=rdp_luid,
        device_name="\\\\.\\DISPLAY1",
        attached=False,
        monitor=0,
    )
    attached_physical = _make_candidate(
        label="physical",
        adapter_luid=gpu_luid,
        device_name="\\\\.\\DISPLAY2",
        attached=True,
        monitor=4242,
    )

    selected = _select_best_candidate(
        [detached_rdp, attached_physical],
        previous_monitor=0,
        previous_name="\\\\.\\DISPLAY1",
        previous_luid=rdp_luid,
    )
    assert selected is attached_physical


def test_prefers_attached_with_monitor_match_over_other_attached() -> None:
    other_attached = _make_candidate(
        label="other",
        adapter_luid=(2, 0),
        device_name="\\\\.\\DISPLAY2",
        attached=True,
        monitor=99,
    )
    same_monitor_attached = _make_candidate(
        label="same-monitor",
        adapter_luid=(1, 0),
        device_name="\\\\.\\DISPLAY1",
        attached=True,
        monitor=1234,
    )

    selected = _select_best_candidate(
        [other_attached, same_monitor_attached],
        previous_monitor=1234,
        previous_name="\\\\.\\DISPLAY1",
        previous_luid=(1, 0),
    )
    assert selected is same_monitor_attached


def test_prefers_attached_same_adapter_when_name_does_not_match() -> None:
    attached_other_adapter = _make_candidate(
        label="other-adapter",
        adapter_luid=(2, 0),
        device_name="\\\\.\\DISPLAY9",
        attached=True,
        monitor=999,
    )
    attached_same_adapter = _make_candidate(
        label="same-adapter",
        adapter_luid=(1, 0),
        device_name="\\\\.\\DISPLAY7",
        attached=True,
        monitor=777,
    )

    selected = _select_best_candidate(
        [attached_other_adapter, attached_same_adapter],
        previous_monitor=0,
        previous_name="",
        previous_luid=(1, 0),
    )
    assert selected is attached_same_adapter


def test_falls_back_to_detached_candidate_only_when_nothing_attached() -> None:
    detached_a = _make_candidate(
        label="a",
        adapter_luid=(1, 0),
        device_name="\\\\.\\DISPLAY1",
        attached=False,
        monitor=0,
    )
    detached_b = _make_candidate(
        label="b",
        adapter_luid=(2, 0),
        device_name="\\\\.\\DISPLAY2",
        attached=False,
        monitor=0,
    )

    selected = _select_best_candidate(
        [detached_a, detached_b],
        previous_monitor=0,
        previous_name="\\\\.\\DISPLAY2",
        previous_luid=(2, 0),
    )
    # Falls back to name+adapter match in the detached tier.
    assert selected is detached_b
