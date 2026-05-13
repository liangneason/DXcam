from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Any, Callable

import comtypes

from dxcam._libs.dxgi import (
    DXGI_ADAPTER_DESC1,
    DXGI_OUTPUT_DESC,
    IDXGIOutput1,
)
from dxcam.core.device import Device
from dxcam.core.dxgi_errors import (
    DXGITransientContext,
    com_error_hresult_u32,
    is_transient_com_error,
    is_transient_hresult,
)
from dxcam.core.output import Output
from dxcam.types import Region
from dxcam.util.io import enum_dxgi_adapters


@dataclass(frozen=True)
class OutputState:
    width: int
    height: int
    rotation_angle: int
    region: Region
    region_was_clamped: bool


@dataclass
class _OutputCandidate:
    output_ptr: Any
    adapter_ptr: Any
    adapter_luid: tuple[int, int]
    desc: DXGI_OUTPUT_DESC


def _monitor_handle_to_int(handle: Any) -> int:
    value = getattr(handle, "value", handle)
    return int(value or 0)


def _region_in_bounds(region: Region, width: int, height: int) -> bool:
    left, top, right, bottom = region
    return width >= right > left >= 0 and height >= bottom > top >= 0


def _clamp_region(region: Region, width: int, height: int) -> Region:
    if width <= 0 or height <= 0:
        raise RuntimeError(
            f"Cannot clamp region with invalid output size {width}x{height}."
        )
    left, top, right, bottom = region
    left = min(max(int(left), 0), width - 1)
    top = min(max(int(top), 0), height - 1)
    right = min(max(int(right), left + 1), width)
    bottom = min(max(int(bottom), top + 1), height)
    return left, top, right, bottom


def _adapter_luid(adapter_ptr: Any) -> tuple[int, int]:
    desc = DXGI_ADAPTER_DESC1()
    adapter_ptr.GetDesc1(ctypes.byref(desc))
    return (int(desc.AdapterLuid.LowPart), int(desc.AdapterLuid.HighPart))


def _safe_get_output_desc(output_ptr: Any) -> DXGI_OUTPUT_DESC | None:
    desc = DXGI_OUTPUT_DESC()
    try:
        output_ptr.GetDesc(ctypes.byref(desc))
    except comtypes.COMError as exc:
        if is_transient_com_error(
            exc,
            DXGITransientContext.SYSTEM_TRANSITION,
            DXGITransientContext.ENUM_OUTPUTS,
        ):
            return None
        raise
    return desc


def _select_best_candidate(
    candidates: list[_OutputCandidate],
    *,
    previous_monitor: int,
    previous_name: str,
    previous_luid: tuple[int, int] | None,
) -> _OutputCandidate | None:
    """Pick the best output candidate using the desktop-duplication priority.

    Priority (earlier tiers win):
        1. Attached + monitor handle matches the previous one.
        2. Attached + DeviceName matches and on the previous adapter.
        3. Attached + DeviceName matches (any adapter).
        4. Attached + on the previous adapter.
        5. Any attached output.
        6. Detached candidates following the same heuristics (fallback).
    """
    if not candidates:
        return None

    def attached(c: _OutputCandidate) -> bool:
        return bool(c.desc.AttachedToDesktop)

    def monitor_matches(c: _OutputCandidate) -> bool:
        return previous_monitor != 0 and (
            _monitor_handle_to_int(c.desc.Monitor) == previous_monitor
        )

    def name_matches(c: _OutputCandidate) -> bool:
        return bool(previous_name) and str(c.desc.DeviceName) == previous_name

    def same_adapter(c: _OutputCandidate) -> bool:
        return previous_luid is not None and c.adapter_luid == previous_luid

    tiers: tuple[Callable[[_OutputCandidate], bool], ...] = (
        lambda c: attached(c) and monitor_matches(c),
        lambda c: attached(c) and name_matches(c) and same_adapter(c),
        lambda c: attached(c) and name_matches(c),
        lambda c: attached(c) and same_adapter(c),
        lambda c: attached(c),
        lambda c: monitor_matches(c),
        lambda c: name_matches(c) and same_adapter(c),
        lambda c: name_matches(c),
        lambda c: same_adapter(c),
        lambda c: True,
    )

    for predicate in tiers:
        for candidate in candidates:
            if predicate(candidate):
                return candidate
    return None


