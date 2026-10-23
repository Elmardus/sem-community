"""#1174 — every decision says why in a plain sentence.

Each decision family sets a stable reason code where it decides
(``coordinator/reason_codes.py``). The decision log carries the code and
its few numbers; the Why? card and the logbook turn it into a sentence from
ONE catalogue, the ``why_reason_<code>`` keys of
``dashboard/translations.json``.

Pinned here:
- coverage both ways: every code has a sentence in all 16 languages, and
  every sentence belongs to a code; each sentence uses exactly the
  placeholders its code names;
- the code is set at the source: no decision constructor in the deciders
  goes without one, and no ``reason_code=`` is a bare string;
- a code changes no decision: it is left out of equality;
- the record carries code and numbers, and the logbook reads the sentence
  in the user's language, falling back to the internal text.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from custom_components.solar_energy_management.coordinator import (
    reason_codes as RC,
)
from custom_components.solar_energy_management.coordinator.charger_types import (
    BatteryDecision,
    BatteryIntent,
    BatteryRuntime,
    BatteryView,
    ChargerDecision,
    ChargerEnergy,
    ChargerIntent,
    ChargerPower,
    ChargerView,
    FleetContext,
)
from custom_components.solar_energy_management.coordinator.decide import decide
from custom_components.solar_energy_management.coordinator.decide_battery import (
    decide_battery,
)
from custom_components.solar_energy_management.coordinator.decision_log import (
    DecisionLog,
    record_battery,
    record_charger,
)
from custom_components.solar_energy_management.coordinator.surplus_controller import (
    LoadIntent,
)
from custom_components.solar_energy_management.logbook import describe_decision
from custom_components.solar_energy_management.plain_reasons import (
    fill,
    load_plain_reasons,
    plain_sentence,
)

ROOT = Path(__file__).resolve().parent.parent
TRANSLATIONS = ROOT / "dashboard" / "translations.json"
LANGS = 16
_PH = re.compile(r"\{(\w+)\}")


@pytest.fixture(scope="module")
def catalogue():
    data = json.loads(TRANSLATIONS.read_text(encoding="utf-8"))
    assert len(data) == LANGS
    return data


# ── coverage ─────────────────────────────────────────────────────────

class TestEveryCodeHasASentence:

    def test_the_code_list_is_not_empty_and_unique(self):
        names = [n for n in dir(RC)
                 if n.isupper()
                 and n.split("_", 1)[0] in ("CHARGER", "BATTERY", "LOAD", "EXPORT")
                 and isinstance(getattr(RC, n), str)]
        values = [getattr(RC, n) for n in names]
        assert len(values) == len(set(values)), "two names share one code"
        assert set(values) == set(RC.ALL_CODES)
        assert len(RC.ALL_CODES) >= 80

    def test_every_code_has_a_sentence_in_every_language(self, catalogue):
        for lang, table in catalogue.items():
            missing = sorted(c for c in RC.ALL_CODES
                             if not table.get(RC.catalogue_key(c), "").strip())
            assert not missing, f"{lang}: no sentence for {missing}"

    def test_every_sentence_has_a_code(self, catalogue):
        for lang, table in catalogue.items():
            stray = sorted(k for k in table if k.startswith(RC.KEY_PREFIX)
                           and k[len(RC.KEY_PREFIX):] not in RC.ALL_CODES)
            assert not stray, f"{lang}: sentence without a code: {stray}"

    def test_each_sentence_uses_exactly_its_placeholders(self, catalogue):
        for lang, table in catalogue.items():
            for code in RC.ALL_CODES:
                got = set(_PH.findall(table[RC.catalogue_key(code)]))
                assert got == set(RC.PARAMS.get(code, ())), (lang, code, got)
        assert set(RC.PARAMS) <= set(RC.ALL_CODES)

    def test_english_sentences_are_short_and_plain(self, catalogue):
        for code in RC.ALL_CODES:
            s = catalogue["en"][RC.catalogue_key(code)]
            for sentence in re.split(r"(?<=\.)\s+", s):
                assert len(sentence.split()) < 20, (code, sentence)
            assert not re.search(r"\bSoC\b|\bSOC\b|→|_", s), (code, s)

    def test_translations_differ_from_english(self, catalogue):
        """A copied English sentence is not a translation."""
        en = catalogue["en"]
        for lang, table in catalogue.items():
            if lang == "en":
                continue
            same = [c for c in RC.ALL_CODES
                    if table[RC.catalogue_key(c)] == en[RC.catalogue_key(c)]]
            assert not same, f"{lang}: English left in {same}"


# ── set at the source ────────────────────────────────────────────────

_PKG = ROOT
_DECIDERS = {
    "coordinator/decide.py": "ChargerDecision",
    "coordinator/decide_battery.py": "BatteryDecision",
    "coordinator/ev_control.py": "ChargerDecision",
}


def _calls(path: str, name: str):
    tree = ast.parse((_PKG / path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if getattr(fn, "id", None) == name or getattr(fn, "attr", None) == name:
                yield node


class TestTheCodeIsSetWhereTheDecisionIsMade:

    @pytest.mark.parametrize("path,cls", sorted(_DECIDERS.items()))
    def test_every_decision_carries_a_code(self, path, cls):
        calls = list(_calls(path, cls))
        assert calls, f"{path}: no {cls}( found — the scan is blind"
        missing = [c.lineno for c in calls
                   if "reason_code" not in {k.arg for k in c.keywords}]
        assert not missing, f"{path}: {cls} without reason_code at {missing}"

    def test_every_load_stop_intent_carries_a_code(self):
        bare = []
        for c in _calls("coordinator/surplus_controller.py", "LoadIntent"):
            first = c.args[0] if c.args else None
            if isinstance(first, ast.Constant) and first.value is False:
                if len(c.args) < 5 and "code" not in {k.arg for k in c.keywords}:
                    bare.append(c.lineno)
        assert not bare, f"LoadIntent(False, …) without a code at {bare}"

    def test_no_reason_code_is_a_bare_string_and_each_names_a_real_code(self):
        bad = []
        paths = [*_PKG.glob("*.py")]
        for sub in ("coordinator", "features", "devices", "analytics", "utils"):
            paths += _PKG.glob(f"{sub}/**/*.py")
        assert len(paths) > 50
        for path in sorted(paths):
            rel = path.relative_to(_PKG).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if kw.arg != "reason_code":
                        continue
                    # a string a code is chosen BY (mode == "off") is fine
                    # …and so is an attribute name read with getattr()
                    compared = {id(c) for cmp in ast.walk(kw.value)
                                if isinstance(cmp, ast.Compare) or (
                                    isinstance(cmp, ast.Call)
                                    and getattr(cmp.func, "id", "") == "getattr")
                                for c in ast.walk(cmp)}
                    for sub in ast.walk(kw.value):
                        if id(sub) in compared:
                            continue
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                                and sub.value:
                            bad.append(f"{rel}:{node.lineno} literal {sub.value!r}")
                        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                                and sub.value.id in ("RC", "reason_codes") \
                                and not hasattr(RC, sub.attr):
                            bad.append(f"{rel}:{node.lineno} unknown {sub.attr}")
        assert not bad, bad


# ── a code changes nothing ───────────────────────────────────────────

class TestACodeChangesNoDecision:

    def test_battery_equality_ignores_the_code(self):
        a = BatteryDecision(battery_id="b", intent=BatteryIntent.NORMAL, reason="x")
        b = BatteryDecision(battery_id="b", intent=BatteryIntent.NORMAL, reason="x",
                            reason_code=RC.BATTERY_NORMAL, reason_params={"floor": 1})
        assert a == b and hash(a) == hash(b)

    def test_charger_equality_ignores_the_code(self):
        a = ChargerDecision(charger_id="c", mode="solar_only", intent=ChargerIntent.IDLE)
        b = ChargerDecision(charger_id="c", mode="solar_only", intent=ChargerIntent.IDLE,
                            reason_code=RC.CHARGER_LOW_SUN)
        assert a == b

    def test_load_intent_equality_ignores_the_code(self):
        assert LoadIntent(False, 0.0, None, "r") == LoadIntent(
            False, 0.0, None, "r", RC.LOAD_NO_SOURCE)


# ── the example from the issue, end to end ───────────────────────────

def _battery_view(soc=21.0, floor=80.3):
    return BatteryView(
        runtime=BatteryRuntime(battery_id="b1", last_known_soc=soc),
        config={"battery_max_discharge_power": 5000, "battery_mode": "auto",
                "battery_reserve_soc": 10},
        fleet=FleetContext(solar_w=0.0, home_w=2667.0, battery_soc=soc,
                           buffer_soc=20.0, battery_assist_min_surplus_w=1200.0),
        charging_state="night_charging_active",
        ev_charging=False, ev_connected=True,
        home_consumption_w=2667.0,
        dynamic_floor_pct=floor,
    )


class TestTheIssueExample:

    def test_the_battery_below_tonights_floor(self, catalogue):
        d = decide_battery(_battery_view())
        assert d.intent is BatteryIntent.LIMIT_DISCHARGE
        assert "tonight's floor 80.3%" in d.reason          # internal text stays
        assert d.reason_code == RC.BATTERY_CAR_BELOW_FLOOR
        assert d.reason_params == {"floor": 80}

        log = DecisionLog()
        rec = record_battery(log, d, value_w=d.discharge_limit_w)
        row = rec.as_dict()
        assert row["reason_code"] == RC.BATTERY_CAR_BELOW_FLOOR
        assert row["params"] == {"floor": 80}
        assert row["text"].startswith("ev plugged in + battery SoC")

        cat = load_plain_reasons(str(TRANSLATIONS))
        assert plain_sentence(cat, row["reason_code"], row["params"], "en") == (
            "A car is plugged in. The battery is below the 80% tonight needs, "
            "so it powers only the house.")
        de = plain_sentence(cat, row["reason_code"], row["params"], "de")
        assert "80%" in de and "Auto" in de

    def test_a_low_pack_below_its_reserve(self):
        d = decide_battery(_battery_view(soc=15.0, floor=None))
        assert d.reason_code == RC.BATTERY_CAR_BELOW_RESERVE
        assert d.reason_params == {}

    def test_no_car_is_a_normal_day(self):
        v = _battery_view()
        v = BatteryView(**{**v.__dict__, "ev_connected": False})
        assert decide_battery(v).reason_code == RC.BATTERY_NORMAL


def _charger_view(*, connected=True, solar_w=0.0, mode="solar_only"):
    return ChargerView(
        power=ChargerPower(charger_id="c1", power_w=0.0,
                           connected=connected, charging=False),
        energy=ChargerEnergy(charger_id="c1"),
        mode=mode,
        config={"ev_min_current": 6, "ev_phases": 3, "ev_voltage": 230,
                "ev_max_current": 16},
        fleet=FleetContext(solar_w=solar_w, home_w=300.0, battery_soc=95.0),
    )


class TestChargerCodes:

    def test_no_car(self):
        assert decide(_charger_view(connected=False)).reason_code == RC.CHARGER_NO_CAR

    def test_low_sun_and_sun(self):
        assert decide(_charger_view(solar_w=0.0)).reason_code == RC.CHARGER_LOW_SUN
        d = decide(_charger_view(solar_w=9000.0))
        assert d.intent is ChargerIntent.CHARGE_AT_AMPS
        assert d.reason_code == RC.CHARGER_SUN

    def test_off_and_max(self):
        assert decide(_charger_view(mode="off")).reason_code == RC.CHARGER_OFF
        assert decide(_charger_view(mode="always_max")).reason_code == RC.CHARGER_MAX

    def test_the_charger_record_carries_the_code(self):
        d = decide(_charger_view(solar_w=9000.0))
        rec = record_charger(DecisionLog(), d, verb="charge",
                             value=float(d.commanded_amps))
        assert rec.reason_code == RC.CHARGER_SUN

    def test_a_decision_without_a_code_keeps_the_old_composed_one(self):
        d = ChargerDecision(charger_id="c", mode="solar_only",
                            intent=ChargerIntent.CHARGE_AT_AMPS, commanded_amps=8)
        rec = record_charger(DecisionLog(), d, verb="charge", value=8.0)
        assert rec.reason_code == "solar_only_charge_at_amps"


# ── the logbook ──────────────────────────────────────────────────────

class TestTheLogbook:

    ROW = {"name": "Battery", "old": "normal", "new": "limit_discharge 2667 W",
           "reason_code": RC.BATTERY_CAR_BELOW_FLOOR, "params": {"floor": 80},
           "text": "ev plugged in + battery SoC 21% < tonight's floor 80.3%"}

    def test_the_sentence_in_the_users_language(self):
        cat = load_plain_reasons(str(TRANSLATIONS))
        msg = describe_decision(dict(self.ROW), cat, "de")["message"]
        assert msg.startswith("normal → limit_discharge 2667 W — Ein Auto")
        assert "SoC" not in msg

    def test_a_regional_language_falls_back_to_its_base(self):
        cat = load_plain_reasons(str(TRANSLATIONS))
        assert "Ein Auto" in describe_decision(dict(self.ROW), cat, "de-CH")["message"]

    def test_no_catalogue_or_no_sentence_reads_the_internal_text(self):
        assert "tonight's floor" in describe_decision(dict(self.ROW))["message"]
        cat = load_plain_reasons(str(TRANSLATIONS))
        row = {**self.ROW, "reason_code": "solar_only_idle"}
        assert "tonight's floor" in describe_decision(row, cat, "en")["message"]

    def test_a_missing_number_reads_the_internal_text(self):
        cat = load_plain_reasons(str(TRANSLATIONS))
        row = {**self.ROW, "params": {}}
        assert "tonight's floor" in describe_decision(row, cat, "en")["message"]

    def test_fill(self):
        assert fill("at {time}", {"time": "06:30"}) == "at 06:30"
        assert fill("{floor}%", {"floor": 80.0}) == "80%"
        assert fill("{floor}%", {}) is None

    def test_an_unreadable_catalogue_is_empty(self, tmp_path):
        assert load_plain_reasons(str(tmp_path / "nope.json")) == {}


# ── a rewrite names its own why ──────────────────────────────────────

#: The fields that change what a device does. A decision rewritten in one
#: of these after it was made must say why it was rewritten.
_DOES = {"intent", "commanded_amps", "mode", "charge_power_w",
         "discharge_limit_w", "discharge_power_w", "target_soc", "floor_soc",
         "safety_stop", "capped_by_limit", "budget_w", "bridgeable",
         "peak_paused", "room_capped"}


def _is_dataclass_replace(node) -> bool:
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id in ("replace", "_dc_replace")
    return (isinstance(fn, ast.Attribute) and fn.attr == "replace"
            and isinstance(fn.value, ast.Name) and fn.value.id == "dataclasses")


def _rewrite_body_lines():
    """The one ``replace`` allowed to forward ``**changes``: rewrite's own."""
    src = (_PKG / "coordinator" / "charger_types.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "rewrite":
            return set(range(node.lineno, node.end_lineno + 1))
    raise AssertionError("charger_types.rewrite not found")


def _source_files():
    paths = [*_PKG.glob("*.py")]
    for sub in ("coordinator", "features", "devices"):
        paths += _PKG.glob(f"{sub}/**/*.py")
    return sorted(paths)


class TestARewriteNamesItsWhy:
    """Review of the first cut: ``replace(decision, intent=IDLE, …)`` kept
    the inner code, so the card could say "The car charges with spare sun"
    over a stop. Every such rewrite goes through ``charger_types.rewrite``,
    which takes the code as a required argument."""

    def test_no_bare_replace_changes_what_a_device_does(self):
        bad = []
        allowed = _rewrite_body_lines()
        for path in _source_files():
            rel = path.relative_to(_PKG).as_posix()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call) or not _is_dataclass_replace(node):
                    continue
                kws = {k.arg for k in node.keywords}
                if rel == "coordinator/charger_types.py" and node.lineno in allowed:
                    continue
                # **kwargs or a second positional hides what changes
                if None in kws or len(node.args) > 1:
                    bad.append(f"{rel}:{node.lineno} hidden fields")
                elif kws & _DOES and "reason_code" not in kws:
                    bad.append(f"{rel}:{node.lineno} {sorted(kws & _DOES)}")
        assert not bad, ("use charger_types.rewrite(..., reason_code=…): "
                         + ", ".join(bad))

    def test_every_rewrite_names_a_code(self):
        calls = 0
        for path in _source_files():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "rewrite":
                    calls += 1
                    assert "reason_code" in {k.arg for k in node.keywords}, (
                        path.name, node.lineno)
        assert calls >= 20, "the scan found too few rewrites — is it blind?"

    def test_rewrite_needs_the_code(self):
        from custom_components.solar_energy_management.coordinator.charger_types import (
            rewrite,
        )
        d = ChargerDecision(charger_id="c", mode="solar_only",
                            intent=ChargerIntent.CHARGE_AT_AMPS, commanded_amps=10,
                            reason_code=RC.CHARGER_SUN, reason_params={"x": 1})
        with pytest.raises(TypeError):
            rewrite(d, intent=ChargerIntent.IDLE)
        r = rewrite(d, intent=ChargerIntent.IDLE, commanded_amps=0,
                    reason_code=RC.CHARGER_DIP_STOP)
        assert (r.intent, r.reason_code, r.reason_params) == (
            ChargerIntent.IDLE, RC.CHARGER_DIP_STOP, {"x": 1})

    def test_the_stability_ramp_says_ramp_not_sun(self):
        from custom_components.solar_energy_management.coordinator.charge_stability import (
            ChargeStability,
        )
        cs = ChargeStability()
        d = ChargerDecision(charger_id="c", mode="solar_only",
                            intent=ChargerIntent.CHARGE_AT_AMPS, commanded_amps=16,
                            reason_code=RC.CHARGER_SUN)
        cs._last_amps["c"] = 8
        out = cs._adjust(d, "c", 16, 1000.0, min_change_amps=1,
                         min_change_interval_s=0.0, ramp_amps=2)
        assert out.commanded_amps == 10
        assert out.reason_code == RC.CHARGER_RAMP


