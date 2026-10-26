"""Helper utility functions for Solar Energy Management.

This module contains pure utility functions that have no dependencies
on the coordinator or other components.
"""
import logging
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)


def safe_float(value: Any, default: float = 0) -> float:
    """Safely convert value to float, handling 'unavailable' and other invalid states.

    Args:
        value: Value to convert to float
        default: Default value if conversion fails

    Returns:
        Float value or default

    Examples:
        >>> safe_float("123.45")
        123.45
        >>> safe_float("unavailable", 0)
        0
        >>> safe_float(None, 100)
        100
    """
    if value in (None, "unknown", "unavailable", "None"):
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def safe_format(value: Any, format_spec: str = ":.1f", fallback: str = "--") -> str:
    """Safely format a value, handling None cases.

    Args:
        value: Value to format
        format_spec: Python format specification (default: ":.1f")
        fallback: Fallback string if formatting fails (default: "--")

    Returns:
        Formatted string or fallback

    Examples:
        >>> safe_format(123.456, ":.2f")
        '123.46'
        >>> safe_format(None)
        '--'
        >>> safe_format("invalid", ":.1f", "N/A")
        'N/A'
    """
    if value is None:
        return fallback
    try:
        return f"{value:{format_spec}}"
    except (ValueError, TypeError):
        return fallback


def convert_power_to_watts(value: float, unit: str, sensor_name: str = "") -> float:
    """Convert power value to watts based on unit, with standardized handling.

    Args:
        value: Power value to convert
        unit: Unit of measurement (e.g., "W", "kW")
        sensor_name: Name of sensor (used for EV detection heuristic)

    Returns:
        Power value in watts

    Examples:
        >>> convert_power_to_watts(5.2, "kW", "solar_power")
        5200.0
        >>> convert_power_to_watts(1500, "W", "home_power")
        1500.0
        >>> convert_power_to_watts(11.5, "unknown", "ev_charging_power")
        11500.0
    """
    unit_lower = unit.lower() if unit else ""

    if unit_lower in ["kw", "kilowatt", "kilowatts"]:
        return value * 1000  # Convert kW to W
    elif unit_lower in ["w", "watt", "watts"]:
        return value  # Already in watts
    else:
        # Default assumption: most power sensors are in W
        # Special case for EV sensors: default to kW conversion if unit not specified or unknown
        if "ev" in sensor_name.lower() or "keba" in sensor_name.lower():
            if unit:
                _LOGGER.warning(
                    f"EV power sensor {sensor_name} has unknown unit '{unit}', assuming kW"
                )
            return value * 1000  # EV sensors default to kW
        else:
            if unit:
                _LOGGER.warning(
                    f"Power sensor {sensor_name} has unknown unit '{unit}', assuming W"
                )
            return value  # Default to watts for non-EV sensors


def solar_share_pct(solar: float, *others: float) -> Optional[float]:
    """(#1046) Solar's percent of what the flows say came in.

    Top and bottom both come from the flows. A meter total (the charger's
    kWh, the battery's measured charge) also counts cycles the flows
    missed — an inverter that reads dark leaves the draw unassigned — so a
    flow divided by it reads solar low: 49 % on PROD for an 89 % solar day.
    ``others`` are the non-solar flows into the same thing. None when the
    flows hold nothing: no split is known.
    """
    solar = max(0.0, solar or 0.0)
    total = solar + sum(max(0.0, x or 0.0) for x in others)
    if total <= 0:
        return None
    return solar / total * 100.0


def battery_export_part(export: float, battery_to_grid: float) -> float:
    """(#1191) The battery's part of one read's export: what the flows sent
    from the pack to the grid, never more than the export itself.

    The allocator serves solar first, so export it left unassigned (a read
    that does not add up) stays solar's — the rule before #1191.
    """
    return min(max(0.0, export or 0.0), max(0.0, battery_to_grid or 0.0))


def solar_export(export: float, battery_part: float) -> float:
    """(#1191) What of an export the sun made: the export less the battery's
    part.

    Self-consumption, CO2 avoided and "% of solar exported" take this away
    from solar. A battery sale was never part of today's solar, so taking
    the whole export away read a sunless morning's sale as solar sent out:
    the rate went below 0 % and the health check fired.
    """
    return max(0.0, (export or 0.0) - max(0.0, battery_part or 0.0))
