"""(#1083) The Simulation tab's websocket commands.

The card asks over these; nothing rides on an entity attribute (#979).
Every result is modelled and says so (``"modelled": True``).
"""
from __future__ import annotations

import base64
import datetime as dt
from typing import Any, Optional

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .analytics.simulation.battery_sim import SimParams
from .analytics.simulation.ev_retime import EV_MODE_CHOICES, RECORDED
from .consts.battery_modes import BATTERY_MODES
from .analytics.simulation.summary import SUMMARY_TAGS
from .const import DOMAIN
from .consts.bounds import BOUNDS, Range
from .coordinator.simulation_job import SimulationError
from .utils.xlsx import write_workbook

WS_PREFIX = f"{DOMAIN}/simulation"
WS_DAYS = f"{WS_PREFIX}/days"
WS_DEFAULTS = f"{WS_PREFIX}/defaults"
WS_START = f"{WS_PREFIX}/start"
WS_STATUS = f"{WS_PREFIX}/status"
WS_CANCEL = f"{WS_PREFIX}/cancel"
WS_RESULT = f"{WS_PREFIX}/result"
WS_XLSX = f"{WS_PREFIX}/xlsx"

ERR_BUSY = "busy"
ERR_INVALID = "invalid"

def _number(value):
    """A JSON number; a boolean is not one (True would be 1 kWh)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise vol.Invalid("expected a number")
    return value


_NUMBER = _number
_RATE = vol.Any("recorded", _number)
# A what-if may try a bigger pack than the setup allows (100 kWh): a car
# battery used as home storage reaches ~150 kWh. Same step and floor as the
# setup row; the Simulation card holds the same maximum.
SIM_CAPACITY = Range(BOUNDS["battery_capacity_kwh"].min, 150,
                     step=BOUNDS["battery_capacity_kwh"].step, unit="kWh")
_CAP = SIM_CAPACITY
#: what-if battery power (W) and PV scaling the form may ask for
SIM_POWER = Range(0, 50000, step=100, unit="W")
SIM_PV_FACTOR = Range(0, 5, step=0.05, unit="x")
EST = " (est.)"


def _job(hass: HomeAssistant, entry_id: Optional[str]):
    entries = hass.config_entries.async_entries(DOMAIN)
    if entry_id:
        entries = [e for e in entries if e.entry_id == entry_id]
    entry = entries[0] if entries else None
    coordinator = getattr(entry, "runtime_data", None) if entry else None
    return getattr(coordinator, "_simulation_job", None)


def _no_job(connection, msg) -> None:
    connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "no SEM entry")


def _params(job, raw: dict) -> SimParams:
    """The form's values on top of the install's config."""
    cap = float(raw["capacity_kwh"])
    reserve = float(raw["reserve_pct"])
    peak = raw.get("peak_limit_kw")
    peak = float(peak) if peak is not None else None
    if not _CAP.min <= cap <= _CAP.max:
        raise SimulationError(
            ERR_INVALID, f"capacity must be {_CAP.min:g}–{_CAP.max:g} kWh")
    if not 0 <= reserve <= 100:
        raise SimulationError(ERR_INVALID, "reserve must be 0–100 %")
    if peak is not None and peak < 0:
        raise SimulationError(ERR_INVALID, "peak limit must be 0 kW or more")
    rates = {}
    for key in ("import_rate", "export_rate"):
        val = raw.get(key, "recorded")
        rates[key] = val if val == "recorded" else float(val)
    cfg = job.install_config
    power = {}
    for key in ("max_charge_w", "max_discharge_w"):
        val = raw.get(key)
        if val is None:
            continue
        val = float(val)
        if not SIM_POWER.min <= val <= SIM_POWER.max:
            raise SimulationError(
                ERR_INVALID, f"battery power must be 0–{SIM_POWER.max:g} W")
        power[key] = val
    pv = _pv_factor(cfg, raw)
    to_ev = raw.get("battery_to_ev")
    mode = raw.get("battery_mode")
    # the configured mode is no change: None keeps the own settings equal
    if not mode or mode == str(cfg.get("battery_mode") or "auto"):
        mode = None
    return SimParams.from_config(cfg, capacity_kwh=cap,
                                 reserve_pct=reserve,
                                 peak_limit_kw=peak or None,
                                 battery_mode=mode,
                                 ev_mode=str(raw.get("ev_mode") or RECORDED),
                                 pv_factor=pv,
                                 # only "off" changes anything: on is how
                                 # the install already runs
                                 battery_to_ev=False if to_ev is False else None,
                                 **power, **rates)


def _pv_factor(cfg: dict, raw: dict) -> float:
    """The solar scale: a factor, or extra kWp over the install's size
    (``system_size_kwp``) — (kWp + extra) ÷ kWp. Extra kWp without a known
    size cannot be turned into a factor: the form then asks for one."""
    factor = raw.get("pv_factor")
    extra = raw.get("pv_extra_kwp")
    if extra is not None and float(extra) != 0.0:
        try:
            kwp = float(cfg.get("system_size_kwp") or 0.0)
        except (TypeError, ValueError):
            kwp = 0.0
        if kwp <= 0:
            raise SimulationError(
                ERR_INVALID, "the PV size is not known — give a factor instead")
        factor = (kwp + float(extra)) / kwp
    factor = 1.0 if factor is None else float(factor)
    if not SIM_PV_FACTOR.min <= factor <= SIM_PV_FACTOR.max:
        raise SimulationError(
            ERR_INVALID, f"the solar factor must be 0–{SIM_PV_FACTOR.max:g}")
    return factor


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): WS_DAYS,
    vol.Optional("entry_id"): str,
})
@callback
def ws_days(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    """The tagged days on offer."""
    job = _job(hass, msg.get("entry_id"))
    if job is None:
        _no_job(connection, msg)
        return
    connection.send_result(msg["id"], job.days())


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): WS_DEFAULTS,
    vol.Optional("entry_id"): str,
})
@callback
def ws_defaults(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    """The form's current values."""
    job = _job(hass, msg.get("entry_id"))
    if job is None:
        _no_job(connection, msg)
        return
    connection.send_result(msg["id"], job.defaults())


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): WS_START,
    vol.Optional("entry_id"): str,
    # one continuous period (the card sends this) — or a list of days
    vol.Optional("days"): [str],
    vol.Optional("from"): str,
    vol.Optional("to"): str,
    vol.Required("params"): {
        vol.Required("capacity_kwh"): _NUMBER,
        vol.Required("reserve_pct"): _NUMBER,
        vol.Optional("peak_limit_kw"): vol.Any(None, _NUMBER),
        vol.Optional("import_rate", default="recorded"): _RATE,
        vol.Optional("export_rate", default="recorded"): _RATE,
        vol.Optional("battery_mode"): vol.Any(None, vol.In(list(BATTERY_MODES))),
        vol.Optional("ev_mode", default=RECORDED): vol.In(list(EV_MODE_CHOICES)),
        vol.Optional("max_charge_w"): vol.Any(None, _NUMBER),
        vol.Optional("max_discharge_w"): vol.Any(None, _NUMBER),
        vol.Optional("pv_factor"): vol.Any(None, _NUMBER),
        vol.Optional("pv_extra_kwp"): vol.Any(None, _NUMBER),
        vol.Optional("battery_to_ev"): vol.Any(None, bool),
    },
    vol.Optional("include_gaps", default=False): bool,
})
@callback
def ws_start(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    """Start a run: ``{"run_id"}``, or the error ``busy`` / ``invalid``.

    ``from`` + ``to`` is the period the card sends: every stored day in it
    runs as one chain. ``days`` (a list) is still taken."""
    job = _job(hass, msg.get("entry_id"))
    if job is None:
        _no_job(connection, msg)
        return
    try:
        params = _params(job, msg["params"])
        if msg.get("from") or msg.get("to"):
            if not (msg.get("from") and msg.get("to")):
                raise SimulationError(ERR_INVALID, "a period needs from and to")
            run_id = job.start_period(msg["from"], msg["to"], params)
        elif msg.get("days"):
            run_id = job.start(msg["days"], params,
                               include_gaps=bool(msg.get("include_gaps")))
        else:
            raise SimulationError(ERR_INVALID, "pick a period")
    except SimulationError as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    connection.send_result(msg["id"], {"run_id": run_id})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): WS_STATUS,
    vol.Optional("entry_id"): str,
})
@callback
def ws_status(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    """The progress of the current (or last) run."""
    job = _job(hass, msg.get("entry_id"))
    if job is None:
        _no_job(connection, msg)
        return
    connection.send_result(msg["id"], dict(job.progress))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): WS_CANCEL,
    vol.Optional("entry_id"): str,
})
@callback
def ws_cancel(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    """Stop the running run; what ran is kept, flagged."""
    job = _job(hass, msg.get("entry_id"))
    if job is None:
        _no_job(connection, msg)
        return
    connection.send_result(msg["id"], {"cancelled": job.cancel()})


def result_payload(run: dict) -> dict:
    """A stored run, flat, marked modelled."""
    out = {k: v for k, v in run.items() if k != "result"}
    out.update(run.get("result") or {})
    out["modelled"] = True
    return out


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): WS_RESULT,
    vol.Optional("entry_id"): str,
    vol.Optional("run_id"): str,
})
@callback
def ws_result(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    """A stored run (the latest when no ``run_id``)."""
    job = _job(hass, msg.get("entry_id"))
    if job is None:
        _no_job(connection, msg)
        return
    run = job.run(msg.get("run_id"))
    if run is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "no such run")
        return
    connection.send_result(msg["id"], result_payload(run))


