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
