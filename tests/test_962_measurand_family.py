"""#962 — a charger's ADVERTISED capability is never its measurement.

Reporter (@bgthb, 2.0.0, Huawei SCharger 22-KT over lbbrhzn/ocpp): SEM read
the charger as drawing full power at all times, with no car plugged in.
Auto-detection had bound ``sensor.wallbox_power_offered`` as
``ev_charging_power_sensor`` and ``sensor.wallbox_energy_active_export_interval``
as ``ev_total_energy_sensor``.

Root cause: OCPP names its sensors after the PROTOCOL's measurands, so one
charge point publishes a whole family under ``device_class: power``
(``Power.Active.Import``, ``Power.Offered``, ``Power.Active.Export``) and
another under ``device_class: energy``. Every brand matcher binds these read
roles on the device class ALONE and keeps the first/last entity it happens to
see, so registry ORDER picked — and ``Power.Offered`` is the box's nameplate,
reported continuously whether or not a car is connected. SEM then infers a
connection "from physics" (``sensor_reader``: current cannot flow without a
plug), so an idle charger reads as a charging car forever.

Bug class 89 (a read role bound among measurand siblings that share domain +
device_class). The closure is brand-agnostic — ``_reject_capability_sensor``
runs inside ``apply_charger_discovery_guards``, the one choke point all four
discovery paths funnel through — so these pins assert the CLASS:

* the reporter's own family resolves to the measurands SEM asked for;
* the answer does not depend on registry ORDER, for any brand;
* no brand, hinted or hand-written, can bind a capability-named sensor to a
  read role;
* a family with nothing but capabilities DROPS the role (fail-closed: a
  missing power reading is honest, a nameplate read as a measurement is not);
* "rated" inside ``solar_generated_power`` is not the word "rated"
  (class 67 — the rules read segments, not substrings).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from custom_components.solar_energy_management.hardware_detection import (
    _EV_CHARGER_PLATFORMS,
    _MEASURAND_ROLES,
    _measures_the_quantity,
    _reject_capability_sensor,
    apply_charger_discovery_guards,
    build_detection_report,
    charger_from_near_miss,
    discover_all_ev_chargers_from_registry,
    probe_charger_candidates,
)


def _entry(entity_id, platform, device_id, device_class=None, unit=None):
    return SimpleNamespace(
        entity_id=entity_id, platform=platform, device_id=device_id,
        original_device_class=device_class, disabled_by=None,
        original_unit_of_measurement=unit, unit_of_measurement=unit,
        unique_id=entity_id.split(".", 1)[1],
    )


def _registry(entries):
    registry = MagicMock()
    registry.entities.values.return_value = entries
    return registry


def _discover(entries):
    with patch(
        "custom_components.solar_energy_management.hardware_detection."
        "entity_registry.async_get",
        return_value=_registry(entries),
    ):
        return discover_all_ev_chargers_from_registry(MagicMock())


# ── @bgthb's install, as his diagnostics reported it ──────────────────
# The OCPP integration creates one sensor per protocol measurand; the entity
# ids carry his charge point's name ("wallbox"), not the ``ocpp_`` prefix the
# glob matrix expects, which is why only the platform-scoped path ran.
_OCPP_FAMILY = [
    ("sensor.wallbox_status_connector", None),
    ("sensor.wallbox_power_active_import", "power"),
    ("sensor.wallbox_power_active_export", "power"),
    ("sensor.wallbox_power_offered", "power"),
    ("sensor.wallbox_energy_active_export_interval", "energy"),
    ("sensor.wallbox_energy_active_export_register", "energy"),
    ("sensor.wallbox_energy_active_import_register", "energy"),
    ("sensor.wallbox_energy_active_import_interval", "energy"),
    ("number.wallbox_maximum_current", "current"),
    ("switch.wallbox_charge_control", None),
]


def _reporter_entries(order=None):
    rows = _OCPP_FAMILY if order is None else order
    return [_entry(eid, "ocpp", "cp-1", dc) for eid, dc in rows]


class TestTheReportersCharger:
    def test_power_is_the_measurand_not_the_offer(self):
        charger = _discover(_reporter_entries())[0]
        assert charger["ev_charging_power_sensor"] == \
            "sensor.wallbox_power_active_import"

    def test_total_energy_is_the_import_register(self):
        charger = _discover(_reporter_entries())[0]
        assert charger["ev_total_energy_sensor"] == \
            "sensor.wallbox_energy_active_import_register"

    def test_the_rest_of_his_config_is_unchanged(self):
        charger = _discover(_reporter_entries())[0]
        assert charger["ev_connected_sensor"] == "sensor.wallbox_status_connector"
        assert charger["ev_charging_sensor"] == "sensor.wallbox_status_connector"
        assert charger["ev_current_control_entity"] == "number.wallbox_maximum_current"
        assert charger["ev_start_stop_entity"] == "switch.wallbox_charge_control"

    def test_registry_order_does_not_decide(self):
        """The bug WAS the ordering: reversing his registry must not move a
        single role."""
        forward = _discover(_reporter_entries())[0]
        backward = _discover(_reporter_entries(list(reversed(_OCPP_FAMILY))))[0]
        assert forward == backward

    def test_the_family_really_does_contain_the_trap(self):
        """No vacuous pass: the entity that used to win is present, is a
        power sensor, and is still rejected on its name."""
        eids = [e.entity_id for e in _reporter_entries()]
        assert "sensor.wallbox_power_offered" in eids
        assert not _measures_the_quantity("sensor.wallbox_power_offered")
        assert not _measures_the_quantity(
            "sensor.wallbox_energy_active_export_interval")
        assert _measures_the_quantity("sensor.wallbox_power_active_import")
        # The rule that was there, spelled out: device_class alone, last-wins
        # for power and first-wins for energy. On this family — his family,
        # in his order — it hands back exactly the two entities his
        # diagnostics carried, so these pins cannot pass vacuously.
        powers = [e for e, dc in _OCPP_FAMILY if dc == "power"]
        energies = [e for e, dc in _OCPP_FAMILY if dc == "energy"]
        assert powers[-1] == "sensor.wallbox_power_offered"
        assert energies[0] == "sensor.wallbox_energy_active_export_interval"


# ── the class, across every brand SEM discovers ───────────────────────
# One device per platform, each carrying a measured power sensor beside a
# capability twin and the V2G direction — the shape OCPP made visible and
# every measurand-named integration can produce.
def _every_platform_entries(reverse=False):
    entries = []
    for platform, _fn in _EV_CHARGER_PLATFORMS:
        dev = f"{platform}-1"
        rows = [
            (f"sensor.{platform}_power_offered", "power"),
            (f"sensor.{platform}_power_active_import", "power"),
            (f"sensor.{platform}_power_active_export", "power"),
            (f"sensor.{platform}_energy_active_export_register", "energy"),
            (f"sensor.{platform}_energy_total_import_register", "energy"),
            (f"sensor.{platform}_status", None),
            (f"number.{platform}_charging_current", "current"),
            (f"switch.{platform}_charge", None),
        ]
        entries += [_entry(eid, platform, dev, dc) for eid, dc in rows]
        entries.append(_entry(f"binary_sensor.{platform}_plug_connected",
                              platform, dev, "plug"))
        entries.append(_entry(f"binary_sensor.{platform}_charging",
                              platform, dev, "power"))
    return list(reversed(entries)) if reverse else entries


class TestNoBrandBindsACapability:
    def test_at_least_most_brands_actually_bite(self):
        """Non-vacuous: this registry must really produce chargers, or the
        invariant below would hold over an empty list."""
        assert len(_discover(_every_platform_entries())) >= 10

    def test_no_discovered_read_role_names_a_capability(self):
        for charger in _discover(_every_platform_entries()):
            for role in ("ev_charging_power_sensor", "ev_total_energy_sensor",
                         "ev_session_energy_sensor"):
                eid = charger.get(role)
                if eid:
                    assert _measures_the_quantity(eid), (
                        f"{charger.get('_platform')} bound {eid} as {role}")

    def test_the_answer_is_the_same_in_either_order(self):
        """Every brand, both directions: the measurand roles are a function
        of the entity ids, never of the order they were created in."""
        def _roles(chargers):
            return {
                c["_platform"]: {r: c.get(r) for r in _MEASURAND_ROLES}
                for c in chargers
            }
        assert _roles(_discover(_every_platform_entries())) == \
            _roles(_discover(_every_platform_entries(reverse=True)))

    def test_the_prober_does_not_suggest_one_either(self):
        candidates = probe_charger_candidates(
            registry=_registry(_every_platform_entries()))
        assert candidates
        for cand in candidates:
            eid = cand["roles"].get("ev_charging_power_sensor")
            assert eid is None or _measures_the_quantity(eid)

    def test_the_diagnostics_report_shows_what_sem_will_use(self):
        report = build_detection_report(
            MagicMock(), registry=_registry(_reporter_entries()))
        ocpp = [c for c in report["chargers"] if c.get("platform") == "ocpp"]
        assert ocpp
        mapped = ocpp[0]["mapped"]
        assert mapped["ev_charging_power_sensor"]["entity"] == \
            "sensor.wallbox_power_active_import"
        assert mapped["ev_total_energy_sensor"]["entity"] == \
            "sensor.wallbox_energy_active_import_register"


class TestSwapOnlyNeverDrop:
    """Removing the role LOOKS like the fail-closed move and is not one in
    this tree: a charger with no power entity is still registered (the retry
    path gates on the service, not the sensor), KEBA's adapter decides
    ``actual_charging`` from power alone, the 18-cycle ``ev_power < 50`` rule
    would anchor its SoC at 100 %, and in a multi-charger install the missing
    per-charger key falls back to the FLEET sum (class 3). So a name SEM
    merely finds suspicious must never cost a user their charger."""

    def test_a_capability_only_family_keeps_what_it_had(self):
        entities = [
            _entry("sensor.box_power_offered", "ocpp", "d1", "power", "W"),
            _entry("sensor.box_status_connector", "ocpp", "d1"),
        ]
        result = {"ev_charging_power_sensor": "sensor.box_power_offered"}
        _reject_capability_sensor(result, entities)
        assert result["ev_charging_power_sensor"] == "sensor.box_power_offered"

    def test_a_charger_on_a_device_named_max_keeps_all_three_roles(self):
        """HA builds object ids from the DEVICE NAME, which the owner picks.
        A KEBA on a box called "Max" carries the segment in every id."""
        entities = [
            _entry("binary_sensor.max_plug_connected", "keba", "d1", "plug"),
            _entry("sensor.max_charging_power", "keba", "d1", "power", "W"),
            _entry("sensor.max_total_energy", "keba", "d1", "energy", "kWh"),
            _entry("sensor.max_session_energy", "keba", "d1", "energy", "kWh"),
        ]
        charger = _discover(entities)[0]
        assert charger["ev_charging_power_sensor"] == "sensor.max_charging_power"
        assert charger["ev_total_energy_sensor"] == "sensor.max_total_energy"
        assert charger["ev_session_energy_sensor"] == "sensor.max_session_energy"

    def test_an_ocpp_box_that_only_offers_still_reports_a_power_entity(self):
        """The brand function filters its family; an empty filter must fall
        back, not unbind."""
        entities = [
            _entry("sensor.cp_status_connector", "ocpp", "d1"),
            _entry("sensor.cp_power_offered", "ocpp", "d1", "power", "W"),
            _entry("number.cp_maximum_current", "ocpp", "d1", "current", "A"),
        ]
        charger = _discover(entities)[0]
        assert charger["ev_charging_power_sensor"] == "sensor.cp_power_offered"


class TestTheReplacementIsCommensurable:
    def test_a_status_string_is_never_the_replacement(self):
        """Zaptec's custom builds omit ``device_class``; without a unit check
        the family becomes "every sensor with no device class"."""
        entities = [
            _entry("sensor.zaptec_go_max_charge_power", "zaptec", "d1",
                   None, "W"),
            _entry("sensor.zaptec_go_charger_operation_mode", "zaptec", "d1"),
            _entry("sensor.zaptec_go_humidity", "zaptec", "d1", None, "%"),
        ]
        result = {"ev_charging_power_sensor": "sensor.zaptec_go_max_charge_power"}
        _reject_capability_sensor(result, entities)
        assert result["ev_charging_power_sensor"] == \
            "sensor.zaptec_go_max_charge_power"

    def test_a_phase_leg_is_not_a_replacement(self):
        """A third of the truth is not a fallback for the truth."""
        entities = [
            _entry("sensor.wb_max_power", "wallbox", "d1", "power", "W"),
            _entry("sensor.wb_power_l1", "wallbox", "d1", "power", "W"),
            _entry("sensor.wb_power_l2", "wallbox", "d1", "power", "W"),
            _entry("sensor.wb_power_phase_3", "wallbox", "d1", "power", "W"),
        ]
        result = {"ev_charging_power_sensor": "sensor.wb_max_power"}
        _reject_capability_sensor(result, entities)
        assert result["ev_charging_power_sensor"] == "sensor.wb_max_power"

    def test_the_total_and_session_roles_never_collapse(self):
        """#698: ha_energy_reader de-duplicates the pair — detection must not
        re-introduce it."""
        entities = [
            _entry("sensor.box_energy_target", "ocpp", "d1", "energy", "kWh"),
            _entry("sensor.box_session_energy", "ocpp", "d1", "energy", "kWh"),
        ]
        result = {"ev_total_energy_sensor": "sensor.box_energy_target",
                  "ev_session_energy_sensor": "sensor.box_session_energy"}
        _reject_capability_sensor(result, entities)
        assert result["ev_total_energy_sensor"] != result["ev_session_energy_sensor"]
        assert result["ev_session_energy_sensor"] == "sensor.box_session_energy"

    def test_a_session_counter_beats_a_metering_interval_delta(self):
        """``Energy.Active.Import.Interval`` is the delta over the last
        METERING interval, not the session — the glob table says so too."""
        entities = [
            _entry("sensor.cp_energy_export_register", "ocpp", "d1",
                   "energy", "kWh"),
            _entry("sensor.cp_energy_active_import_interval", "ocpp", "d1",
                   "energy", "kWh"),
            _entry("sensor.cp_session_energy", "ocpp", "d1", "energy", "kWh"),
        ]
        result = {"ev_session_energy_sensor": "sensor.cp_energy_export_register"}
        _reject_capability_sensor(result, entities)
        assert result["ev_session_energy_sensor"] == "sensor.cp_session_energy"


class TestTheChoiceIsAFunctionOfTheName:
    """The bug was that ordering decided. These pin the two terms that make
    the choice total — remove either and the registry decides again."""

    def _pick(self, candidates, bound="sensor.box_power_offered"):
        entities = [_entry(bound, "ocpp", "d1", "power", "W")] + [
            _entry(c, "ocpp", "d1", "power", "W") for c in candidates]
        result = {"ev_charging_power_sensor": bound}
        _reject_capability_sensor(result, entities)
        return result["ev_charging_power_sensor"]

    def test_the_entity_id_breaks_a_tie_not_the_order(self):
        both = ["sensor.box_power_zulu", "sensor.box_power_alpha"]
        assert self._pick(both) == "sensor.box_power_alpha"
        assert self._pick(list(reversed(both))) == "sensor.box_power_alpha"

    def test_the_window_outranks_the_direction(self):
        """A lifetime register in the session slot is a different mistake
        from the one this guard exists to fix: the window a role asks for
        must not be traded away for a nicer-looking direction."""
        entities = [
            _entry("sensor.box_energy_export_register", "ocpp", "d1",
                   "energy", "kWh"),
            _entry("sensor.box_energy_active_import", "ocpp", "d1",
                   "energy", "kWh"),
            _entry("sensor.box_energy_total", "ocpp", "d1", "energy", "kWh"),
        ]
        result = {"ev_total_energy_sensor": "sensor.box_energy_export_register"}
        _reject_capability_sensor(result, entities)
        assert result["ev_total_energy_sensor"] == "sensor.box_energy_total"

    def test_the_import_direction_outranks_alphabetical_order(self):
        both = ["sensor.box_power_a", "sensor.box_power_active_import"]
        assert self._pick(both) == "sensor.box_power_active_import"
        assert self._pick(list(reversed(both))) == "sensor.box_power_active_import"


class TestWhatCountsAsACapability:
    """A literal list, not a loop over the constant: shrinking the segment
    set must fail here rather than quietly shrink the test with it."""

    REJECTED = (
        "sensor.box_power_offered", "sensor.box_offer_power",
        "sensor.box_power_limit", "sensor.box_power_limits",
        "sensor.box_max_power", "sensor.box_maximum_power",
        "sensor.box_maximal_power", "sensor.box_maximale_leistung_power",
        "sensor.box_rated_power", "sensor.box_nominal_power",
        "sensor.box_capacity_power", "sensor.box_available_power",
        "sensor.box_setpoint_power", "sensor.box_target_power",
        "sensor.box_allowed_power",
        "sensor.box_power_active_export", "sensor.box_energy_exported",
        "sensor.box_power_reactive_import",
    )

    def test_each_of_these_names_is_a_capability_or_the_wrong_quantity(self):
        for eid in self.REJECTED:
            assert not _measures_the_quantity(eid), eid

    def test_a_substring_is_not_a_word(self):
        """Class 67: 'rated' lives inside 'generated', 'max' inside
        'maximum' — the rules read segments."""
        assert _measures_the_quantity("sensor.senec_solar_generated_power")
        assert _measures_the_quantity("sensor.keba_p30_charging_power")
        assert _measures_the_quantity("sensor.wallbox_power_active_import")


class TestTheOtherTwoPaths:
    def test_the_near_miss_offer_falls_away_with_its_control(self):
        """The offer is built AROUND a control; if a guard takes it, the
        honest answer is "please report", not a charger with no throttle."""
        dev = [
            _entry("number.jb_max_current_offline_wanted", "mqtt", "d1",
                   "current", "A"),
            _entry("sensor.jb_power", "mqtt", "d1", "power", "W"),
            _entry("binary_sensor.jb_plug", "mqtt", "d1", "plug"),
        ]
        proposed = {"ev_current_control": {
            "entity": "number.jb_max_current_offline_wanted"}}
        assert charger_from_near_miss(dev, "mqtt", proposed) == {}

    def test_the_near_miss_offer_survives_a_clean_control(self):
        dev = [
            _entry("number.jb_charging_current", "mqtt", "d1", "current", "A"),
            _entry("sensor.jb_power", "mqtt", "d1", "power", "W"),
            _entry("binary_sensor.jb_plug", "mqtt", "d1", "plug"),
        ]
        proposed = {"ev_current_control": {"entity": "number.jb_charging_current"}}
        offer = charger_from_near_miss(dev, "mqtt", proposed)
        assert offer["ev_charging_power_sensor"] == "sensor.jb_power"


class TestNoRegression:
    def test_a_plain_single_power_sensor_is_untouched(self):
        entities = [_entry("sensor.keba_p30_power", "keba", "d1", "power", "W")]
        result = {"ev_charging_power_sensor": "sensor.keba_p30_power"}
        apply_charger_discovery_guards(result, entities)
        assert result["ev_charging_power_sensor"] == "sensor.keba_p30_power"

    def test_an_unknown_entity_is_left_alone(self):
        """A role pointing outside the family we were handed is left alone —
        a swap there would be a guess of its own."""
        result = {"ev_charging_power_sensor": "sensor.somewhere_else_offered"}
        _reject_capability_sensor(result, [])
        assert result["ev_charging_power_sensor"] == "sensor.somewhere_else_offered"


def _e(entity_id, device_class=None):
    """One OCPP entity on one device, in the shape ``_entry`` builds."""
    return _entry(entity_id, "ocpp", "dev-ocpp", device_class)


class TestBothWordOrders:
    """(review of a parallel #962 build, 15.09) OCPP writes the qualifier LAST
    (`power_offered`, `power_reactive_import`); most other brands write it
    FIRST (`offered_power`, `reactive_power`). The guard matches on the word,
    not on a compound token — pinned so it cannot regress into one."""

    def test_offered_power_adjective_first_is_swapped_for_the_measurement(self):
        ents = [
            _e("sensor.box_offered_power", "power"),
            _e("sensor.box_charging_power", "power"),
        ]
        m = {"ev_charging_power_sensor": "sensor.box_offered_power"}
        apply_charger_discovery_guards(m, ents)
        assert m["ev_charging_power_sensor"] == "sensor.box_charging_power"

    def test_reactive_power_adjective_first_is_never_the_better_sibling(self):
        """A disqualified binding must not be repaired INTO a different wrong one."""
        ents = [
            _e("sensor.box_power_offered", "power"),
            _e("sensor.box_reactive_power", "power"),
            _e("sensor.box_power_active_import", "power"),
        ]
        m = {"ev_charging_power_sensor": "sensor.box_power_offered"}
        apply_charger_discovery_guards(m, ents)
        assert m["ev_charging_power_sensor"] == "sensor.box_power_active_import"

    def test_the_reporters_exact_registry_order(self):
        """@bgthb's device, in the order that reproduced BOTH wrong bindings on
        the unfixed code: offered listed last (last-wins power), export_interval
        listed first (first-wins energy)."""
        ents = [
            _e("sensor.wallbox_status_connector"),
            _e("sensor.wallbox_energy_active_export_interval", "energy"),
            _e("sensor.wallbox_energy_active_import_register", "energy"),
            _e("sensor.wallbox_power_active_import", "power"),
            _e("sensor.wallbox_power_reactive_import", "power"),
            _e("sensor.wallbox_power_offered", "power"),
            _e("number.wallbox_maximum_current", "current"),
            _e("switch.wallbox_charge_control"),
        ]
        from custom_components.solar_energy_management.hardware_detection import _discover_ocpp
        m = _discover_ocpp(ents)
        apply_charger_discovery_guards(m, ents)
        assert m["ev_charging_power_sensor"] == "sensor.wallbox_power_active_import"
        assert m["ev_total_energy_sensor"] == "sensor.wallbox_energy_active_import_register"
