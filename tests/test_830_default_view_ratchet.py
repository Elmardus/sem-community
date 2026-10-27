"""#830 — the default view is shrink-only, and it has no name.

Rule: *"if a user is starting with SEM he is overwhelmed with so many options
and stops using it — it is just too much."* And on naming: *"we do not name it
beginner. It's just nameless and advanced view."* Nobody is labelled a
beginner; there is the configuration, and a switch for whoever wants the
rest. The switch has two named states, **Everyday** (the default view) and
**All settings**, and neither labels the person using it.

A default view is only useful while it stays small. Every control on it arrived
for a good reason, which is exactly how ninety happened in the first place — so
the essential list is ratcheted the same shrink-only way as the option surface:
growth fails and has to be argued for.

Two properties matter as much as the size:

* **advanced hides nothing.** Everything remains one toggle away, so the split
  can never lose someone a setting they need;
* **a configured subsystem stays visible.** Hiding a section a user has already
  set up is not simplification, it is losing their work — so the section filter
  consults the Setup overview's own done-signals rather than a second opinion.
"""

import re
from pathlib import Path

CARD = (Path(__file__).resolve().parent.parent / "dashboard" / "card" / "src"
        / "cards" / "sem-config-card.js")


def _set(name):
    src = CARD.read_text(encoding="utf-8")
    m = re.search(r"const " + name + r" = new Set\(\[(.*?)\]\)", src, re.DOTALL)
    assert m, f"{name} not found"
    return set(re.findall(r"'([a-z0-9_]+)'", m.group(1)))


#: The ceiling. Raising either number is a decision, not a detail.
#: 4 → 5 (#1213): Sensor sources. A new user decides here whether SEM is for
#: them: it opens with what SEM reads, in watts, and one tap to turn a wrong
#: grid or battery direction around. The pickers stay in All settings (they
#: are counted as hidden rows), so what a user sees is one block, not four
#: more controls. Two new users (#1051, #1103) left because a wrong guess
#: could only be fixed behind All settings.
MAX_SECTIONS = 5
#: 5 → 7 (#897): the load-management arm switch and its ceiling. The shedder
#: switches off house circuits, and hiding its on/off behind Advanced is how
#: a first install shed a Span panel circuit by circuit (forum #30). A
#: control that can turn the lights off is not one to bury.
#: 7 → 8 (#1040): Calendar mode's schedule helper. It is the one entity of
#: that mode, as dynamic_tariff_entity is of Dynamic, and the card shows only
#: one of the two at a time — so what a user sees does not grow. Without it
#: the default view offered Calendar and no way to set its times.
#: 8 → 9 (#1040, second round): Calendar's off-peak rate. The mode prices
#: every hour at one of two rates; with the second hidden, two users set up
#: Calendar and read "no price difference". It shows in Calendar mode only
#: (``ESSENTIAL_IN_MODE``), so Static and Dynamic users see no change.
MAX_CONTROLS = 9


def test_the_default_view_shows_at_most_five_sections():
    got = _set("ESSENTIAL_SECTIONS")
    assert len(got) <= MAX_SECTIONS, (
        f"the default view grew to {len(got)} sections: {sorted(got)}. "
        f"It exists to be small; if this one genuinely belongs, raise "
        f"MAX_SECTIONS deliberately and say why.")


def test_the_default_view_shows_at_most_five_loose_controls():
    got = _set("ESSENTIAL_CONTROLS")
    assert len(got) <= MAX_CONTROLS, (
        f"the default view grew to {len(got)} controls: {sorted(got)}. "
        f"Every one of ninety arrived for a good reason — that is the whole "
        f"problem this list is against.")


def test_it_covers_what_sem_cannot_work_without():
    """The inverse pin: shrink-only must not shrink into uselessness. Without
    a price SEM reports zero savings, and without the EV section most installs
    have nothing to control."""
    secs = _set("ESSENTIAL_SECTIONS")
    assert "tariff" in secs, "SEM cannot cost anything without a tariff"
    assert "ev_chargers" in secs
    assert "overview" in secs, "the setup guide is how a user finds the rest"
    ctrls = _set("ESSENTIAL_CONTROLS")
    assert "tariff_mode" in ctrls, "the one tariff decision must be reachable"


def test_advanced_hides_nothing():
    """The escape. Whatever the tiering decides, advanced shows everything —
    so the split can never lose a user a setting."""
    src = CARD.read_text(encoding="utf-8")
    assert "if (this._advanced) return true;" in src, (
        "the control filter no longer short-circuits for advanced — something "
        "can now be unreachable")
    assert "this._advanced || ESSENTIAL_SECTIONS.has(s.id)" in src, (
        "the section filter no longer short-circuits for advanced")


def test_a_configured_subsystem_stays_visible():
    src = CARD.read_text(encoding="utf-8")
    assert "this._sectionConfigured(s.id)" in src, (
        "a section the user already set up can now vanish from the default "
        "view — that is losing their work, not simplifying")


def test_the_configured_check_reuses_the_setup_overview():
    """One answer to 'is this configured'. Two would drift, and the overview's
    is the one already shown to the user."""
    src = CARD.read_text(encoding="utf-8")
    m = re.search(r"_sectionConfigured\(id\) \{(.*?)\n    \}", src, re.DOTALL)
    assert m and "this._setupItems()" in m.group(1), (
        "_sectionConfigured no longer derives from the Setup overview's own "
        "done-signals")


