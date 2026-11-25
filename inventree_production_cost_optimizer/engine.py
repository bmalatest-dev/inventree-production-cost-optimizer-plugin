"""Pure, deterministic inventory/BOM planner. Does not write to InvenTree."""

from collections import defaultdict, deque
from decimal import Decimal, ROUND_CEILING


def dec(value):
    return Decimal(str(value or 0))


def ceil(value):
    return dec(value).to_integral_value(rounding=ROUND_CEILING)


def plan(root, quantity, bom, stock, prices):
    """Expand one production run, consuming shared available stock only once.

    bom: parent -> [{part, quantity, attrition, setup_quantity, rounding_multiple}]
    stock: part -> available units; prices: part -> (unit CAD cost, provenance)
    """
    quantity = int(quantity)
    if quantity <= 0:
        raise ValueError("Build quantity must be positive")
    available = {key: dec(value) for key, value in stock.items()}
    demand = defaultdict(Decimal, {root: dec(quantity)})
    purchased = defaultdict(Decimal)
    consumed = defaultdict(Decimal)
    warnings = []
    # Topological order ensures a shared subassembly reached by different
    # branches/depths is allocated once, after all parent demands are combined.
    graph = {}
    incoming = defaultdict(int)
    seen = {root}
    pending = [root]
    while pending and len(seen) <= 10000:
        parent = pending.pop()
        children = [line for line in bom.get(parent, []) if not line.get("optional")]
        graph[parent] = children
        for line in children:
            child = line["part"]
            incoming[child] += 1
            if child not in seen:
                seen.add(child)
                pending.append(child)
    if pending:
        warnings.append("BOM exceeds 10,000 distinct parts")
    if incoming[root]:
        warnings.append("Circular BOM reference detected")
    queue = deque([root])
    processed = set()
    while queue:
        part_id = queue.popleft()
        if part_id in processed:
            warnings.append(f"Circular BOM reference detected at {part_id}")
            continue
        processed.add(part_id)
        required = demand[part_id]
        if part_id == root:
            build = required  # Target is newly manufactured units.
        else:
            used = min(required, max(Decimal(0), available.get(part_id, Decimal(0))))
            available[part_id] = available.get(part_id, Decimal(0)) - used
            consumed[part_id] += used
            build = required - used
        lines = graph.get(part_id, [])
        if build > 0 and not lines:
            purchased[part_id] += build
        for line in lines:
            child = line["part"]
            if build > 0:
                per = dec(line.get("quantity", 1))
                attrition = dec(line.get("attrition", 0))
                setup = dec(line.get("setup_quantity", 0))
                multiple = dec(line.get("rounding_multiple", 0))
                required_child = ceil(build * per * (1 + attrition / 100) + setup)
                if multiple > 0:
                    required_child = ceil(required_child / multiple) * multiple
                demand[child] += required_child
            incoming[child] -= 1
            if incoming[child] == 0:
                queue.append(child)
    if seen - processed:
        warnings.append("Circular BOM reference detected")

    rows = []
    known = Decimal(0)
    missing = []
    for part_id, required in sorted(purchased.items(), key=lambda p: str(p[0])):
        quote = prices.get(part_id)
        unit = dec(quote[0]) if quote is not None else None
        total = required * unit if unit is not None else None
        if total is None:
            missing.append(part_id)
        else:
            known += total
        rows.append({"part_id": part_id, "shortage": float(required),
                     "unit_cost_cad": float(unit) if unit is not None else None,
                     "purchase_cost_cad": float(total) if total is not None else None,
                     "source": quote[1] if quote is not None else "Missing price"})
    return {"quantity": quantity, "known_spend_cad": float(known),
            "known_spend_per_unit_cad": float(known / quantity),
            "complete": not missing and not warnings, "missing_price_part_ids": missing,
            "warnings": warnings, "purchase_lines": rows,
            "stock_consumed": {k: float(v) for k, v in consumed.items() if v}}
