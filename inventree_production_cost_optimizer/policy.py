from __future__ import annotations

import re
from decimal import Decimal

D = Decimal

# Recovered verbatim from the existing estimating-allocation scripts.
PASSIVE_FOOTPRINT_SPILLAGE = {
    "0201": 200,
    "0402": 100,
    "0603": 50,
    "0805": 25,
    "1206": 20,
    "1210": 20,
}


def dec(value, default="0") -> Decimal:
    try:
        if value is None or value == "":
            return D(default)
        return D(str(value))
    except Exception:
        return D(default)

def fmt_decimal(value) -> str:
    """Display decimals without unnecessary trailing zeroes."""
    value = dec(value)
    if value == value.to_integral():
        return str(value.quantize(D("1")))
    return format(value.normalize(), "f")




def select_effective_price(part_pricing_max, stock_unit_price):
    """Choose the live price used by the spillage policy."""
    part_price = dec(part_pricing_max)
    stock_price = dec(stock_unit_price)

    if part_price > 0:
        return {"effective_price": part_price, "price_source": "part_pricing_max"}
    if stock_price > 0:
        return {"effective_price": stock_price, "price_source": "stock_item_unit_price"}
    return {"effective_price": D("0"), "price_source": "missing_price_fallback"}




def compact_tracking_note(parts, max_length=512):
    """Build a compact note which never exceeds max_length."""
    cleaned = [str(x or "").strip() for x in parts if str(x or "").strip()]
    note = " | ".join(cleaned)
    if len(note) <= max_length:
        return note
    suffix = " | ..."
    return note[: max_length - len(suffix)].rstrip(" |") + suffix




def parse_transient_location_patterns(value):
    """Normalize configured transient-location match strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = str(value).split(",")
    return [str(x).strip().casefold() for x in raw if str(x).strip()]


def location_is_transient(path, patterns):
    """Return True when a location path matches a configured transient pattern."""
    text = str(path or "").casefold()
    return any(pattern in text for pattern in parse_transient_location_patterns(patterns))


def recommend_return_location(history, patterns):
    """Return the newest non-transient location from newest-first history."""
    for row in history or []:
        if not location_is_transient(row.get("path") or row.get("name"), patterns):
            return row
    return None


def normalize_footprint(value: str) -> str:
    """Normalize package text to the footprint tokens used by the legacy engine."""
    text = str(value or "").strip().upper()
    compact = re.sub(r"[^A-Z0-9]", "", text)
    for fp in PASSIVE_FOOTPRINT_SPILLAGE:
        if fp in compact:
            return fp
    return compact


def category_is_basic_passive(part_category: str) -> bool:
    """Backward-compatible passive check for a single text value."""
    return is_basic_passive_identity(part_category)


def is_basic_passive_identity(*values) -> bool:
    """Identify resistor / capacitor / inductor / generic passive identity text.

    This intentionally accepts multiple operator-visible Part fields because
    production data does not always place passives in a dedicated Part Category.
    """
    text = " ".join(str(value or "") for value in values).casefold()
    return any(token in text for token in ("resistor", "capacitor", "inductor", "passive"))


def spillage_per_project(case_package: str, pricing_max, part_category: str = "", passive_hint=None):
    """Return the per-project spillage / overage allowance.

    Priority order:
      1. Basic passive with effective unit price >= 0.50 -> 20 pieces,
         regardless of footprint.
      2. Existing passive-footprint rules.
      3. Existing price-band rules.
      4. Existing missing-price fallbacks.
    """
    fp = normalize_footprint(case_package)
    price = dec(pricing_max)
    passive = category_is_basic_passive(part_category) if passive_hint is None else bool(passive_hint)

    # Highest-priority real-world rule: higher-value passives get a capped
    # allowance independent of package size.
    if passive and price >= D("0.5"):
        return D(20), "passive_price_ge_0_50_cap_20"

    if price <= 0:
        if passive and fp in PASSIVE_FOOTPRINT_SPILLAGE:
            return D(PASSIVE_FOOTPRINT_SPILLAGE[fp]), f"missing_price_passive_footprint_{fp}"
        if passive:
            return D(5), "missing_price_passive_unknown_footprint_default_5"
        return D(5), "missing_price_non_passive_default_5"

    if fp in PASSIVE_FOOTPRINT_SPILLAGE:
        return D(PASSIVE_FOOTPRINT_SPILLAGE[fp]), f"footprint_{fp}"

    if price > 200:
        return D(0), "price_over_200"
    if price > 50:
        return D(1), "price_50_to_200"
    if price > 10:
        return D(2), "price_10_to_50"
    return D(5), "price_10_or_less"



