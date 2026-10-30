"""#1024 — the EV card reads the session list over a websocket command.

No entity attribute carries the list (the recorder cap, #979): the card
calls ``solar_energy_management/session_history`` and gets rows, newest
first, filtered by charger and month when asked.
"""
from unittest.mock import MagicMock

from custom_components.solar_energy_management.const import DOMAIN
from custom_components.solar_energy_management.session_history import (
    WS_SESSION_HISTORY, async_register_websocket, select_sessions,
    ws_session_history,
)

HISTORY = [
    {"timestamp": "2026-08-30T18:00:00+02:00", "charger_id": "keba", "energy_kwh": 5.0},
    {"timestamp": "2026-09-02T18:00:00+02:00", "charger_id": "keba", "energy_kwh": 2.0},
    {"timestamp": "2026-09-05T18:00:00+02:00", "charger_id": "wall2", "energy_kwh": 7.0},
    {"timestamp": "2026-09-30T17:52:00+02:00", "charger_id": "keba", "energy_kwh": 2.1},
    {"timestamp": "2026-09-12T18:00:00+02:00", "energy_kwh": 1.0},   # an old record, no charger
]


class TestSelect:
    def test_newest_first_and_everything_by_default(self):
        rows = select_sessions(HISTORY)
        assert [r["energy_kwh"] for r in rows] == [2.1, 1.0, 7.0, 2.0, 5.0]

    def test_by_charger_keeps_the_old_records_that_name_none(self):
        rows = select_sessions(HISTORY, charger_id="keba")
        assert [r["energy_kwh"] for r in rows] == [2.1, 1.0, 2.0, 5.0]

    def test_another_chargers_rows_are_out(self):
        rows = select_sessions(HISTORY, charger_id="wall2")
        assert [r["energy_kwh"] for r in rows] == [1.0, 7.0]

    def test_by_month(self):
        rows = select_sessions(HISTORY, month="2026-09")
        assert [r["energy_kwh"] for r in rows] == [2.1, 1.0, 7.0, 2.0]

    def test_since_a_date(self):
        rows = select_sessions(HISTORY, since="2026-09-05")
        assert [r["energy_kwh"] for r in rows] == [2.1, 1.0, 7.0]

    def test_a_record_without_a_timestamp_sorts_last_and_is_kept(self):
        rows = select_sessions([{"energy_kwh": 3.0}, HISTORY[0]])
        assert [r["energy_kwh"] for r in rows] == [5.0, 3.0]


def _hass(history):
    hass = MagicMock()
    hass.data = {}
    coordinator = MagicMock()
    coordinator._storage.get_session_history.return_value = list(history)
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry.runtime_data = coordinator
    hass.config_entries.async_entries.return_value = [entry]
    return hass


class TestLegacyDoubles:
    def test_an_exact_double_is_shown_once(self):
        rec = {"timestamp": "2026-08-12T18:00:00+02:00", "energy_kwh": 5.2,
               "solar_share_pct": 40.0, "duration_min": 90.0, "taper_detected": False}
        twin = dict(rec, taper_detected=True)
        rows = select_sessions([rec, twin, HISTORY[1]])
        assert [r["energy_kwh"] for r in rows] == [2.0, 5.2]

    def test_two_chargers_at_the_same_second_are_two_sessions(self):
        a = {"timestamp": "2026-09-02T18:00:00+02:00", "charger_id": "keba", "energy_kwh": 2.0}
        b = dict(a, charger_id="wall2")
        assert len(select_sessions([a, b])) == 2

    def test_same_start_different_energy_is_kept(self):
        a = {"timestamp": "2026-09-02T18:00:00+02:00", "energy_kwh": 2.0}
        assert len(select_sessions([a, dict(a, energy_kwh=2.5)])) == 2


class TestCommand:
    def test_the_command_name(self):
        assert WS_SESSION_HISTORY == f"{DOMAIN}/session_history"

    def test_rows_come_back_on_the_message_id(self):
        hass = _hass(HISTORY)
        connection = MagicMock()
        ws_session_history(hass, connection, {"id": 7, "type": WS_SESSION_HISTORY,
                                              "charger_id": "keba", "month": "2026-09"})
        connection.send_result.assert_called_once()
        msg_id, payload = connection.send_result.call_args.args
        assert msg_id == 7
        assert [r["energy_kwh"] for r in payload["rows"]] == [2.1, 1.0, 2.0]

    def test_no_sem_entry_is_an_error_not_a_crash(self):
        hass = MagicMock()
        hass.data = {}
        hass.config_entries.async_entries.return_value = []
        connection = MagicMock()
        ws_session_history(hass, connection, {"id": 1, "type": WS_SESSION_HISTORY})
        connection.send_error.assert_called_once()
        connection.send_result.assert_not_called()

    def test_registration_lands_in_the_websocket_table(self):
        hass = MagicMock()
        hass.data = {}
        async_register_websocket(hass)
        assert WS_SESSION_HISTORY in hass.data["websocket_api"]
