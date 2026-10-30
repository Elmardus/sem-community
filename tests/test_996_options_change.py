"""#996 — the house changes and the controls follow.

A capability key is not a reload key — plant size and investment are
sliders. A capability that appears while SEM runs — a plant size typed on
the card, a forecast integration installed — is growth: ABSENT → PRESENT
reloads once through the #923 guard, from the coordinator's cycle. PRESENT → ABSENT waits for the next
restart, on purpose."""
from __future__ import annotations

from custom_components.solar_energy_management import _SET_OPTION_STRUCTURAL_KEYS
from custom_components.solar_energy_management.coordinator.install_modules import (
    CAPABILITY_KEYS, Module, Presence, all_unknown, modules_grown,
)


def test_no_capability_key_reloads_on_every_tweak():
    # Plant size and investment are sliders; a reload per tweak is #462.
    # The per-cycle growth check reloads once, on ABSENT -> PRESENT only.
    assert not set(CAPABILITY_KEYS) & _SET_OPTION_STRUCTURAL_KEYS


def test_a_capability_that_appears_is_growth():
    at_setup = {**all_unknown(), Module.PV_SIZE: Presence.ABSENT}
    now = {**all_unknown(), Module.PV_SIZE: Presence.PRESENT}
    assert modules_grown(at_setup, now) == (Module.PV_SIZE,)


def test_a_capability_that_goes_waits_for_the_restart():
    at_setup = {**all_unknown(), Module.DYNAMIC_TARIFF: Presence.PRESENT}
    now = {**all_unknown(), Module.DYNAMIC_TARIFF: Presence.ABSENT}
    assert modules_grown(at_setup, now) == ()


def test_a_forecast_found_later_is_growth_not_a_flicker():
    at_setup = {**all_unknown(), Module.SOLAR_FORECAST: Presence.ABSENT}
    assert modules_grown(at_setup, {**all_unknown(), Module.SOLAR_FORECAST: Presence.UNKNOWN}) == ()
    assert modules_grown(at_setup, {**all_unknown(), Module.SOLAR_FORECAST: Presence.PRESENT}) == (
        Module.SOLAR_FORECAST,)
