"""#996 — the coordinator feeds the two registry answers into the verdict.

A forecast integration and the inverter's export-limit entity are found in
the entity registry, not in SEM's options. The coordinator asks its own
readers and passes True / False / None. None — "not asked" — is UNKNOWN
and keeps every row (#925). Two things are "not asked":

* any read while Home Assistant is still starting: the integration may
  simply load after SEM (the restart race the review found);
* a miss the reader itself took before HA was running.

ABSENT for a runtime capability only ever comes from a read taken while
HA is running."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from homeassistant.core import CoreState

from custom_components.solar_energy_management.coordinator.coordinator import SEMCoordinator
from custom_components.solar_energy_management.coordinator.install_modules import (
    Module, Presence,
)

RUNNING = SimpleNamespace(state=CoreState.running)
STARTING = SimpleNamespace(state=CoreState.starting)


def _stub(installed=None, export_cache="unset", hass=RUNNING):
    """``installed`` is what the forecast reader's registry read answers:
    True (installed), False (asked while running, nothing), None (not asked).
    Its registry side is tested on a real registry in
    test_996_outage_is_not_absence."""
    reader = MagicMock()
    reader.installed_answer = lambda: reader.answer
    reader.answer = installed
    sensors = MagicMock()
    if export_cache == "unset":
        sensors.export_limit_answer = lambda *_: None
    else:
        sensors.export_limit_answer = lambda *_: export_cache is not None
    return SimpleNamespace(
        hass=hass, config={"solar_production_sensor": "sensor.pv"},
        _ed_raw_config=None, _ed_answered=True,
        _forecast_reader=reader, _sensor_reader=sensors,
    )


class TestTheVerdict:

    def test_no_answers_keep_both_unknown(self):
        p = SEMCoordinator.install_presence(_stub(installed=None))
        assert p[Module.SOLAR_FORECAST] is Presence.UNKNOWN
        assert p[Module.EXPORT_LIMIT] is Presence.UNKNOWN

    def test_found_answers_are_present(self):
        p = SEMCoordinator.install_presence(
            _stub(installed=True,
                  export_cache="number.inverter_export_limit"))
        assert p[Module.SOLAR_FORECAST] is Presence.PRESENT
        assert p[Module.EXPORT_LIMIT] is Presence.PRESENT

    def test_asked_while_running_and_nothing_is_absent(self):
        p = SEMCoordinator.install_presence(
            _stub(installed=False, export_cache=None))
        assert p[Module.SOLAR_FORECAST] is Presence.ABSENT
        assert p[Module.EXPORT_LIMIT] is Presence.ABSENT

    def test_a_miss_while_ha_is_starting_is_not_absent(self):
        # The readers answer None for a miss read before HA was running.
        p = SEMCoordinator.install_presence(
            _stub(installed=None, hass=STARTING))
        assert p[Module.SOLAR_FORECAST] is Presence.UNKNOWN
        assert p[Module.EXPORT_LIMIT] is Presence.UNKNOWN

    def test_a_hit_while_ha_is_starting_is_present(self):
        p = SEMCoordinator.install_presence(
            _stub(installed=True,
                  export_cache="number.inverter_export_limit", hass=STARTING))
        assert p[Module.SOLAR_FORECAST] is Presence.PRESENT
        assert p[Module.EXPORT_LIMIT] is Presence.PRESENT

    def test_a_miss_read_while_starting_stays_unknown_after_start(self):
        # Running now, but the reader's last miss was taken during start-up.
        p = SEMCoordinator.install_presence(
            _stub(installed=None))
        assert p[Module.SOLAR_FORECAST] is Presence.UNKNOWN

    def test_a_reader_that_raises_is_not_an_answer(self):
        stub = _stub(installed=False, export_cache=None)
        stub._forecast_reader.installed_answer = MagicMock(side_effect=RuntimeError("boom"))
        stub._sensor_reader.export_limit_answer = MagicMock(side_effect=RuntimeError("boom"))
        p = SEMCoordinator.install_presence(stub)
        assert p[Module.SOLAR_FORECAST] is Presence.UNKNOWN
        assert p[Module.EXPORT_LIMIT] is Presence.UNKNOWN

    def test_a_coordinator_without_hass_or_readers_is_unknown(self):
        stub = SimpleNamespace(config={}, _ed_raw_config=None, _ed_answered=True)
        p = SEMCoordinator.install_presence(stub)
        assert p[Module.SOLAR_FORECAST] is Presence.UNKNOWN
        assert p[Module.EXPORT_LIMIT] is Presence.UNKNOWN
        assert p[Module.DYNAMIC_TARIFF] is Presence.ABSENT


class _Store:
    """The two SEMStorage accessors, in memory."""

    def __init__(self, verdicts=None):
        self.verdicts = dict(verdicts or {})
        self.saves = 0

    def get_capability_verdicts(self):
        return dict(self.verdicts)

    def set_capability_verdict(self, name, value):
        if self.verdicts.get(name) is value:
            return False
        self.verdicts[name] = value
        return True

    async def async_save_energy_now(self):
        self.saves += 1


class TestTheStoredRunningVerdict:
    """A restart must not flip rows an earlier running read decided."""

    def _with_store(self, stub, store):
        stub._storage = store
        stub.hass = SimpleNamespace(state=stub.hass.state, async_create_task=MagicMock())
        return stub

    def test_starting_with_a_stored_absent_is_absent(self):
        stub = self._with_store(_stub(installed=None, hass=STARTING),
                                _Store({"solar_forecast": False, "export_limit": False}))
        p = SEMCoordinator.install_presence(stub)
        assert p[Module.SOLAR_FORECAST] is Presence.ABSENT
        assert p[Module.EXPORT_LIMIT] is Presence.ABSENT

    def test_starting_with_a_stored_present_is_present(self):
        stub = self._with_store(_stub(installed=None, hass=STARTING),
                                _Store({"solar_forecast": True}))
        p = SEMCoordinator.install_presence(stub)
        assert p[Module.SOLAR_FORECAST] is Presence.PRESENT
        assert p[Module.EXPORT_LIMIT] is Presence.UNKNOWN

    def test_nothing_stored_is_unknown(self):
        stub = self._with_store(_stub(installed=None, hass=STARTING), _Store())
        assert SEMCoordinator.install_presence(stub)[Module.SOLAR_FORECAST] is Presence.UNKNOWN

    def test_a_running_read_wins_and_is_stored_once(self):
        store = _Store({"solar_forecast": False})
        stub = self._with_store(
            _stub(installed=True, export_cache=None), store)
        p = SEMCoordinator.install_presence(stub)
        assert p[Module.SOLAR_FORECAST] is Presence.PRESENT
        # The hit is remembered at once; the export miss is not (yet).
        assert store.verdicts == {"solar_forecast": True}
        assert stub.hass.async_create_task.call_count == 1
        SEMCoordinator.install_presence(stub)          # unchanged: no second write
        assert stub.hass.async_create_task.call_count == 1

    def test_a_miss_while_starting_is_never_stored(self):
        store = _Store()
        stub = self._with_store(
            _stub(installed=None, hass=STARTING), store)
        SEMCoordinator.install_presence(stub)
        assert store.verdicts == {}

    def test_a_live_hit_before_running_outranks_a_stored_absent(self):
        # Installed while HA was down: the stored "no" must not win over
        # a scan that already finds Solcast.
        store = _Store({"solar_forecast": False})
        stub = self._with_store(
            _stub(installed=True, hass=STARTING), store)
        p = SEMCoordinator.install_presence(stub)
        assert p[Module.SOLAR_FORECAST] is Presence.PRESENT
        assert store.verdicts["solar_forecast"] is True


class TestAMissIsRememberedOnlyOnceItHolds:
    """The SOC-step confirm streak: a one-cycle forecast blip must not
    become "no forecast" at the next restart."""

    def _stub(self, store, clock):
        stub = _stub(installed=False, export_cache="unset")
        stub._storage = store
        stub.hass = SimpleNamespace(state=CoreState.running, async_create_task=MagicMock())
        return stub

    def _clock(self, monkeypatch):
        from custom_components.solar_energy_management.coordinator import coordinator as mod
        now = [1000.0]
        monkeypatch.setattr(mod, "_capability_clock", lambda: now[0])
        return now

    def test_one_missed_cycle_leaves_a_stored_present(self, monkeypatch):
        self._clock(monkeypatch)
        store = _Store({"solar_forecast": True})
        SEMCoordinator.install_presence(self._stub(store, None))
        assert store.verdicts["solar_forecast"] is True

    def test_a_held_miss_is_remembered(self, monkeypatch):
        now = self._clock(monkeypatch)
        store = _Store({"solar_forecast": True})
        stub = self._stub(store, now)
        for _ in range(6):
            SEMCoordinator.install_presence(stub)
            now[0] += 120.0
        assert store.verdicts["solar_forecast"] is False

    def test_many_reads_in_a_short_time_are_not_enough(self, monkeypatch):
        now = self._clock(monkeypatch)
        store = _Store({"solar_forecast": True})
        stub = self._stub(store, now)
        for _ in range(30):                      # 30 reads in 5 minutes
            SEMCoordinator.install_presence(stub)
            now[0] += 10.0
        assert store.verdicts["solar_forecast"] is True

    def test_a_hit_breaks_the_streak(self, monkeypatch):
        now = self._clock(monkeypatch)
        store = _Store({"solar_forecast": True})
        stub = self._stub(store, now)
        for _ in range(5):
            SEMCoordinator.install_presence(stub)
            now[0] += 150.0
        stub._forecast_reader.answer = True           # it came back
        SEMCoordinator.install_presence(stub)
        stub._forecast_reader.answer = False
        SEMCoordinator.install_presence(stub)          # a new streak starts at 1
        now[0] += 150.0
        assert store.verdicts["solar_forecast"] is True
