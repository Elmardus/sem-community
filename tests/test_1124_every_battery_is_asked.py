"""#1124 — a second battery that ignores SEM's writes got no warning.

#915 reads every battery control write back, and three writes the entity
did not keep become a Repair. The per-cycle check asked
``_primary_battery_adapter()`` only, so on a two-battery home (two Sessys,
#978) the second battery could ignore every write and only the log said so.

Pinned here, through real generic adapters and the real per-cycle method
(``SEMCoordinator._verify_battery_writes``):

* the second battery's ignored mode change and ignored limit each become a
  Repair, once;
* the first battery's kept writes do not take that Repair down (class 83 —
  before, any kept write cleared every raised Repair);
* two batteries set up on one entity share one Repair, and it stays while
  either still sees its writes ignored;
* the card's one verdict is False when any battery's write was not kept;
* a Deye second in the list still gets its rate note;
* every remaining ``_primary_battery_adapter()`` caller says why the first
  battery alone is the right answer (class 146).
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.solar_energy_management.coordinator import (
    repair_issues as ri,
)
from custom_components.solar_energy_management.coordinator.battery_adapters.deye import (
    DeyeBatteryAdapter,
)
from custom_components.solar_energy_management.coordinator.battery_adapters.generic import (
    GenericBatteryAdapter,
)
from custom_components.solar_energy_management.coordinator.coordinator import (
    SEMCoordinator,
)

ROOT = Path(__file__).resolve().parents[1]
_NOT_PRODUCTION = {"tests", "scripts", "tools", "docs", "dashboard", "brand",
                   "translations", "__pycache__"}


# ─────────────────────────────────────────────────────────────────
# A home with two batteries
# ─────────────────────────────────────────────────────────────────

def _num(value, lo=0.0, hi=5000.0):
    return SimpleNamespace(state=str(value), attributes={
        "min": lo, "max": hi, "unit_of_measurement": "W"})


def _home(states, deaf=()):
    """One hass for every battery. A ``deaf`` entity takes the service call
    without an error and keeps its old state — a register or select that
    ignores SEM (#915, #978)."""
    table = dict(states)
    deaf = set(deaf)
    hass = MagicMock()
    hass.states.get = MagicMock(side_effect=lambda e: table.get(e))

    async def _call(domain, service, data=None, **kw):
        ent = str((data or {}).get("entity_id", "") or "")
        if ent in deaf or ent not in table:
            return None
        if service == "set_value":
            table[ent] = SimpleNamespace(state=str(data["value"]),
                                         attributes=table[ent].attributes)
        elif service == "select_option":
            table[ent] = SimpleNamespace(state=data["option"],
                                         attributes=table[ent].attributes)

    hass.services.async_call = AsyncMock(side_effect=_call)
    hass.table = table
    hass.deaf = deaf
    return hass


def _coord(hass, adapters):
    c = SEMCoordinator.__new__(SEMCoordinator)
    c.hass = hass
    c._battery_adapters = dict(adapters)
    return c


def _sessy(hass, n):
    return GenericBatteryAdapter(hass, {
        "battery_force_discharge_control_entity": f"number.sessy_{n}_power_setpoint",
        "battery_strategy_control_entity": f"select.sessy_{n}_power_strategy",
        "battery_strategy_active_value": "api",
        "battery_strategy_idle_value": "eco",
        "battery_max_discharge_power": 1700,
        "battery_max_charge_power": 2200,
        "battery_setpoint_bidirectional": True,
    })


def _sessy_states(n):
    return {
        f"number.sessy_{n}_power_setpoint": _num(0.0, -2200.0, 1700.0),
        f"select.sessy_{n}_power_strategy": SimpleNamespace(
            state="nom", attributes={"options": ["api", "eco", "idle", "nom", "roi"]}),
    }


def _limited(hass, entity):
    return GenericBatteryAdapter(hass, {
        "battery_discharge_control_entity": entity,
        "battery_max_discharge_power": 5000,
    })


class _Repairs:
    """Every raise and clear in order, so a test can ask what came after
    what."""

    def __init__(self):
        self.events = []

    def raised(self, hass, *, entity_id, wanted, seen, strikes):
        self.events.append(("raise", entity_id))

    def cleared(self, hass, entity_id):
        self.events.append(("clear", entity_id))

    def count(self, kind, entity_id):
        return self.events.count((kind, entity_id))

    def after_first_raise(self, entity_id):
        i = self.events.index(("raise", entity_id))
        return self.events[i + 1:]


def _patched(rep):
    return (patch.object(ri, "raise_battery_control_write_not_taken", rep.raised),
            patch.object(ri, "clear_battery_control_write_not_taken", rep.cleared))


async def _limit_cycles(c, wishes, n_cycles, rep, verdicts, frozen=()):
    """10-second cycles. Every battery is commanded, then the per-cycle
    check runs — the coordinator's order. A wish moves every second cycle,
    so each write has a cycle to be judged in (the adapters skip a wish
    within 100 W of the last one); a ``frozen`` battery's wish stays put."""
    t = [1000.0]
    a, b = _patched(rep)
    orig = {bid: ad.verify_pending_write for bid, ad in c._battery_adapters.items()}

    def _spy(bid, real):
        def _v():
            v = real()
            rep.events.append(("verdict", bid, v))
            return v
        return _v
    with a, b, patch("time.monotonic", side_effect=lambda: t[0]):
        for bid, ad in c._battery_adapters.items():
            ad.verify_pending_write = _spy(bid, orig[bid])
        try:
            for i in range(n_cycles):
                for bid, ad in c._battery_adapters.items():
                    step = 0.0 if bid in frozen else 200.0 * (i // 2)
                    await ad.command_limit_discharge(wishes[bid] + step)
                result = {}
                c._verify_battery_writes(result)
                verdicts.append(result)
                t[0] += 10.0
        finally:
            for bid, ad in c._battery_adapters.items():
                ad.verify_pending_write = orig[bid]


# ─────────────────────────────────────────────────────────────────
# 1. The issue: the second battery is asked
# ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestTheSecondBatteryIsAsked:
    async def test_a_second_sessy_that_refuses_a_mode_change_gets_a_repair(self):
        """The reported case: two Sessys, the second's strategy select never
        takes ``api``. The first is the one the old check asked."""
        sel2 = "select.sessy_2_power_strategy"
        hass = _home({**_sessy_states(1), **_sessy_states(2)}, deaf={sel2})
        b1, b2 = _sessy(hass, 1), _sessy(hass, 2)
        c = _coord(hass, {"b1": b1, "b2": b2})
        assert c._primary_battery_adapter() is b1   # the old check's only battery
        rep = _Repairs()
        raise_kw = []
        t = [1000.0]

        def _raised(hass, **kw):
            raise_kw.append(kw)
            rep.raised(hass, **kw)
        with patch.object(ri, "raise_battery_control_write_not_taken", _raised), \
                patch.object(ri, "clear_battery_control_write_not_taken", rep.cleared), \
                patch("time.monotonic", side_effect=lambda: t[0]):
            for _ in range(20):                         # 200 s of 10 s cycles
                await b1.command_force_discharge(1700, 20.0)
                await b2.command_force_discharge(1700, 20.0)
                c._verify_battery_writes({})
                t[0] += 10.0
        assert hass.table["select.sessy_1_power_strategy"].state == "api"
        assert b2.write_not_taken_strikes == 3
        assert [k["entity_id"] for k in raise_kw] == [sel2]
        assert (raise_kw[0]["wanted"], raise_kw[0]["seen"]) == ("api", "nom")
        assert not any(kind == "clear" and eid == sel2 for kind, eid in rep.events)

    async def test_a_second_battery_that_ignores_its_limit_gets_a_repair(self):
        hass = _home({"number.b1_limit": _num(5000.0),
                      "number.b2_limit": _num(5000.0)}, deaf={"number.b2_limit"})
        c = _coord(hass, {"b1": _limited(hass, "number.b1_limit"),
                          "b2": _limited(hass, "number.b2_limit")})
        rep, verdicts = _Repairs(), []
        await _limit_cycles(c, {"b1": 1000.0, "b2": 1200.0}, 8, rep, verdicts)
        assert rep.count("raise", "number.b2_limit") == 1
        assert rep.count("raise", "number.b1_limit") == 0


# ─────────────────────────────────────────────────────────────────
# 2. A healthy battery does not take another's Repair down
# ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestAKeptWriteClearsOnlyItsOwnEntity:
    async def test_the_first_batterys_kept_writes_leave_the_second_ones_repair(self):
        hass = _home({"number.b1_limit": _num(5000.0),
                      "number.b2_limit": _num(5000.0)}, deaf={"number.b2_limit"})
        c = _coord(hass, {"b1": _limited(hass, "number.b1_limit"),
                          "b2": _limited(hass, "number.b2_limit")})
        rep, verdicts = _Repairs(), []
        await _limit_cycles(c, {"b1": 1000.0, "b2": 1200.0}, 30, rep, verdicts)
        after = rep.after_first_raise("number.b2_limit")
        # liveness: the healthy battery DID prove writes after the Repair —
        # without these, "nothing cleared it" would pass on a frozen run
        assert sum(1 for e in after if e == ("verdict", "b1", True)) >= 5
        assert ("clear", "number.b2_limit") not in after
        assert rep.count("raise", "number.b2_limit") == 1
        # the healthy battery's own entity was cleared once, as #933 wants
        assert rep.count("clear", "number.b1_limit") == 1

    async def test_two_batteries_on_one_entity_share_one_repair(self):
        """A set-up mistake #531 warns about: both write one register. The
        second write wins, so the first battery's writes are never kept
        while the second's always are. The shared Repair must not flap."""
        hass = _home({"number.shared": _num(5000.0)})
        c = _coord(hass, {"b1": _limited(hass, "number.shared"),
                          "b2": _limited(hass, "number.shared")})
        rep, verdicts = _Repairs(), []
        # both wishes stay under the 5000 W ceiling: a wish clamped there
        # stops moving, the second battery stops writing and the fight ends
        await _limit_cycles(c, {"b1": 1000.0, "b2": 2000.0}, 30, rep, verdicts)
        after = rep.after_first_raise("number.shared")
        assert sum(1 for e in after if e == ("verdict", "b2", True)) >= 5
        assert ("clear", "number.shared") not in after
        assert rep.count("raise", "number.shared") == 1

    async def test_the_shared_repair_clears_once_both_writes_are_kept(self):
        hass = _home({"number.shared": _num(5000.0)})
        b1, b2 = _limited(hass, "number.shared"), _limited(hass, "number.shared")
        c = _coord(hass, {"b1": b1, "b2": b2})
        rep, verdicts = _Repairs(), []
        await _limit_cycles(c, {"b1": 1000.0, "b2": 2000.0}, 12, rep, verdicts)
        assert rep.count("raise", "number.shared") == 1
        assert ("clear", "number.shared") not in rep.after_first_raise("number.shared")
        # the fight ends: b2 stays but its wish stops moving, so it stops
        # writing, and b1's next changed write is kept
        await _limit_cycles(c, {"b1": 4000.0, "b2": 2000.0}, 6, rep, verdicts,
                            frozen={"b2"})
        after = rep.after_first_raise("number.shared")
        assert ("verdict", "b1", True) in after
        assert after.count(("clear", "number.shared")) == 1
        assert rep.count("raise", "number.shared") == 1


@pytest.mark.unit
class TestOnlyThisCyclesBatteriesAreAsked:
    """A synthetic ``primary`` from the first cycles stays in the adapter
    cache once b1/b2 exist (class 84 left open (5)). Its ledger is frozen:
    it must not answer for the house, nor hold a Repair b1 has disproved."""

    def _stale_and_live(self):
        hass = _home({"number.limit": _num(1200.0)})
        stale = _stub(None, 3)
        stale.last_unverified_entity = "number.limit"
        live = _stub(True, 0)
        live.last_verified_entity = "number.limit"
        c = _coord(hass, {"primary": stale, "b1": live})
        c._live_battery_ids = frozenset({"b1"})
        c._battery_write_repair_raised = {"number.limit"}
        return c

    def test_a_stale_adapter_holds_no_repair(self):
        c = self._stale_and_live()
        rep = _Repairs()
        a, b = _patched(rep)
        result = {}
        with a, b:
            c._verify_battery_writes(result)
        assert rep.events == [("clear", "number.limit")]
        assert result == {"battery_control_write_verified": True,
                          "battery_control_write_strikes": 0}

    def test_before_the_first_cycle_every_cached_adapter_is_asked(self):
        c = self._stale_and_live()
        del c._live_battery_ids
        rep = _Repairs()
        a, b = _patched(rep)
        result = {}
        with a, b:
            c._verify_battery_writes(result)
        assert rep.events == []                 # the "primary" still holds it
        assert result["battery_control_write_strikes"] == 3


# ─────────────────────────────────────────────────────────────────
# 2b. The select proves itself (review of #1124)
# ─────────────────────────────────────────────────────────────────

SEL, SP, LIM = ("select.sessy_1_power_strategy", "number.sessy_1_power_setpoint",
                "number.sessy_1_limit")


def _sessy_with_limit(hass):
    """A Sessy that also has a discharge-limit entity: two entities on the
    #915 ledger of one adapter."""
    return GenericBatteryAdapter(hass, {
        "battery_force_discharge_control_entity": SP,
        "battery_strategy_control_entity": SEL,
        "battery_strategy_active_value": "api",
        "battery_strategy_idle_value": "eco",
        "battery_discharge_control_entity": LIM,
        "battery_max_discharge_power": 1700,
        "battery_max_charge_power": 2200,
        "battery_setpoint_bidirectional": True,
    })


@pytest.mark.asyncio
class TestTheSelectProvesItself:
    async def test_a_select_that_lands_while_the_limit_is_judged_is_cleared(self):
        """The select's kept verdict waits while the limit's is reported. The
        limit's verdict re-pointed the ledger at the limit, so the select's
        verdict, one cycle later, named the limit — and only the named
        entity is cleared now."""
        hass = _home({**_sessy_states(1), LIM: _num(5000.0)}, deaf={SEL})
        ad = _sessy_with_limit(hass)
        c = _coord(hass, {"b1": ad})
        rep = _Repairs()
        t = [1000.0]
        a, b = _patched(rep)
        with a, b, patch("time.monotonic", side_effect=lambda: t[0]):
            for _ in range(20):                         # three misses
                await ad.command_force_discharge(1700, 20.0)
                c._verify_battery_writes({})
                t[0] += 10.0
            assert rep.count("raise", SEL) == 1
            hass.deaf.discard(SEL)                      # the user fixes it
            t[0] += 60.0
            # a limit write goes out: NORMAL's max (5000 → 1700). (#1175) A
            # lower limit would be put back to the max by the sale below,
            # and that new write would be checked one cycle later.
            await ad.command_normal()
            c._verify_battery_writes({})
            t[0] += 10.0
            await ad.command_force_discharge(1700, 20.0)    # the select lands…
            c._verify_battery_writes({})                # …the limit is judged
            assert hass.table[SEL].state == "api"
            assert ("clear", LIM) in rep.events
            t[0] += 10.0
            c._verify_battery_writes({})                # the select's verdict
        assert ("clear", SEL) in rep.after_first_raise(SEL)

    async def test_a_fresh_lifetime_clears_the_selects_old_repair(self):
        """After a restart the select lands at once. That is proof too: a
        Repair from before the restart must go (class 84)."""
        hass = _home(_sessy_states(1))
        ad = _sessy(hass, 1)
        c = _coord(hass, {"b1": ad})
        rep = _Repairs()
        t = [1000.0]
        a, b = _patched(rep)
        with a, b, patch("time.monotonic", side_effect=lambda: t[0]):
            for _ in range(5):
                await ad.command_force_discharge(1700, 20.0)
                c._verify_battery_writes({})
                t[0] += 10.0
        assert rep.count("clear", SEL) == 1
        assert ad.last_verified_entity == SEL

    async def test_a_landing_does_not_hide_a_miss_waiting_to_be_reported(self):
        hass = _home(_sessy_states(1))
        ad = _sessy(hass, 1)
        ad._strategy_verdict = False
        ad._strategy_miss = ("api", "nom")
        await ad.command_force_discharge(1700, 20.0)    # sends api, it lands
        assert ad.verify_pending_write() is False
        assert (ad.last_unverified_entity, ad.last_unverified_wanted,
                ad.last_unverified_seen) == (SEL, "api", "nom")


# ─────────────────────────────────────────────────────────────────
# 3. The card's one verdict
# ─────────────────────────────────────────────────────────────────

def _stub(verdict, strikes=0, boom=False):
    def _v():
        if boom:
            raise RuntimeError("read-back failed")
        return verdict
    return SimpleNamespace(verify_pending_write=_v, write_not_taken_strikes=strikes,
                           last_unverified_entity="", last_verified_entity="")


@pytest.mark.unit
class TestTheCardVerdictCoversEveryBattery:
    def _run(self, adapters):
        c = _coord(MagicMock(), adapters)
        result = {}
        with patch.object(ri, "raise_battery_control_write_not_taken"), \
                patch.object(ri, "clear_battery_control_write_not_taken"):
            c._verify_battery_writes(result)
        return result

    def test_one_battery_not_kept_reads_false(self):
        r = self._run({"b1": _stub(True, 0), "b2": _stub(False, 2)})
        assert r["battery_control_write_verified"] is False
        assert r["battery_control_write_strikes"] == 2

    def test_not_kept_first_still_reads_false(self):
        """The order must not decide: a later kept write is no answer for
        the battery whose write was not kept."""
        r = self._run({"b1": _stub(False, 3), "b2": _stub(True, 0)})
        assert r["battery_control_write_verified"] is False
        assert r["battery_control_write_strikes"] == 3

    def test_kept_and_not_judged_reads_true(self):
        r = self._run({"b1": _stub(None, 0), "b2": _stub(True, 0)})
        assert r["battery_control_write_verified"] is True

    def test_nothing_judged_reads_none_not_false(self):
        r = self._run({"b1": _stub(None, 0), "b2": _stub(None, 1)})
        assert r["battery_control_write_verified"] is None
        assert r["battery_control_write_strikes"] == 1

    def test_one_battery_is_unchanged(self):
        r = self._run({"primary": _stub(False, 3)})
        assert r == {"battery_control_write_verified": False,
                     "battery_control_write_strikes": 3}

    def test_no_battery_publishes_nothing(self):
        assert self._run({}) == {}

    def test_a_read_back_that_raises_does_not_cost_the_other(self):
        r = self._run({"b1": _stub(None, boom=True), "b2": _stub(False, 1)})
        assert r["battery_control_write_verified"] is False


# ─────────────────────────────────────────────────────────────────
# 4. The Deye rate note, whichever battery it is
# ─────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestTheRateNoteOfASecondDeye:
    def _deye(self, control):
        hass = MagicMock()
        return DeyeBatteryAdapter(hass, {
            "battery_platform": "deye",
            "deye_system_work_mode_control": control,
            "deye_system_work_mode_entity": "select.deye_system_work_mode",
            "deye_system_work_mode_selling_option": "Selling First",
            "deye_system_work_mode_zero_load_option": "Zero Export To Load",
            "deye_system_work_mode_zero_ct_option": "Zero Export To CT",
        })

    def test_a_deye_second_in_the_list_has_its_note(self):
        hass = _home({})
        c = _coord(hass, {"b1": _limited(hass, "number.b1_limit"),
                          "b2": self._deye(True)})
        note = c._battery_discharge_rate_caveat()
        assert note and "inverter" in note.lower()

    def test_without_the_mode_control_there_is_no_note(self):
        hass = _home({})
        c = _coord(hass, {"b1": _limited(hass, "number.b1_limit"),
                          "b2": self._deye(False)})
        assert c._battery_discharge_rate_caveat() is None


# ─────────────────────────────────────────────────────────────────
# 5. Class 146 — who may ask the first battery alone
# ─────────────────────────────────────────────────────────────────

#: Every production function that calls ``_primary_battery_adapter()``, and
#: why the FIRST battery alone answers its question. A check every battery
#: must pass (a write read-back, a Repair, a note about one brand) does not
#: belong here: it loops over ``_battery_adapters``.
PRIMARY_ONLY_CALLERS = {
    "coordinator/coordinator.py::SEMCoordinator._run_battery_pipeline":
        "the #845 mode watch reads ONE install-wide entity "
        "(battery_operating_mode_entity); taking its expected modes from the "
        "first battery's brand is class 84 left open (3), a decision",
    "coordinator/coordinator.py::SEMCoordinator._export_control_adapter":
        "the last fallback after the holder of the cut and any brand that can "
        "cut export: a house with neither still gets a refusal with a brand "
        "in it (#955)",
}


def _primary_callers(tree, rel):
    found = []
    stack = []

    class _V(ast.NodeVisitor):
        def visit_ClassDef(self, n):
            stack.append(n.name); self.generic_visit(n); stack.pop()

        def visit_FunctionDef(self, n):
            stack.append(n.name); self.generic_visit(n); stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, n):
            fn = n.func
            if isinstance(fn, ast.Attribute) and fn.attr == "_primary_battery_adapter":
                found.append(f"{rel}::{'.'.join(stack)}")
            self.generic_visit(n)

    _V().visit(tree)
    return found


def _production_files():
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT)
        if rel.parts[0] in _NOT_PRODUCTION or any(
                part.startswith(".") for part in rel.parts):
            continue
        yield p, rel.as_posix()


