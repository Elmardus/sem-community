"""#955 — the export cut stays on SEM's one track, structurally.

Reading the built arc: *"is this implemented where all the decisions
are taking place, or are we on a second track? I remember having 3 layers in
SEM."* That was right: the first build decided IN the coordinator, so there
were two producers of ``BatteryDecision``, two ``actuate_battery`` call
sites, and an observer surface where they clobbered each other under one key
— which is how the cut came out invisible on .175.

**What this file claims, and what it does not.** SEM does NOT have a single
universal rule that every write goes through a decide layer: ``charge_pacing``
and ``load_management`` call services directly today, and a ruflo review
refuted that broader framing when this arc first asserted it. The claim here
is narrower and true: the two axes that share the battery adapters — the
battery itself and the house's meter limit — each have ONE producer of their
decision, ONE seam that writes it, and a decider that touches nothing outside
the decide layer.

Structural, not textual (#925/#924): a grep for ``"actuate_export("`` passes
on a mention in a comment and is blind to the sibling call site that is the
actual defect. These walk the parsed tree.
"""
from pathlib import Path

import pytest

from .ast_contracts import call_sites

ROOT = Path(__file__).resolve().parent.parent
DECIDERS = ("decide.py", "decide_battery.py", "decide_export.py")


def _names_symbol(name: str) -> set:
    """Every production file that NAMES ``name`` in code, import included.

    Strings, comments and docstrings are ignored by construction (the tree is
    parsed), which is the point — a mention in prose is not a reference.
    """
    import ast
    out = set()
    for p in sorted((ROOT / "coordinator").rglob("*.py")):
        rel = str(p.relative_to(ROOT))
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(n, ast.ImportFrom):
                if any(a.name == name or a.asname == name for a in n.names):
                    out.add(rel)
            elif isinstance(n, ast.Import):
                if any(a.asname == name for a in n.names):
                    out.add(rel)
            elif isinstance(n, ast.Name) and n.id == name:
                out.add(rel)
            elif isinstance(n, ast.Attribute) and n.attr == name:
                out.add(rel)
            elif isinstance(n, ast.Call) and getattr(n.func, "id", None) == "getattr":
                for a in n.args[1:2]:
                    if isinstance(a, ast.Constant) and a.value == name:
                        out.add(rel)
    return out


def _producing_files(class_name: str) -> set:
    """Every production file that CONSTRUCTS ``class_name``."""
    import ast
    out = set()
    for p in sorted((ROOT / "coordinator").rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == class_name:
                out.add(str(p.relative_to(ROOT)))
    return out


class TestOneProducerPerDecision:
    """A decision with two authors has no author."""

    def test_only_the_battery_decider_builds_a_battery_decision(self):
        assert _producing_files("BatteryDecision") == {"coordinator/decide_battery.py"}

    def test_only_the_export_decider_builds_an_export_decision(self):
        assert _producing_files("ExportDecision") == {"coordinator/decide_export.py"}


class TestOneSeamPerAxis:
    """One write per axis, so observer mode has one place to cut."""

    @pytest.mark.parametrize("seam", ["actuate_battery", "actuate_export"])
    def test_exactly_one_production_call_site(self, seam):
        sites = [(f, n) for f, n, _ in call_sites(seam, root=ROOT)
                 if not f.endswith(f"{seam}.py")]   # the def is not a call
        assert len(sites) == 1, f"{seam} is dispatched from {len(sites)} places: {sites}"

    def test_the_export_seam_owns_its_observer_key(self):
        """The .175 bug, made unrepresentable: the cut was published under
        ``battery:<id>``, so the battery's own decision overwrote it every
        cycle and a feature whose whole promise is 'I am holding the meter
        shut' was invisible on the rig (#855).

        Not ``symbol_reference_files``: it sees attribute access and getattr
        literals, and an IMPORT of the name is neither — a mutation that did
        ``from .actuate_export import OBSERVER_KEY`` in ``actuate_battery``
        left this test green. A borrowed key is borrowed by importing it.
        """
        assert _names_symbol("OBSERVER_KEY") <= {"coordinator/actuate_export.py"}, \
            _names_symbol("OBSERVER_KEY")


class TestTheDecidersStayInTheDecideLayer:
    """Pure: same inputs, same decision. No hass, no adapters, no clock, no
    awaiting anything — the property ``decide.py`` and ``decide_battery.py``
    already hold, now pinned so the next axis cannot quietly break it."""

    @pytest.mark.parametrize("module", DECIDERS)
    def test_it_reaches_nothing_outside(self, module):
        import ast
        tree = ast.parse((ROOT / "coordinator" / module).read_text(encoding="utf-8"))
        reaches = []
        for n in ast.walk(tree):
            if isinstance(n, (ast.Await, ast.AsyncFunctionDef)):
                reaches.append(("await/async", n.lineno))
            elif isinstance(n, ast.Attribute) and n.attr in (
                    "async_call", "hass", "_battery_adapters", "_export_guard"):
                reaches.append((n.attr, n.lineno))
            elif isinstance(n, ast.Name) and n.id == "hass":
                reaches.append(("hass", n.lineno))
        assert not reaches, f"{module} reaches outside the decide layer: {reaches}"


class TestTheCutIsIdentityKeyed:
    """#908/#936: hand back only what SEM commanded, to the adapter it
    commanded. The store round-trips by battery id, and the question 'am I
    holding a cut' is asked of the ADAPTER, never inferred from its ability
    to describe one — Huawei can always describe the reset."""

    def test_holding_is_a_question_the_adapter_answers(self):
        from custom_components.solar_energy_management.coordinator.battery_adapters.base import (
            BatteryControlAdapter,
        )
        assert callable(getattr(BatteryControlAdapter, "holds_export_cut", None))

    @pytest.mark.parametrize("fn", ["export_release_recipes",
                                    "async_release_export_guard",
                                    "_export_guard_adopt"])
    def test_every_hand_back_path_asks_it(self, fn):
        import ast
        import inspect
        import textwrap
        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )
        tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(SEMCoordinator, fn))))
        names = {getattr(n.func, "attr", None) for n in ast.walk(tree)
                 if isinstance(n, ast.Call)}
        if fn == "_export_guard_adopt":
            # adoption takes over only the adapters the STORE says were
            # holding; that it SKIPS the others is a behaviour, pinned in
            # test_921_handback.py, not a phrase to grep for.
            assert "adopt_export_prior" in names
        else:
            assert "holds_export_cut" in names, f"{fn} hands back what it never took"


