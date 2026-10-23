"""#1084 stage E — the Why? card reads the decision log over a websocket.

The card needs every kept row, not only the newest per device that rides a
sensor: ``solar_energy_management/decision_log`` hands it the ring buffer,
newest first. Read-only; any user may ask.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from custom_components.solar_energy_management.const import DOMAIN
from custom_components.solar_energy_management.coordinator.decision_log import (
    DECISION_LOG_SIZE, KIND_BATTERY, KIND_CHARGER, KIND_LOAD, DecisionLog,
)
from custom_components.solar_energy_management.decision_ws import (
    WS_DECISION_LOG, async_register_websocket, ws_decision_log,
)

T0 = datetime(2026, 10, 23, 14, 10, tzinfo=timezone.utc)
RECORD_KEYS = {"time", "device_id", "device_kind", "name", "old", "new",
               "reason_code", "text", "inputs", "observer"}


def _log():
    log = DecisionLog(None)
    log.set_inputs({"solar_w": 1400, "surplus_w": 1400, "export_rate": 0.06})
    log.record(device_id="pool", kind=KIND_LOAD, verb="on", name="Pool pump",
               reason_code="start", text="1.4 kW of sun to spare.",
               observer=True, now=T0, quiet_first_rest=False)
    log.set_inputs({"solar_w": 3100, "soc": 74, "import_price": 0.27})
    log.record(device_id="bat", kind=KIND_BATTERY, verb="block_discharge",
               reason_code="negative_export", text="Selling earns less than nothing.",
               now=T0 + timedelta(hours=3))
    log.set_inputs({"solar_w": 0, "soc": 72})
    log.record(device_id="box", kind=KIND_CHARGER, verb="charge", value=8,
               reason_code="solar_charge", text="Sun to spare.",
               now=T0 + timedelta(hours=5))
    return log


def _hass(log):
    hass = MagicMock()
    hass.data = {}
    coordinator = MagicMock()
    coordinator.decision_log = log
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry.runtime_data = coordinator
    hass.config_entries.async_entries.return_value = [entry]
    return hass


def _call(hass, **extra):
    connection = MagicMock()
    ws_decision_log(hass, connection, {"id": 9, "type": WS_DECISION_LOG, **extra})
    return connection


class TestDecisionLogCommand:
    def test_the_command_name(self):
        assert WS_DECISION_LOG == f"{DOMAIN}/decision_log"

    def test_records_come_back_newest_first(self):
        conn = _call(_hass(_log()))
        conn.send_result.assert_called_once()
        msg_id, payload = conn.send_result.call_args.args
        assert msg_id == 9
        assert [r["device_id"] for r in payload["records"]] == ["box", "bat", "pool"]
        assert payload["size"] == DECISION_LOG_SIZE

    def test_an_empty_log_is_an_empty_list(self):
        conn = _call(_hass(DecisionLog(None)))
        _, payload = conn.send_result.call_args.args
        assert payload["records"] == []
        conn.send_error.assert_not_called()

    def test_the_observer_flag_is_carried(self):
        _, payload = _call(_hass(_log())).send_result.call_args.args
        by_id = {r["device_id"]: r for r in payload["records"]}
        assert by_id["pool"]["observer"] is True
        assert by_id["box"]["observer"] is False

    def test_record_shape_plain_text_and_inputs(self):
        _, payload = _call(_hass(_log())).send_result.call_args.args
        for row in payload["records"]:
            assert set(row) == RECORD_KEYS
        box = payload["records"][0]
        assert box["new"] == "charge 8 A"
        assert box["text"] == "Sun to spare."
        assert box["inputs"] == {"solar_w": 0.0, "soc": 72.0}
        assert box["time"] == (T0 + timedelta(hours=5)).isoformat()
        pool = payload["records"][-1]
        assert pool["name"] == "Pool pump"
        assert pool["inputs"]["solar_w"] == 1400.0

    def test_the_answer_is_a_copy_not_the_live_buffer(self):
        log = _log()
        _, payload = _call(_hass(log)).send_result.call_args.args
        payload["records"].clear()
        assert len(log) == 3

    def test_the_entry_id_picks_the_entry(self):
        hass = _hass(_log())
        conn = _call(hass, entry_id="other")
        conn.send_error.assert_called_once()
        conn.send_result.assert_not_called()

    def test_no_sem_entry_is_an_error_not_a_crash(self):
        hass = MagicMock()
        hass.data = {}
        hass.config_entries.async_entries.return_value = []
        conn = _call(hass)
        conn.send_error.assert_called_once()
        conn.send_result.assert_not_called()

    def test_a_coordinator_without_a_log_is_an_error(self):
        conn = _call(_hass(None))
        conn.send_error.assert_called_once()

    def test_any_user_may_read_it(self):
        # no admin wrapper (it would raise Unauthorized): the rows are the
        # logbook's rows
        conn = MagicMock()
        conn.user.is_admin = False
        ws_decision_log(_hass(_log()), conn, {"id": 3, "type": WS_DECISION_LOG})
        conn.send_result.assert_called_once()

    def test_registration_lands_in_the_websocket_table(self):
        hass = MagicMock()
        hass.data = {}
        async_register_websocket(hass)
        assert WS_DECISION_LOG in hass.data["websocket_api"]


class TestSolarInput:
    def test_the_sun_is_kept_as_an_input(self):
        log = DecisionLog(None)
        log.set_inputs({"solar_w": 2345.67, "nonsense": 1})
        rec = log.record(device_id="box", kind=KIND_CHARGER, verb="charge",
                         value=6, now=T0)
        assert rec.inputs == {"solar_w": 2345.7}


# ─────────────── each device's own name rides the record ───────────────

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from custom_components.solar_energy_management.coordinator.actuate import actuate  # noqa: E402
from custom_components.solar_energy_management.coordinator.charger_reconciler import (  # noqa: E402
    ChargerReconciler,
)
from custom_components.solar_energy_management.coordinator.charger_types import (  # noqa: E402
    ChargerDecision, ChargerIntent, ChargerPower,
)
from custom_components.solar_energy_management.coordinator.coordinator import (  # noqa: E402
    SEMCoordinator,
)
from custom_components.solar_energy_management.coordinator.surplus_controller import (  # noqa: E402
    SurplusController,
)


def _charger(name):
    dev = MagicMock()
    dev.name = name
    dev._current_setpoint = 0
    dev.observer_mode = False
    dev.contactor_surface = False
    dev.can_stop_charging = MagicMock(return_value=True)
    a = MagicMock()
    a._device = dev
    a.phases, a.voltage, a.max_current_a = 3, 230.0, 32
    a.actual_charging = MagicMock(return_value=True)
    a.is_self_charging = MagicMock(return_value=False)
    a.enable_state = MagicMock(return_value=(None, True))

    async def _write(amps):
        dev._current_setpoint = amps
    a.command_current = AsyncMock(side_effect=_write)
    for m in ("command_disable", "command_park_off", "arm_failsafe",
              "ensure_enabled"):
        setattr(a, m, AsyncMock())
    return a


async def _charge(sc, cid, adapter, amps=10):
    rec = ChargerReconciler(charger_id=cid, heartbeat_s=3600.0)
    dec = ChargerDecision(charger_id=cid, mode="solar_only",
                          intent=ChargerIntent.CHARGE_AT_AMPS,
                          commanded_amps=amps, reason="sun to spare")
    power = ChargerPower(charger_id=cid, power_w=6900.0, connected=True,
                         charging=True)
    await actuate(dec, adapter, power, rec, controller=sc)


class TestNames:
    async def test_two_chargers_carry_their_own_names(self):
        sc = SurplusController(MagicMock())
        sc.hass.bus.async_fire = MagicMock()
        await _charge(sc, "c1", _charger("Garage"))
        await _charge(sc, "c2", _charger("Carport"), amps=8)
        rows = sc.decision_log.records()
        assert {r["device_id"]: r["name"] for r in rows} == {
            "c1": "Garage", "c2": "Carport"}

    async def test_a_charger_without_a_name_keeps_its_id(self):
        sc = SurplusController(MagicMock())
        sc.hass.bus.async_fire = MagicMock()
        ad = _charger(None)
        await _charge(sc, "c1", ad)
        assert sc.decision_log.records()[0]["name"] == "c1"

    def test_stamped_names_fill_a_seam_that_knows_only_an_id(self):
        log = DecisionLog(None)
        log.set_names({"battery:b1": "Garage battery", "battery:b2": ""})
        r1 = log.record(device_id="b1", kind=KIND_BATTERY, verb="force_charge",
                        value=2000, now=T0)
        r2 = log.record(device_id="b2", kind=KIND_BATTERY, verb="force_charge",
                        value=2000, now=T0)
        r3 = log.record(device_id="b1", kind=KIND_LOAD, verb="on", now=T0,
                        name="Pool pump", quiet_first_rest=False)
        assert (r1.name, r2.name, r3.name) == ("Garage battery", "b2", "Pool pump")

    def test_battery_names_come_from_their_devices(self, monkeypatch):
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
        ents = {"sensor.p1": SimpleNamespace(device_id="d1"),
                "sensor.p2": SimpleNamespace(device_id="d2"),
                "sensor.p3": SimpleNamespace(device_id="d2"),
                "sensor.p4": None}
        devs = {"d1": SimpleNamespace(name="Pack A", name_by_user="Cellar"),
                "d2": SimpleNamespace(name="Inverter", name_by_user=None)}
        monkeypatch.setattr(er, "async_get", lambda hass: SimpleNamespace(
            async_get=lambda eid: ents.get(eid)))
        monkeypatch.setattr(dr, "async_get", lambda hass: SimpleNamespace(
            async_get=lambda did: devs.get(did)))
        coord = SimpleNamespace(hass=MagicMock())
        power = SimpleNamespace(batteries={
            f"b{i}": SimpleNamespace(name=f"sensor.p{i}") for i in range(1, 5)})
        names = SEMCoordinator._battery_display_names(coord, power)
        assert names == {"battery:b1": "Cellar", "battery:b2": "Inverter b2",
                         "battery:b3": "Inverter b3"}

    def test_no_breakdown_no_names(self):
        coord = SimpleNamespace(hass=MagicMock())
        assert SEMCoordinator._battery_display_names(
            coord, SimpleNamespace(batteries={})) == {}
