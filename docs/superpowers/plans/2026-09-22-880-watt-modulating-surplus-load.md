# Watt-Modulating Surplus Loads (#880) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A surplus load whose control is a `number` entity receives the watts SEM allocated to it, instead of being switched fully on and reported as consuming its nameplate.

**Architecture:** The contract already exists and is already called — `ControllableDevice.activate(available_watts)` and `.adjust_power(available_watts)` are abstract, and `SurplusController` calls both. What is missing is an implementation that writes the number. This adds one device class beside `SwitchDevice`, selects it in the one factory that builds surplus devices, and teaches the shed path to restore a setpoint rather than a boolean. The allocator is not touched.

**Tech Stack:** Python 3.13/3.14, Home Assistant custom component, pytest. No new dependencies.

---

## Background an engineer needs

@jonasbkarlsson reported (#880) that SEM detects surplus correctly and then writes nothing to a configured `number` control; the allocation stays at 0 W. He reproduced it with a plain Home Assistant number helper, so no brand or integration is implicated. Calling `number.set_value` by hand works immediately.

The cause, verified on `develop`:

```python
# devices/base.py:1729 — SwitchDevice.activate
async def activate(self, available_watts: float) -> float:
    ...
    await self.send("homeassistant", "turn_on", {"entity_id": self.entity_id})
    self._status.current_consumption_w = self.rated_power
    return self.rated_power
```

`available_watts` is accepted, documented, and discarded. Every surplus device is built as a `SwitchDevice` (`features/device_registry.py:986`) unless it is an EV charger, which gets `CurrentControlDevice` — the only class that uses the argument, and it converts watts to amps for a charger rather than writing watts.

The milestone context: 2.1 promises deciding how surplus is spent "across battery, EV, loads and export". Battery, EV and export shipped. This is the loads half.

### What already works and must not be re-built

- `SurplusController` calls `device.activate(watts)` at `coordinator/surplus_controller.py:797` (via `_activate_owned`) and `device.adjust_power(intent.power_w)` at `:857` and `:1878`. A device that implements both is driven continuously with no allocator change.
- Ownership is recorded at that one choke point (`_activate_owned` / `_deactivate_owned`), guarded by `tests/test_load_ownership_choke_point.py`. Do not set `_sem_owned` in the new class.
- The fit gate is `effective_surplus >= device.min_power_threshold` at `coordinator/surplus_controller.py:1842`.
- Observer mode is handled by the controller, not the device (`OBSERVER · WOULD ADJUST` at `:777`). The new class must not check observer mode itself.

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `devices/power_setpoint.py` | **New.** The watt-modulating device class, alone in its own module so `devices/base.py` does not grow again. | Create |
| `devices/base.py` | Re-export the new class beside the others so existing `from .base import ...` call sites keep working. | Modify (one import line, one `__all__` entry) |
| `features/device_registry.py:986` | Build the new class when the discovered control is a `number`, `SwitchDevice` otherwise. | Modify |
| `features/load_management.py:1474` | The shed path: a modulating load sheds to its minimum and restores to what it was, not to on/off. | Modify |
| `tests/test_880_watt_modulating_load.py` | **New.** The reporter's own case plus the states the weather will not produce. | Create |

`devices/base.py` is 3,900 lines. The new class goes in its own module; do not add a sixth device class to that file.

---

## Task 1: The device class writes the watts it was given

**Files:**
- Create: `devices/power_setpoint.py`
- Test: `tests/test_880_watt_modulating_load.py`

- [ ] **Step 1: Write the failing test**

```python
"""#880 — a surplus load whose control is a number entity gets the watts.

@jonasbkarlsson: SEM detects the surplus and writes nothing, so the
allocation stays at 0 W. Reproduced by the reporter with a plain Home
Assistant number helper, so no integration is implicated.

`SwitchDevice.activate(available_watts)` accepts the argument, documents
it, and discards it: it calls `homeassistant.turn_on` and reports
`rated_power`. There was no class that writes a setpoint.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.solar_energy_management.devices.power_setpoint import (
    PowerSetpointDevice,
)

ENTITY = "number.mypv_ac_thor_9s_power_ac9"


def _hass(current: float = 0.0, minimum: float = 0.0, maximum: float = 9000.0,
          step: float = 1.0):
    hass = MagicMock()
    hass.states.get = MagicMock(return_value=SimpleNamespace(
        state=str(current),
        attributes={"min": minimum, "max": maximum, "step": step}))
    hass.services.async_call = AsyncMock()
    return hass


def _device(hass, **kw):
    return PowerSetpointDevice(
        hass=hass, device_id="ac_thor", name="AC-THOR 9s",
        rated_power=9000.0, entity_id=ENTITY, **kw)


@pytest.mark.unit
class TestItWritesTheWattsItWasGiven:

    @pytest.mark.asyncio
    async def test_activate_writes_the_allocation(self):
        hass = _hass()
        d = _device(hass)
        consumed = await d.activate(2500.0)
        hass.services.async_call.assert_awaited_once()
        domain, service, payload = hass.services.async_call.await_args.args[:3]
        assert (domain, service) == ("number", "set_value")
        assert payload == {"entity_id": ENTITY, "value": 2500.0}
        assert consumed == 2500.0

    @pytest.mark.asyncio
    async def test_it_reports_the_setpoint_not_the_nameplate(self):
        """The bug's other half: a switch claims rated_power whatever it got,
        so the allocator's accounting was wrong even when it turned on."""
        hass = _hass()
        d = _device(hass)
        await d.activate(2500.0)
        assert d.status.current_consumption_w == 2500.0
        assert d.status.allocated_power_w == 2500.0
        assert d.rated_power == 9000.0            # nameplate unchanged
```

- [ ] **Step 2: Run it and watch it fail**

```bash
rsync -a --delete --exclude=.git --exclude=node_modules ./ /tmp/ha-880/custom_components/solar_energy_management/
cd /tmp/ha-880 && PYTHONPATH=/tmp/ha-880 python3.12 -m pytest \
  custom_components/solar_energy_management/tests/test_880_watt_modulating_load.py -q
```

Expected: `ModuleNotFoundError: No module named '...devices.power_setpoint'`

- [ ] **Step 3: Write the class**

```python
"""(#880) A surplus load whose control is a watt setpoint, not a switch.

SEM's surplus allocator has always handed each device the watts it may
have — ``activate(available_watts)`` and ``adjust_power(available_watts)``
are on the base contract and the controller calls both. Every
implementation but the EV chargers' threw the number away: ``SwitchDevice``
turns the entity fully on and reports its nameplate, so an AC-THOR or any
other modulating load ran at whatever it felt like and the accounting was
wrong on top (#880, reproduced by the reporter on a plain number helper).

This class is the missing one. It writes the allocation, clamped to the
entity's own min/max and quantised to its step, and reports what it wrote.

It does NOT check observer mode — the controller does that before calling
(``coordinator/surplus_controller.py``) — and it does NOT set
``_sem_owned``: ownership is recorded at the one choke point,
``_activate_owned``, and guarded by ``tests/test_load_ownership_choke_point.py``.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from homeassistant.core import HomeAssistant

from .base import ControllableDevice, DeviceState, DeviceType

_LOGGER = logging.getLogger(__name__)

#: Fallbacks for an entity that declares no bounds. A number entity should
#: carry min/max, but a template one need not, and refusing to drive it
#: would be worse than driving it between 0 and its nameplate.
_DEFAULT_MIN_W = 0.0


class PowerSetpointDevice(ControllableDevice):
    """A load whose draw SEM sets in watts, through a ``number`` entity."""

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        name: str,
        rated_power: float,
        priority: int = 5,
        min_power_threshold: float = 0.0,
        entity_id: Optional[str] = None,
        power_entity_id: Optional[str] = None,
        energy_entity_id: Optional[str] = None,
    ) -> None:
        super().__init__(
            hass, device_id, name, priority,
            min_power_threshold, entity_id, power_entity_id,
            energy_entity_id=energy_entity_id,
        )
        self.rated_power = rated_power

    @property
    def device_type(self) -> DeviceType:
        return DeviceType.SETPOINT

    # ── the entity's own bounds, read live ────────────────────────────
    def _bounds(self) -> tuple[float, float, float]:
        """``(min, max, step)`` from the entity, with honest fallbacks."""
        lo, hi, step = _DEFAULT_MIN_W, self.rated_power, 1.0
        state = self.hass.states.get(self.entity_id) if self.entity_id else None
        attrs = getattr(state, "attributes", None) or {}
        for key, default in (("min", lo), ("max", hi), ("step", step)):
            try:
                value = float(attrs.get(key, default))
            except (TypeError, ValueError):
                continue
            if key == "min":
                lo = value
            elif key == "max":
                hi = value
            elif value > 0:
                step = value
        if hi < lo:
            lo, hi = hi, lo
        return lo, hi, step

    def _clamp(self, watts: float) -> float:
        lo, hi, step = self._bounds()
        value = max(lo, min(float(watts), hi))
        if step > 0:
            value = round(value / step) * step
        return max(lo, min(value, hi))

    async def _write(self, watts: float) -> float:
        """Write the setpoint and record it. Returns what was written."""
        if not self.entity_id:
            return 0.0
        value = self._clamp(watts)
        await self.send("number", "set_value",
                        {"entity_id": self.entity_id, "value": value})
        self._status.current_consumption_w = value
        self._status.allocated_power_w = value
        return value

    async def activate(self, available_watts: float) -> float:
        if not self.entity_id:
            return 0.0
        if self._last_deactivated:
            elapsed = (datetime.now() - self._last_deactivated).total_seconds()
            if elapsed < self.min_off_seconds:
                return 0.0
        try:
            written = await self._write(available_watts)
        except Exception as e:  # noqa: BLE001 — one device must not break the pass
            _LOGGER.error("Failed to set %s to %.0fW: %s", self.name,
                          available_watts, e)
            self._status.state = DeviceState.ERROR
            self._status.error_message = str(e)
            return 0.0
        if written <= 0:
            return 0.0
        self._status.state = DeviceState.ACTIVE
        self._status.last_activated = datetime.now()
        self._last_activated = self._status.last_activated   # (#644) unified clock
        self._status.activation_count += 1
        _LOGGER.info("Set %s to %.0fW (surplus)", self.name, written)
        return written

    async def adjust_power(self, available_watts: float) -> float:
        """The whole point of the class: follow the surplus while running."""
        if not self.is_active:
            return 0.0
        try:
            return await self._write(available_watts)
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("Failed to adjust %s to %.0fW: %s", self.name,
                          available_watts, e)
            return self._status.current_consumption_w

    async def deactivate(self) -> None:
        if not self.entity_id:
            return
        if self._last_activated:
            elapsed = (datetime.now() - self._last_activated).total_seconds()
            if elapsed < self.min_on_seconds:
                return
        lo, _hi, _step = self._bounds()
        try:
            await self.send("number", "set_value",
                            {"entity_id": self.entity_id, "value": lo})
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("Failed to stop %s: %s", self.name, e)
            return
        self._status.state = DeviceState.IDLE
        self._status.current_consumption_w = 0.0
        self._status.allocated_power_w = 0.0
        self._status.last_deactivated = datetime.now()
        self._last_deactivated = self._status.last_deactivated
        _LOGGER.info("Stopped %s (setpoint → %.0fW)", self.name, lo)
```

- [ ] **Step 4: Run the test and watch it pass**

```bash
cd /tmp/ha-880 && PYTHONPATH=/tmp/ha-880 python3.12 -m pytest \
  custom_components/solar_energy_management/tests/test_880_watt_modulating_load.py -q
```

Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add devices/power_setpoint.py tests/test_880_watt_modulating_load.py
git commit -m "feat(#880): a surplus load can take a watt setpoint

SEM's allocator has always handed each device the watts it may have —
activate(available_watts) and adjust_power(available_watts) are on the base
contract and the controller calls both. Every implementation but the EV
chargers' threw the number away: SwitchDevice turns the entity fully on and
reports its nameplate. This is the class that writes it.

Refs #880"
```

---

## Task 2: It follows the surplus, and stops at the entity's floor

**Files:**
- Modify: `tests/test_880_watt_modulating_load.py`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.unit
class TestItFollowsTheSurplus:

    @pytest.mark.asyncio
    async def test_adjust_writes_the_new_allocation(self):
        hass = _hass()
        d = _device(hass)
        await d.activate(2500.0)
        hass.services.async_call.reset_mock()
        consumed = await d.adjust_power(1200.0)
        assert consumed == 1200.0
        payload = hass.services.async_call.await_args.args[2]
        assert payload["value"] == 1200.0

    @pytest.mark.asyncio
    async def test_adjust_does_nothing_when_not_running(self):
        hass = _hass()
        d = _device(hass)
        assert await d.adjust_power(1200.0) == 0.0
        hass.services.async_call.assert_not_awaited()


@pytest.mark.unit
class TestItRespectsTheEntitysOwnBounds:

    @pytest.mark.asyncio
    async def test_it_never_writes_above_the_entity_max(self):
        hass = _hass(maximum=3000.0)
        d = _device(hass)
        assert await d.activate(9000.0) == 3000.0

    @pytest.mark.asyncio
    async def test_it_quantises_to_the_entity_step(self):
        hass = _hass(step=100.0)
        d = _device(hass)
        assert await d.activate(2437.0) == 2400.0

    @pytest.mark.asyncio
    async def test_a_load_with_a_floor_stops_at_its_floor(self):
        """An AC-THOR's minimum is 0, but a load with a real floor must not
        be written below it — that is a value the hardware will refuse."""
        hass = _hass(minimum=500.0)
        d = _device(hass)
        await d.activate(2500.0)
        d._last_activated = None            # bypass the min-on clock
        await d.deactivate()
        payload = hass.services.async_call.await_args.args[2]
        assert payload["value"] == 500.0
        assert d.status.current_consumption_w == 0.0

    @pytest.mark.asyncio
    async def test_an_entity_with_no_bounds_still_drives(self):
        """A template number need not declare min/max. Refusing to drive it
        would be worse than driving it between 0 and its nameplate."""
        hass = MagicMock()
        hass.states.get = MagicMock(return_value=SimpleNamespace(
            state="0", attributes={}))
        hass.services.async_call = AsyncMock()
        d = _device(hass)
        assert await d.activate(2500.0) == 2500.0
```

- [ ] **Step 2: Run them**

```bash
cd /tmp/ha-880 && PYTHONPATH=/tmp/ha-880 python3.12 -m pytest \
  custom_components/solar_energy_management/tests/test_880_watt_modulating_load.py -q
```

Expected: `7 passed`. They exercise Task 1's code; if any fail, fix
`_bounds`/`_clamp` rather than the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_880_watt_modulating_load.py
git commit -m "test(#880): it follows the surplus and respects the entity's bounds

Refs #880"
```

---

## Task 3: The factory builds it when the control is a number

**Files:**
- Modify: `features/device_registry.py:986`
- Modify: `devices/base.py` (re-export)
- Modify: `tests/test_880_watt_modulating_load.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.unit
class TestTheFactoryPicksIt:

    def test_a_number_control_builds_a_setpoint_device(self):
        """The reporter configured 'Control type: Number entity' and got a
        SwitchDevice, which is why nothing was ever written."""
        from custom_components.solar_energy_management.features.device_registry import (
            device_class_for_control,
        )
        from custom_components.solar_energy_management.devices.base import SwitchDevice

        assert device_class_for_control("number.ac_thor") is PowerSetpointDevice
        assert device_class_for_control("switch.towel_heater") is SwitchDevice
        assert device_class_for_control("input_number.x") is PowerSetpointDevice

    def test_no_entity_falls_back_to_the_switch(self):
        from custom_components.solar_energy_management.features.device_registry import (
            device_class_for_control,
        )
        from custom_components.solar_energy_management.devices.base import SwitchDevice

        assert device_class_for_control("") is SwitchDevice
        assert device_class_for_control(None) is SwitchDevice
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd /tmp/ha-880 && PYTHONPATH=/tmp/ha-880 python3.12 -m pytest \
  custom_components/solar_energy_management/tests/test_880_watt_modulating_load.py -q -k Factory
```

Expected: `ImportError: cannot import name 'device_class_for_control'`

- [ ] **Step 3: Add the resolver and use it**

In `features/device_registry.py`, above the class that builds devices:

```python
from ..devices.power_setpoint import PowerSetpointDevice


#: (#880) Domains whose control is a watt SETPOINT rather than a contact.
#: The reporter configured "Control type: Number entity" and got a
#: SwitchDevice, so SEM turned the AC-THOR fully on and wrote no value.
_SETPOINT_DOMAINS = ("number", "input_number")


def device_class_for_control(entity_id: str | None):
    """Which device class drives this control entity.

    One place, so the factory and the shed path cannot disagree about what
    a device is.
    """
    domain = str(entity_id or "").split(".", 1)[0]
    return PowerSetpointDevice if domain in _SETPOINT_DOMAINS else SwitchDevice
```

Then at `features/device_registry.py:986`, replace `SwitchDevice(` with the
resolved class:

```python
                surplus_device = device_class_for_control(entity)(
                    hass=self.hass,
                    device_id=device.device_id,
                    name=device.name,
                    rated_power=self._initial_rated_power(
                        device.device_id, device.power_sensor),
                    priority=device.priority,
                    entity_id=entity,
                    power_entity_id=device.power_sensor,
```

(The remaining keyword arguments on that call are unchanged — both classes
take the same constructor signature.)

In `devices/base.py`, beside the other device exports at the end of the
module:

```python
# (#880) Re-exported so `from .base import PowerSetpointDevice` works for
# the call sites that already import every other device class from here.
from .power_setpoint import PowerSetpointDevice  # noqa: E402,F401
```

- [ ] **Step 4: Run the test and watch it pass**

```bash
cd /tmp/ha-880 && PYTHONPATH=/tmp/ha-880 python3.12 -m pytest \
  custom_components/solar_energy_management/tests/test_880_watt_modulating_load.py -q
```

Expected: `9 passed`

- [ ] **Step 5: Commit**

```bash
git add features/device_registry.py devices/base.py tests/test_880_watt_modulating_load.py
git commit -m "feat(#880): a number-controlled load is built as a setpoint device

The reporter configured 'Control type: Number entity' and got a
SwitchDevice — which is why SEM turned the AC-THOR fully on and wrote no
value. One resolver, so the factory and the shed path cannot disagree.

Refs #880"
```

---

## Task 4: Shedding a modulating load turns it down, not off

**Files:**
- Modify: `features/load_management.py:1474`
- Modify: `tests/test_880_watt_modulating_load.py`

Peak shedding currently knows two control types, `switch` and `current`
(`features/load_management.py:1476` and `:1492`). A `number`-controlled
surplus load falls through both and is not shed at all.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.unit
class TestSheddingTurnsItDownNotOff:

    @pytest.mark.asyncio
    async def test_a_setpoint_load_sheds_to_its_floor(self):
        """It fell through both branches of the shed dispatch and was not
        shed at all — a modulating load ignored the peak guard entirely."""
        from custom_components.solar_energy_management.features.load_management import (
            shed_control_call,
        )
        call = shed_control_call({"type": "setpoint", "entity": ENTITY}, floor=0.0)
        assert call == ("number", "set_value", {"entity_id": ENTITY, "value": 0.0})

    @pytest.mark.asyncio
    async def test_restore_puts_back_the_value_it_found(self):
        from custom_components.solar_energy_management.features.load_management import (
            restore_control_call,
        )
        call = restore_control_call(
            {"type": "setpoint", "entity": ENTITY}, previous=2500.0)
        assert call == ("number", "set_value", {"entity_id": ENTITY, "value": 2500.0})
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd /tmp/ha-880 && PYTHONPATH=/tmp/ha-880 python3.12 -m pytest \
  custom_components/solar_energy_management/tests/test_880_watt_modulating_load.py -q -k Shedding
```

Expected: `ImportError: cannot import name 'shed_control_call'`

- [ ] **Step 3: Add the two pure helpers and use them**

In `features/load_management.py`, above the shed method:

```python
def shed_control_call(control: dict, floor: float = 0.0) -> tuple | None:
    """(#880) The service call that sheds this control, or None.

    Pure so the dispatch can be tested without an instance. A setpoint load
    is turned DOWN to its floor; it used to fall through both the switch and
    the current branch and not be shed at all.
    """
    entity = (control or {}).get("entity")
    if not entity:
        return None
    kind = (control or {}).get("type")
    if kind == "switch":
        return ("switch", "turn_off", {"entity_id": entity})
    if kind in ("current", "setpoint"):
        return ("number", "set_value", {"entity_id": entity, "value": floor})
    return None


def restore_control_call(control: dict, previous: float | bool) -> tuple | None:
    """(#880) The service call that restores what shedding took."""
    entity = (control or {}).get("entity")
    if not entity:
        return None
    kind = (control or {}).get("type")
    if kind == "switch":
        return ("switch", "turn_on" if previous else "turn_off",
                {"entity_id": entity})
    if kind in ("current", "setpoint"):
        return ("number", "set_value",
                {"entity_id": entity, "value": float(previous)})
    return None
```

Then in the shed method at `:1474`, replace the `if control_type == "switch": … elif control_type == "current": …` chain with a single dispatch that records the pre-shed value first:

```python
            if control:
                entity = control.get("entity")
                kind = control.get("type")
                if entity:
                    # Record what shedding is about to take, so restore puts
                    # back what was there rather than a guess.
                    state = self.hass.states.get(entity)
                    if kind == "switch":
                        self._devices[device_id]["_pre_shed_was_on"] = (
                            state is not None
                            and state.state.lower() in ("on", "true", "1"))
                    else:
                        try:
                            self._devices[device_id]["_pre_shed_current"] = float(
                                state.state) if state else 0.0
                        except (ValueError, TypeError):
                            self._devices[device_id]["_pre_shed_current"] = float(
                                control.get("original_value", 0))
                    call = shed_control_call(control)
                    if call:
                        await self.hass.services.async_call(
                            *call[:2], call[2], blocking=True)
                        success = True
                        _LOGGER.debug("Shed device via %s %s", kind, entity)
```

- [ ] **Step 4: Wire the restore path too**

Task 4's test asserts `restore_control_call` works, and nothing calls it —
shedding without a matching restore leaves a load turned down forever. Find
the restore method (the one that reads `_pre_shed_was_on` /
`_pre_shed_current`) and replace its dispatch with:

```python
            previous = (self._devices[device_id].get("_pre_shed_was_on")
                        if control.get("type") == "switch"
                        else self._devices[device_id].get("_pre_shed_current", 0.0))
            call = restore_control_call(control, previous)
            if call:
                await self.hass.services.async_call(
                    *call[:2], call[2], blocking=True)
                success = True
                _LOGGER.debug("Restored device via %s %s → %s",
                              control.get("type"), call[2]["entity_id"], previous)
```

Locate it with:

```bash
grep -n "_pre_shed_was_on\|_pre_shed_current" features/load_management.py
```

- [ ] **Step 5: Run the tests**

```bash
cd /tmp/ha-880 && PYTHONPATH=/tmp/ha-880 python3.12 -m pytest \
  custom_components/solar_energy_management/tests/test_880_watt_modulating_load.py -q
```

Expected: `11 passed`

- [ ] **Step 6: Run the existing shed tests — this changed a live path**

```bash
cd /tmp/ha-880 && PYTHONPATH=/tmp/ha-880 python3.12 -m pytest \
  custom_components/solar_energy_management/tests/ -q -k "shed or load_management or peak" --tb=short
```

Expected: all pass. If a test asserts the old inline `switch.turn_off` call
shape, update the test to call `shed_control_call` — the behaviour is
identical for switches by construction.

- [ ] **Step 7: Commit**

```bash
git add features/load_management.py tests/test_880_watt_modulating_load.py
git commit -m "fix(#880): a modulating load sheds to its floor instead of not at all

Peak shedding dispatched on 'switch' and 'current'. A number-controlled
surplus load matched neither and was never shed — it ignored the peak guard
entirely. The dispatch is two pure functions now, so shed and restore cannot
drift apart.

Refs #880 #864"
```

---

## Task 5: The reporter's own cycle, end to end

**Files:**
- Modify: `tests/test_880_watt_modulating_load.py`

- [ ] **Step 1: Write the test**

```python
@pytest.mark.unit
class TestTheReportersCycle:
    """@jonasbkarlsson's AC-THOR 9s: min 0, max 9000, step 1, on a plain
    number helper. 'SEM correctly detects available/distributable PV
    surplus, but never writes a value to the configured Number control
    entity. The allocated surplus therefore remains at 0 W.'"""

    @pytest.mark.asyncio
    async def test_surplus_arrives_rises_falls_and_ends(self):
        hass = _hass(minimum=0.0, maximum=9000.0, step=1.0)
        d = _device(hass)

        assert await d.activate(1800.0) == 1800.0      # a cloud clears
        assert d.status.current_consumption_w == 1800.0

        assert await d.adjust_power(4200.0) == 4200.0  # midday
        assert await d.adjust_power(900.0) == 900.0    # cloud returns

        d._last_activated = None                        # bypass min-on clock
        await d.deactivate()                            # sun gone
        assert d.status.current_consumption_w == 0.0
        assert hass.services.async_call.await_args.args[2]["value"] == 0.0

    @pytest.mark.asyncio
    async def test_it_never_claims_the_nameplate(self):
        """The accounting half: a switch reports rated_power whatever it
        received, so the allocator's remaining-surplus sum was wrong even
        when the device did turn on."""
        hass = _hass()
        d = _device(hass)
        await d.activate(1800.0)
        assert d.status.current_consumption_w == 1800.0
        assert d.status.current_consumption_w != d.rated_power
```

- [ ] **Step 2: Run the whole file**

```bash
cd /tmp/ha-880 && PYTHONPATH=/tmp/ha-880 python3.12 -m pytest \
  custom_components/solar_energy_management/tests/test_880_watt_modulating_load.py -q
```

Expected: `13 passed`

- [ ] **Step 3: Prove the pins are not vacuous**

```bash
python3.12 ~/claude-jobs/vacuity-audit.py
```

Edit its `REVERTS` list first to revert, one at a time: `_write`'s
`number.set_value` call, `device_class_for_control`'s setpoint branch, and
`shed_control_call`'s `setpoint` case. Each must report `CAUGHT`.

- [ ] **Step 4: Commit**

```bash
git add tests/test_880_watt_modulating_load.py
git commit -m "test(#880): the reporter's own cycle, and the accounting half

Refs #880"
```

---

## Task 6: Documentation and the changelog

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `docs/KNOWN_LIMITATIONS.md`

- [ ] **Step 1: Add the changelog entry**

Under `# [Unreleased]`:

```markdown
- ✨ **A surplus load can now take a watt setpoint, not just on or off**
  (#880, reported by @jonasbkarlsson). SEM's allocator has always handed each
  device the watts it may have — `activate(available_watts)` is on the base
  contract and the controller calls it every cycle — but every
  implementation except the EV chargers' discarded the number: the entity
  was switched fully on and reported as drawing its nameplate. A my-PV
  AC-THOR, or any load driven by a `number` entity, now receives the
  allocation itself, follows it as the sun moves, is clamped to the entity's
  own min/max and step, and reports what it actually wrote. Two consequences
  beyond the obvious one: the allocator's remaining-surplus arithmetic was
  wrong whenever such a load was on, and peak shedding dispatched only on
  `switch` and `current` controls, so a modulating load was never shed at
  all — it now turns down to its floor.
```

- [ ] **Step 2: Remove the limitation it retires**

In `docs/KNOWN_LIMITATIONS.md`, find the paragraph stating that surplus
loads are on/off only and delete it. If no such paragraph exists, skip this
step rather than inventing one — check with:

```bash
grep -n -iE "on/off|modulat|setpoint" docs/KNOWN_LIMITATIONS.md
```

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md docs/KNOWN_LIMITATIONS.md
git commit -m "docs(#880): a surplus load can take a watt setpoint

Refs #880"
```

---

## Task 7: Full verification and the live check

- [ ] **Step 1: The full suite, on the merge result**

```bash
git fetch origin && git merge origin/develop --no-edit
rm -rf /tmp/sem880-ci && mkdir -p /tmp/sem880-ci/custom_components
rsync -a --exclude=.git --exclude=node_modules --exclude=.hypothesis \
  ./ /tmp/sem880-ci/custom_components/solar_energy_management/
cd /tmp/sem880-ci && PYTHONPATH=/tmp/sem880-ci python3.12 -m pytest \
  custom_components/solar_energy_management/tests/ -q --tb=short -rf \
  > /tmp/suite-880.log 2>&1; tail -3 /tmp/suite-880.log
```

Expected: `0 failed`. Never tail a running suite — write to the file and
read it at the end.

- [ ] **Step 2: Lint**

```bash
/tmp/venv-ci/bin/ruff check .
```

Expected: `All checks passed!`

- [ ] **Step 3: Live-verify on the rig**

The reference install has no modulating load, so it cannot demonstrate this
— that is the trap that stalled five issues this week (#1000). Use `.175`,
which has `number.mock_charger_2_current` and can carry a helper:

```bash
SRC=$(pwd) ~/bin/sem-deploy-175.sh
```

Then create an `input_number` helper on `.175` (0–3000 W, step 1), configure
it as a surplus device with SEM may control = Yes, and watch
`switch.sem_observer_mode`'s `would_decisions` for an `adjust` row naming
the watts. Observer mode is on, so nothing is written — the decision is the
evidence.

- [ ] **Step 4: A challenge record, because this asserts something structural**

`~/bin/sem-ready.sh` gate 2b refuses to pass without one. Write
`~/claude-jobs/challenge-feature-880-watt-modulating-load.md`:

```
CLAIM:   A surplus load driven by a number entity receives the watts SEM
         allocated to it, follows them while running, is bounded by the
         entity's own limits, and is shed to its floor under the peak guard.
AGENT:   ruflo-core:reviewer
VERDICT: <CONFIRMED | REFUTED | OVERSTATED> + what changed
```

Dispatch the reviewer directly with `Agent`, `subagent_type:
ruflo-core:reviewer`, and ask it to REFUTE — specifically: whether the
allocator's accounting is now correct when such a load is on, whether
`adjust_power` can fight the anti-flicker clocks, and whether any other
control domain falls through `device_class_for_control`.

- [ ] **Step 5: Open the PR and merge on approval**

This is an enhancement, so the pre-push hook needs the typed override and
the merge needs an explicit go-ahead:

```bash
git push -u origin feature/880-watt-modulating-load
gh pr create --base develop \
  --title "feat(#880): a surplus load can take a watt setpoint" \
  --body "SEM's allocator has always handed each device the watts it may have —
\`activate(available_watts)\` is on the base contract and the controller calls it
every cycle. Every implementation except the EV chargers' discarded the number:
the entity was switched fully on and reported as drawing its nameplate.

@jonasbkarlsson reproduced it on a plain Home Assistant number helper, so no
integration is implicated.

Three consequences, all fixed here: the load now receives the allocation and
follows it, clamped to the entity's own min/max/step; the allocator's
remaining-surplus arithmetic was wrong whenever such a load was on, because the
device claimed its nameplate; and peak shedding dispatched only on \`switch\` and
\`current\`, so a modulating load was never shed at all — it now turns down to
its floor.

Live-verified on .175 with an input_number helper, observer mode on."
```

---

## Out of scope, deliberately

- **Multiple modulating loads competing for one surplus.** The allocator
  already walks devices in priority order and passes each the remaining
  surplus; nothing here changes that, and a second AC-THOR would be handled
  by the same loop. If it proves wrong, that is its own issue.
- **A per-device minimum-change threshold.** `SetpointDevice` has
  `_min_power_change_interval`; this class does not, because the controller
  already rate-limits the pass and adding a second clock is the kind of
  machinery that earns its place only after a real complaint.
- **The config flow's wording.** The reporter picked "Control type: Number
  entity" and it did the wrong thing; now it does the right thing. Renaming
  the option is #996's territory.