def _day(value: str):
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        return value


#: the Days sheet's energy columns: (row key stem, header)
_DAY_FLOWS = (("grid_import", "Grid import kWh"), ("grid_export", "Grid export kWh"),
              ("battery_charge", "Battery charge kWh"),
              ("battery_discharge", "Battery discharge kWh"))
#: the Totals sheet's energy lines: (key, header)
_ENERGY = (("grid_import_kwh", "Grid import kWh"),
           ("grid_export_kwh", "Grid export kWh"),
           ("self_consumption_pct", "Self-consumption %"),
           ("battery_charge_kwh", "Battery charge kWh"),
           ("battery_discharge_kwh", "Battery discharge kWh"),
           ("cycles", "Battery cycles"))


def run_xlsx(run: dict) -> bytes:
    """Four sheets: one row per day (with its energy and why it differs),
    the difference per month, the totals per day type and actual against
    simulated energy, and notes."""
    res = run.get("result") or {}
    cur = res.get("currency") or ""
    money = f" ({cur})" if cur else ""
    head = ["Date", "Tags", f"Actual cost{money}",
            f"Simulated cost{money}{EST}", f"Delta{money}{EST}",
            f"Delta incl. what the pack gained{money}{EST}"]
    for _key, label in _DAY_FLOWS:
        head += [f"{label}, actual", f"{label}, simulated{EST}"]
    head += ["Pack at day end kWh, actual", f"Pack at day end kWh, simulated{EST}",
             f"Car kWh moved{EST}", f"Car kWh not delivered{EST}",
             "Indicative", "Note"]
    days = [head]
    for row in res.get("days") or []:
        line = [_day(row.get("date")), ", ".join(row.get("tags") or []),
                row.get("actual_cost"), row.get("simulated_cost"),
                row.get("delta"), row.get("net_delta", row.get("delta"))]
        for key, _label in _DAY_FLOWS:
            line += [row.get(f"actual_{key}_kwh"), row.get(f"simulated_{key}_kwh")]
        line += [row.get("actual_end_kwh"), row.get("simulated_end_kwh"),
                 row.get("ev_moved_kwh", 0.0), row.get("ev_not_delivered_kwh", 0.0),
                 bool(row.get("indicative")),
                 "; ".join(row.get("story") or [])]
        days.append(line)
    months = [["Month", "Days", f"Actual cost{money}", f"Simulated cost{money}{EST}",
               f"Delta{money}{EST}", f"Delta incl. what the pack gained{money}{EST}"]]
    for m in res.get("months") or []:
        months.append([m.get("month"), m.get("days"), m.get("actual"),
                       m.get("simulated"), m.get("delta"), m.get("net_delta")])
    totals = [["Day type", "Days", f"Actual cost{money}",
               f"Simulated cost{money}{EST}", f"Delta{money}{EST}"]]
    per_tag = res.get("per_tag") or {}
    for tag in SUMMARY_TAGS:
        t = per_tag.get(tag)
        if t and t.get("days"):
            totals.append([tag, t["days"], t["actual"], t["simulated"], t["delta"]])
    t = res.get("total") or {}
    stored = res.get("stored_energy") or {}
    totals.append(["total", t.get("days", 0), t.get("actual", 0.0),
                   t.get("simulated", 0.0), t.get("delta", 0.0)])
    totals.append([])
    totals.append([f"Net difference incl. stored energy{money}{EST}", None, None,
                   None, t.get("net_delta", t.get("delta", 0.0))])
    energy = res.get("energy") or {}
    act, sim = energy.get("actual") or {}, energy.get("simulated") or {}
    if act or sim:
        totals.append([])
        totals.append(["Energy", None, "Actual", f"Simulated{EST}"])
        totals.append([f"Cost{money}", None, act.get("cost"), sim.get("cost")])
        for key, label in _ENERGY:
            totals.append([label, None, act.get(key), sim.get(key)])
    calib = res.get("calibration") or {}
    ev = res.get("ev") or {}
    period = res.get("period") or {}
    notes = [["What", "Value"],
             ["Period", f"{period.get('from', '')} – {period.get('to', '')}".strip(" –")],
             ["How the days run",
              "One chain: the battery carries its charge from day to day. A gap "
              "of up to two days is bridged; a longer gap starts again from the "
              "recorded level."],
             ["Stretches (a new one after a longer gap)", stored.get("stretches")],
             [f"Extra energy left in the pack at stretch ends, summed, kWh{EST}",
              stored.get("kwh")],
             [f"Extra energy left in the pack at stretch ends, summed{money}{EST}",
              stored.get("value")],
             [f"Net difference incl. stored energy{money}{EST}",
              t.get("net_delta", t.get("delta"))],
             ["Own settings against the real cost, % off", calib.get("error_pct")],
             ["Own settings reproduce the real cost", calib.get("calibrated")],
             [f"Car kWh moved{EST}", ev.get("moved_kwh")],
             [f"Car kWh not delivered{EST}", ev.get("not_delivered_kwh")]]
    return write_workbook([("Days", days), ("Months", months),
                           ("Totals", totals), ("Notes", notes)])


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): WS_XLSX,
    vol.Optional("entry_id"): str,
    vol.Optional("run_id"): str,
})
@callback
def ws_xlsx(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    """A stored run as an .xlsx, base64."""
    job = _job(hass, msg.get("entry_id"))
    if job is None:
        _no_job(connection, msg)
        return
    run = job.run(msg.get("run_id"))
    if run is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "no such run")
        return
    stamp = str(run.get("started_at") or "")[:10] or "run"
    connection.send_result(msg["id"], {
        "filename": f"sem-simulation-{stamp}.xlsx",
        "base64": base64.b64encode(run_xlsx(run)).decode("ascii"),
    })


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    """Register the commands. Idempotent: a reload overwrites the same slot."""
    for handler in (ws_days, ws_defaults, ws_start, ws_status, ws_cancel,
                    ws_result, ws_xlsx):
        websocket_api.async_register_command(hass, handler)