class TestBatteryNormalSaysWhatItDoes:

    def test_the_fall_through_is_no_limit(self, catalogue):
        """NORMAL writes the pack's own maximum discharge: SEM sets no limit."""
        assert "no limit" in catalogue["en"][RC.catalogue_key(RC.BATTERY_NORMAL)]


class TestALimitKeepsItsOwnCode:

    def test_every_capped_by_limit_names_the_limit(self):
        """The stability clamp keeps the code of the site that set
        ``capped_by_limit``; so each such site must name its limit."""
        peak = {"CHARGER_PEAK_PAUSE", "CHARGER_PEAK_SLOW", "CHARGER_PEAK_STOP",
                "CHARGER_PHASE_SLOW", "CHARGER_PHASE_STOP"}
        sites = 0
        for path in _source_files():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                cap = kw.get("capped_by_limit")
                if not (isinstance(cap, ast.Constant) and cap.value is True):
                    continue
                sites += 1
                code = kw.get("reason_code")
                assert isinstance(code, ast.Attribute) and code.attr in peak, (
                    path.name, node.lineno)
        assert sites >= 3

    def test_rewrite_keeps_the_numbers_unless_given(self):
        from custom_components.solar_energy_management.coordinator.charger_types import (
            rewrite,
        )
        d = ChargerDecision(charger_id="c", mode="m", intent=ChargerIntent.IDLE,
                            reason_code=RC.CHARGER_PLAN_WAIT_UNTIL,
                            reason_params={"time": "02:00"})
        assert rewrite(d, bridgeable=False, reason_code=d.reason_code
                       ).reason_params == {"time": "02:00"}
        assert rewrite(d, bridgeable=False, reason_code=RC.CHARGER_STOP_NOW,
                       reason_params={}).reason_params == {}