class OutputRecoveryHandler:
    """Resolves current output geometry/rotation during display transitions."""

    def __init__(self, output: Output, device: Device) -> None:
        self._output = output
        self._device = device

    # ------------------------------------------------------------------
    # Output enumeration helpers
    # ------------------------------------------------------------------

    def _enumerate_outputs_on_adapter(
        self, adapter_ptr: Any
    ) -> list[_OutputCandidate]:
        try:
            luid = _adapter_luid(adapter_ptr)
        except comtypes.COMError as exc:
            if is_transient_com_error(
                exc,
                DXGITransientContext.SYSTEM_TRANSITION,
                DXGITransientContext.ENUM_OUTPUTS,
            ):
                return []
            raise

        candidates: list[_OutputCandidate] = []
        i = 0
        while True:
            try:
                p_output = ctypes.POINTER(IDXGIOutput1)()
                adapter_ptr.EnumOutputs(i, ctypes.byref(p_output))
            except comtypes.COMError as exc:
                hresult_u32 = com_error_hresult_u32(exc)
                if is_transient_hresult(
                    hresult_u32,
                    DXGITransientContext.ENUM_OUTPUTS,
                ):
                    break
                raise
            i += 1
            desc = _safe_get_output_desc(p_output)
            if desc is None:
                continue
            candidates.append(
                _OutputCandidate(
                    output_ptr=p_output,
                    adapter_ptr=adapter_ptr,
                    adapter_luid=luid,
                    desc=desc,
                )
            )
        return candidates

    def _enumerate_all_outputs(self) -> list[_OutputCandidate]:
        """Enumerate every output across every DXGI adapter.

        Searches the currently-bound adapter first (cheap), then enumerates a
        fresh ``IDXGIFactory1`` so adapters that came online (or whose output
        set changed) since process start are visible. This is required for
        the RDP <-> physical display switch where the active desktop output
        moves from the indirect-display adapter to the physical GPU.
        """
        candidates: list[_OutputCandidate] = []
        try:
            candidates.extend(
                self._enumerate_outputs_on_adapter(self._device.adapter)
            )
        except comtypes.COMError as exc:
            if not is_transient_com_error(
                exc,
                DXGITransientContext.SYSTEM_TRANSITION,
                DXGITransientContext.ENUM_OUTPUTS,
            ):
                raise

        try:
            adapters = enum_dxgi_adapters()
        except comtypes.COMError as exc:
            if not is_transient_com_error(
                exc,
                DXGITransientContext.SYSTEM_TRANSITION,
                DXGITransientContext.ENUM_OUTPUTS,
            ):
                raise
            adapters = []

        seen_luids = {c.adapter_luid for c in candidates}
        for adapter_ptr in adapters:
            try:
                luid = _adapter_luid(adapter_ptr)
            except comtypes.COMError as exc:
                if is_transient_com_error(
                    exc,
                    DXGITransientContext.SYSTEM_TRANSITION,
                    DXGITransientContext.ENUM_OUTPUTS,
                ):
                    continue
                raise
            if luid in seen_luids:
                continue
            try:
                candidates.extend(self._enumerate_outputs_on_adapter(adapter_ptr))
                seen_luids.add(luid)
            except comtypes.COMError as exc:
                if not is_transient_com_error(
                    exc,
                    DXGITransientContext.SYSTEM_TRANSITION,
                    DXGITransientContext.ENUM_OUTPUTS,
                ):
                    raise

        return candidates

    # ------------------------------------------------------------------
    # Recovery path
    # ------------------------------------------------------------------

    def _current_adapter_luid(self) -> tuple[int, int] | None:
        try:
            return _adapter_luid(self._device.adapter)
        except comtypes.COMError as exc:
            if is_transient_com_error(
                exc,
                DXGITransientContext.SYSTEM_TRANSITION,
                DXGITransientContext.ENUM_OUTPUTS,
            ):
                return None
            raise

    def _refresh_output_desc(self) -> None:
        previous_monitor = _monitor_handle_to_int(self._output.hmonitor)
        previous_name = str(self._output.devicename or "")
        previous_luid = self._current_adapter_luid()

        # Fast path: the cached output pointer still describes an attached
        # output. If GetDesc succeeds but the output is detached (the RDP
        # virtual display being torn down is a common case), fall through and
        # search for a real attached output instead of trusting the stale
        # pointer.
        try:
            self._output.update_desc()
            if self._output.attached_to_desktop:
                return
        except comtypes.COMError as exc:
            if not is_transient_com_error(
                exc,
                DXGITransientContext.SYSTEM_TRANSITION,
                DXGITransientContext.ENUM_OUTPUTS,
            ):
                raise

        candidates = self._enumerate_all_outputs()
        selected = _select_best_candidate(
            candidates,
            previous_monitor=previous_monitor,
            previous_name=previous_name,
            previous_luid=previous_luid,
        )
        if selected is None:
            raise RuntimeError("No DXGI outputs available during recovery.")

        # If the chosen output lives on a different adapter (e.g., desktop
        # moved from RDP indirect display to the physical GPU), rebind the
        # device wrapper so the duplicator/stage surface get rebuilt against
        # the correct D3D11 device.
        if previous_luid is None or selected.adapter_luid != previous_luid:
            self._device.rebind_to_adapter(selected.adapter_ptr)

        self._output.output = selected.output_ptr
        self._output.update_desc()

    def handle(
        self,
        *,
        requested_region: Region,
        region_set_by_user: bool,
    ) -> OutputState:
        self._refresh_output_desc()
        if not self._output.attached_to_desktop:
            # Include output device details in the error message for debugging.
            output_info = (
                f"DeviceName: {self._output.devicename}, "
                f"Resolution: {self._output.resolution}, "
                f"Rotation: {self._output.rotation_angle}, "
                f"Monitor Handle: {self._output.hmonitor}, "
                f"Attached to Desktop: {self._output.attached_to_desktop}"
            )
            raise RuntimeError(
                f"Output is not attached to desktop. Output details: {output_info}"
            )

        width, height = self._output.resolution
        rotation_angle = self._output.rotation_angle

        if not region_set_by_user:
            return OutputState(
                width=width,
                height=height,
                rotation_angle=rotation_angle,
                region=(0, 0, width, height),
                region_was_clamped=False,
            )

        if _region_in_bounds(requested_region, width, height):
            return OutputState(
                width=width,
                height=height,
                rotation_angle=rotation_angle,
                region=requested_region,
                region_was_clamped=False,
            )

        clamped = _clamp_region(requested_region, width, height)
        return OutputState(
            width=width,
            height=height,
            rotation_angle=rotation_angle,
            region=clamped,
            region_was_clamped=clamped != requested_region,
        )