VIEW_MODE = CARD.parent.parent / "util" / "view-mode.js"


def test_the_view_preference_is_per_browser_not_per_install():
    """A view preference is not a setting. Storing it in the config entry would
    make one person's choice everyone's. The store moved to a helper every card
    shares (one switch for every tab); the key stayed, so a choice made before
    the move is kept."""
    helper = VIEW_MODE.read_text(encoding="utf-8")
    assert 'VIEW_MODE_KEY = "sem_config_advanced_v1"' in helper, (
        "the key changed — every existing choice would be lost")
    assert "localStorage.setItem(VIEW_MODE_KEY" in helper
    assert helper.count("catch (_e)") == 2, (
        "storage access is not guarded — a private window or blocked site data "
        "would throw and take the card with it")
    # That the card starts in the stored view, stores a pick through this
    # helper and follows the event is behaviour, pinned by the real card in
    # dashboard/card/test/everyday-view.test.js.


#: Rows inside one charger's block that Everyday keeps: what wires it to SEM.
#: Before this list existed every per-charger row (nine on a plain charger)
#: showed in the default view, because the nested helpers never asked.
MAX_CHARGER_CONTROLS = 2


def test_a_charger_shows_at_most_two_rows_in_everyday():
    got = _set("ESSENTIAL_CHARGER_CONTROLS")
    assert len(got) <= MAX_CHARGER_CONTROLS, (
        f"Everyday grew to {len(got)} rows per charger: {sorted(got)}. "
        f"Set-once car values and extra wiring belong in All settings.")
    assert {"ev_charging_power_sensor", "ev_current_control_entity"} <= got, (
        "a charger in Everyday must still show the power it reads and the "
        "control it drives")


#: What a template must contain to be a setting row (picker, stepper, knob,
#: free-text input). The JS test counts rendered rows by the same marks.
_ROW_MARKS = ('class="picker-cell', 'class="stepper-cell', 'class="zone-knob',
              'class="txt-opt', 'class="pv-name-input')


def _methods(src):
    """Each class method of the card as (name, body), by indentation."""
    return [(m.group(1), m.group(2)) for m in re.finditer(
        r"^    (_[A-Za-z]\w*)\([^\n]*\) \{\n(.*?)^    \}$", src, re.M | re.S)]


def test_every_row_helper_asks_the_everyday_gate():
    """The leak this closes: only some helpers asked whether a row belongs in
    Everyday. The battery knobs, entity toggles and selects, every per-charger
    row and the free-text rows did not, so the default view showed twenty rows
    on a home with a battery and one charger. Any method that draws a setting
    row now goes through ``_gate`` (or ``_gateCharger`` inside a charger)."""
    src = CARD.read_text(encoding="utf-8")
    drawing = [(n, b) for n, b in _methods(src) if any(k in b for k in _ROW_MARKS)]
    assert len(drawing) >= 15, [n for n, _ in drawing]
    leaks = [n for n, b in drawing
             if "this._gate(" not in b and "this._gateCharger(" not in b]
    assert not leaks, (
        f"these helpers draw a setting row without asking the Everyday gate: "
        f"{leaks}")


def test_the_switch_reads_everyday_and_all_settings():
    import json
    tr = json.loads((Path(__file__).resolve().parent.parent / "dashboard"
                     / "translations.json").read_text(encoding="utf-8"))
    assert tr["en"]["config_view_everyday"] == "Everyday"
    assert tr["en"]["config_advanced_label"] == "All settings"
    for lang, table in tr.items():
        for key in ("config_view_everyday", "config_advanced_label",
                    "config_group_basics", "config_group_devices",
                    "config_group_finetuning", "config_finetuning_folded",
                    "config_more_in_all", "config_everyday_nothing_here"):
            assert table.get(key), f"{lang}.{key} missing"
        if lang != "en":
            assert table["config_advanced_label"] != "All settings", lang
            assert table["config_view_everyday"] != "Everyday", lang


def test_nothing_is_called_beginner():
    """Explicit rule: the default view has no name. A label that tells
    someone they are a beginner is a reason to leave."""
    src = CARD.read_text(encoding="utf-8")
    # Strip comments before looking. A line-prefix check cannot see the middle
    # of a block comment, and the word appears there legitimately — explaining
    # exactly why the view has no name.
    stripped = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    stripped = re.sub(r"//[^\n]*", "", stripped)
    offenders = [ln.strip() for ln in stripped.split("\n")
                 if re.search(r"\bbeginner\b", ln, re.I)]
    assert not offenders, f"user-facing 'beginner' wording: {offenders}"


def test_the_translations_never_say_beginner():
    """The word must not reach a user in any of the sixteen languages."""
    import json
    tr = json.loads((Path(__file__).resolve().parent.parent / "dashboard"
                     / "translations.json").read_text(encoding="utf-8"))
    bad = []
    for lang, table in tr.items():
        for key, value in table.items():
            if "beginner" in str(key).lower() or "beginner" in str(value).lower():
                bad.append(f"{lang}.{key}")
    assert not bad, f"'beginner' reaches the user: {bad[:5]}"