# ── the grid export limit (#1090) ────────────────────────────────────

class _ExportAdapter:
    def __init__(self):
        self.cuts, self.releases = [], 0

    async def command_limit_export(self, watts):
        self.cuts.append(watts)

    async def command_release_export(self):
        self.releases += 1

    def export_dry_run(self, intent, watts):
        return {"service": "x.limit", "data": {"w": watts}, "why": None}


def _dutch_day_cut(threshold=0.05, rate=0.012):
    """A Dutch day: a sold kWh is worth 1.2 ct, the user's limit is 5 ct."""
    from datetime import datetime, timezone
    from custom_components.solar_energy_management.coordinator.decide_export import (
        decide_export,
    )
    from custom_components.solar_energy_management.coordinator.export_guard import (
        ExportGuard,
    )
    from custom_components.solar_energy_management.coordinator.sink_verdicts import (
        sink_verdicts,
    )
    v = sink_verdicts(
        now=datetime(2027, 6, 1, 13, tzinfo=timezone.utc), tariff_level="normal",
        upcoming=[], export_rate=rate, export_rate_known=True,
        export_guard_enabled=True, house_sink_enabled=False,
        morning_window_enabled=False, departure=None, morning_hours=2.0,
        forecast_refills_pack=False, pacing_horizon_end=None,
        export_threshold=threshold)["grid_export"]
    guard = ExportGuard(engage_hold_s=0.0, release_hold_s=0.0)

    def tick(now, state, export_w):
        cmd = guard.update(now, state, export_w, reason_code=v.reason_code,
                           reason_params=v.reason_params)
        fleet = type("F", (), {"export_guard_enabled": True, "export_command": cmd})()
        return decide_export(fleet)
    return v, tick