class TestTheCoordinatorOnlyTicks:
    """Named pieces, each testable alone — the 76-line method that ticked,
    decided and wrote could only be exercised with a whole coordinator, which
    is how it shipped a guard that released a cut it never made."""

    @pytest.mark.parametrize("name", ["_ensure_export_guard",
                                      "_compute_export_command",
                                      "_apply_export_decision",
                                      "_export_control_adapter",
                                      "_publish_export_guard_state"])
    def test_the_piece_exists_and_is_reachable(self, name):
        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )
        assert callable(getattr(SEMCoordinator, name, None))

    def test_the_old_one_track_method_is_gone(self):
        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )
        assert not hasattr(SEMCoordinator, "_run_export_guard")

    def test_the_tick_decides_nothing_and_writes_nothing(self):
        import ast
        import inspect
        import textwrap
        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(SEMCoordinator._compute_export_command)))
        names = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                 for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert not (names & {"decide_export", "actuate_export", "async_call"}), names

    def test_the_card_is_refreshed_even_when_the_dispatch_fails(self):
        """A failed store write must not leave the rig reading 'engaged' while
        the axis is broken — silence looks like health (#925). The publish sits
        in the dispatch site's ``finally``, not inside the thing that can raise."""
        import ast
        import inspect
        import textwrap
        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )
        for meth in (SEMCoordinator._run_battery_pipeline,
                     SEMCoordinator._apply_export_decision):
            tree = ast.parse(textwrap.dedent(inspect.getsource(meth)))
            in_finally = any(
                isinstance(c, ast.Call)
                and getattr(c.func, "attr", None) == "_publish_export_guard_state"
                for t in ast.walk(tree) if isinstance(t, ast.Try)
                for f in t.finalbody for c in ast.walk(f))
            if in_finally:
                return
        raise AssertionError("_publish_export_guard_state is not in a finally")

    def test_the_guard_ticks_after_the_verdicts_it_reads(self):
        """It sat beside ``_compute_peak_slot_allowance`` for symmetry, 60
        lines BEFORE ``self._sink_verdicts`` is assigned — so the guard was
        keying on the previous cycle's grid verdict."""
        import ast
        import inspect
        import textwrap
        from custom_components.solar_energy_management.coordinator.coordinator import (
            SEMCoordinator,
        )
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(SEMCoordinator._build_fleet_cycle_state)))
        assigned = [n.lineno for n in ast.walk(tree)
                    if isinstance(n, ast.Assign)
                    for t in n.targets
                    if isinstance(t, ast.Attribute) and t.attr == "_sink_verdicts"]
        ticked = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                  and getattr(n.func, "attr", None) == "_compute_export_command"]
        assert assigned and ticked, (assigned, ticked)
        assert min(ticked) > max(assigned), "the guard reads a stale verdict"