@pytest.mark.unit
class TestEveryFirstBatteryCallerIsDeclared:
    def test_no_undeclared_caller(self):
        callers = set()
        for p, rel in _production_files():
            callers.update(_primary_callers(ast.parse(p.read_text()), rel))
        assert len(callers) >= 2, "the walker found nothing — it parses nothing"
        new = sorted(callers - set(PRIMARY_ONLY_CALLERS))
        assert not new, (
            f"{new} ask _primary_battery_adapter() only. A check every battery "
            "must pass loops over _battery_adapters (#1124); if the first "
            "battery alone is right here, declare why in PRIMARY_ONLY_CALLERS")
        stale = sorted(set(PRIMARY_ONLY_CALLERS) - callers)
        assert not stale, f"{stale} no longer call it — remove the declaration"

    def test_the_detector_sees_the_old_shape(self):
        """The #1124 code itself, as it was: the twin must fire on it."""
        src = (
            "class SEMCoordinator:\n"
            "    async def _async_update_data(self):\n"
            "        _ad = self._primary_battery_adapter()\n"
            "        _v = _ad.verify_pending_write()\n"
        )
        got = _primary_callers(ast.parse(src), "coordinator/coordinator.py")
        assert got == ["coordinator/coordinator.py::SEMCoordinator._async_update_data"]
        assert not set(got) <= set(PRIMARY_ONLY_CALLERS)

    def test_the_write_check_loops_over_every_battery(self):
        tree = ast.parse(Path(
            SEMCoordinator._verify_battery_writes.__code__.co_filename).read_text())

        def _fn(name):
            return next(n for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef) and n.name == name)

        def _attrs(node):
            return {a.attr for a in ast.walk(node) if isinstance(a, ast.Attribute)}
        check = _fn("_verify_battery_writes")
        assert "_primary_battery_adapter" not in _attrs(check)
        assert "_live_battery_adapters" in _attrs(check)
        roster = _fn("_live_battery_adapters")
        assert "_primary_battery_adapter" not in _attrs(roster)
        assert any(isinstance(n, ast.Constant) and n.value == "_battery_adapters"
                   for n in ast.walk(roster))