class TestTheExportLimitIsLogged:

    def _ctl(self):
        return type("C", (), {"decision_log": DecisionLog(),
                              "publish_observer_decision": lambda *a, **k: None})()

    def test_a_dutch_day_stop_records_once_with_its_sentence(self):
        import asyncio
        from custom_components.solar_energy_management.coordinator.actuate_export import (
            actuate_export,
        )
        v, tick = _dutch_day_cut()
        assert v.state == "closed" and v.reason_code == RC.EXPORT_NOT_WORTH
        ctl, adapter = self._ctl(), _ExportAdapter()
        cut = tick(0.0, v.state, 2400.0)
        asyncio.run(actuate_export(cut, adapter, controller=ctl))
        for t in (10.0, 20.0):     # the meter stays closed: no new write, no row
            asyncio.run(actuate_export(tick(t, v.state, 2400.0), adapter, controller=ctl))
        asyncio.run(actuate_export(cut, adapter, controller=ctl))   # a re-sent cut
        rows = ctl.decision_log.records()
        assert adapter.cuts == [0.0, 0.0]
        assert len(rows) == 1
        row = rows[0]
        assert (row["device_kind"], row["new"], row["reason_code"]) == (
            "export", "limit_export 0 W", RC.EXPORT_NOT_WORTH)
        assert row["params"] == {"value": "0.012", "threshold": "0.05"}
        cat = load_plain_reasons(str(TRANSLATIONS))
        assert plain_sentence(cat, row["reason_code"], row["params"], "en") == (
            "Exporting earns 0.012 per kWh, below your limit of 0.05, "
            "so SEM stops exporting.")
        msg = describe_decision(row, cat, "nl")
        assert msg["name"] == "Teruglevering"
        assert "0.012" in msg["message"] and "0.05" in msg["message"]

    def test_the_release_records_once(self):
        import asyncio
        from custom_components.solar_energy_management.coordinator.actuate_export import (
            actuate_export,
        )
        v, tick = _dutch_day_cut()
        ctl, adapter = self._ctl(), _ExportAdapter()
        asyncio.run(actuate_export(tick(0.0, "closed", 2400.0), adapter, controller=ctl))
        asyncio.run(actuate_export(tick(10.0, "open", 0.0), adapter, controller=ctl))
        release = tick(20.0, "open", 0.0)
        asyncio.run(actuate_export(release, adapter, controller=ctl))
        for t in (30.0, 40.0):
            asyncio.run(actuate_export(tick(t, "open", 0.0), adapter, controller=ctl))
        rows = ctl.decision_log.records()
        assert adapter.releases == 1
        assert [r["reason_code"] for r in rows] == [RC.EXPORT_NOT_WORTH, RC.EXPORT_RELEASED]
        assert rows[1]["new"] == "release_export"

    def test_a_negative_price_names_itself(self):
        v, tick = _dutch_day_cut(threshold=0.0, rate=-0.02)
        assert (v.reason_code, v.reason_params) == (RC.EXPORT_NEGATIVE_PRICE, {})
        assert tick(0.0, v.state, 900.0).reason_code == RC.EXPORT_NEGATIVE_PRICE

    def test_observer_rows_are_flagged_and_nothing_is_written(self):
        import asyncio
        from custom_components.solar_energy_management.coordinator.actuate_export import (
            actuate_export,
        )
        v, tick = _dutch_day_cut()
        ctl, adapter = self._ctl(), _ExportAdapter()
        asyncio.run(actuate_export(tick(0.0, v.state, 2400.0), adapter,
                                   controller=ctl, observer=True))
        rows = ctl.decision_log.records()
        assert adapter.cuts == [] and len(rows) == 1 and rows[0]["observer"] is True

    def test_a_refused_cut_records_nothing(self):
        import asyncio
        from custom_components.solar_energy_management.coordinator.actuate_export import (
            actuate_export,
        )
        v, tick = _dutch_day_cut()
        ctl = self._ctl()
        refused = asyncio.run(actuate_export(tick(0.0, v.state, 2400.0), None,
                                             controller=ctl))
        assert refused and ctl.decision_log.records() == []
