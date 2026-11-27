"""Pure, deterministic inventory/BOM planner. Does not write to InvenTree."""

from collections import defaultdict, deque
from decimal import Decimal, ROUND_CEILING


def dec(value):
    return Decimal(str(value or 0))


def ceil(value):
    return dec(value).to_integral_value(rounding=ROUND_CEILING)


def plan(root, quantity, bom, stock, prices, policies=None, packages=None, incoming=None, spillage=False, single_package=False, use_subassemblies=True, mixed=()):
    """Expand one production run, consuming shared available stock only once.

    bom: parent -> [{part, quantity, attrition, setup_quantity, rounding_multiple}]
    stock: part -> available units; prices: part -> (unit CAD cost, provenance)
    """
    policies, packages, incoming = policies or {}, packages or {}, incoming or {}
    projects = defaultdict(dict)
    requirements, usable, po_used = {}, {}, {}
    quantity = int(quantity)
    if quantity <= 0:
        raise ValueError("Build quantity must be positive")
    available = {key: dec(value) for key, value in stock.items()}
    order_stock = {key: dec(value) for key, value in incoming.items()}
    package_stock = {key: [dec(q) for q in values if q > 0] for key, values in packages.items()}
    demand = defaultdict(Decimal, {root: dec(quantity)})
    purchased = defaultdict(Decimal)
    consumed = defaultdict(Decimal)
    warnings = []
    # Topological order ensures a shared subassembly reached by different
    # branches/depths is allocated once, after all parent demands are combined.
    graph = {}
    dependencies = defaultdict(int)
    subassemblies = []
    seen = {root}
    pending = [root]
    while pending and len(seen) <= 10000:
        parent = pending.pop()
        children = [line for line in bom.get(parent, []) if not line.get("optional")]
        graph[parent] = children
        for line in children:
            child = line["part"]
            dependencies[child] += 1
            if child not in seen:
                seen.add(child)
                pending.append(child)
    if pending:
        warnings.append("BOM exceeds 10,000 distinct parts")
    if dependencies[root]:
        warnings.append("Circular BOM reference detected")
    queue = deque([root])
    processed = set()
    while queue:
        part_id = queue.popleft()
        if part_id in processed:
            warnings.append(f"Circular BOM reference detected at {part_id}")
            continue
        processed.add(part_id)
        lines = graph.get(part_id, [])
        scopes = [part_id, (part_id, True)] if part_id in mixed else [part_id]
        required_total = build = Decimal(0)
        for scope in scopes:
            required = demand[scope]
            if required <= 0:
                continue
            if part_id == root:
                build = required  # Target is newly manufactured units.
                if not lines:
                    offset = _take_stock(order_stock, [scope], build)
                    po_used[scope] = offset
                    purchased[scope] += build - offset
                continue
            policy = policies.get(scope, {})
            pools = [part_id] if scope == part_id else [scope, part_id]
            stock_available = sum(max(Decimal(0), available.get(key, Decimal(0))) for key in pools)
            package_only = not lines and single_package and policy.get('single_package')
            if not lines:
                needs = [
                    max(adjusted, nominal + dec(policy.get('spill', 0))) if spillage else adjusted
                    for nominal, adjusted in projects[scope].values()
                ]
                required = sum(needs, Decimal(0))
                if package_only:
                    stock_available = _reserve_packages(needs, pools, package_stock, available)
                requirements[scope] = required
                usable[scope] = stock_available
            used = min(required, stock_available)
            if lines and not use_subassemblies:
                used = Decimal(0)
            if not package_only:
                _take_stock(available, pools, used)
            consumed[part_id] += used
            shortfall = required - used
            required_total += required
            build += shortfall
            if shortfall > 0 and not lines:
                offset = Decimal(0) if package_only else _take_stock(order_stock, pools, shortfall)
                po_used[scope] = offset
                purchased[scope] += shortfall - offset
        if lines and part_id != root:
            subassemblies.append({
                'part_id': part_id,
                'required': float(required_total),
                'available': float(sum(max(Decimal(0), dec(stock.get(scope, 0))) for scope in scopes)),
                'utilized': float(required_total - build),
                'to_manufacture': float(build),
            })
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
                scope = (child, True) if child in mixed and line.get("allow_variants") else child
                demand[scope] += required_child
                nominal = ceil(build * per + setup)
                if multiple > 0:
                    nominal = ceil(nominal / multiple) * multiple
                old_nominal, old_adjusted = projects[scope].get(part_id, (Decimal(0), Decimal(0)))
                projects[scope][part_id] = (old_nominal + nominal, old_adjusted + required_child)
            dependencies[child] -= 1
            if dependencies[child] == 0:
                queue.append(child)
    if seen - processed:
        warnings.append("Circular BOM reference detected")

    rows = []
    known = Decimal(0)
    missing = []
    for part_id, required in sorted(purchased.items(), key=lambda p: str(p[0])):
        if required <= 0 and not po_used.get(part_id, 0):
            continue
        quote = prices.get(part_id)
        unit = dec(quote[0]) if quote is not None else None
        total = Decimal(0) if required == 0 else (required * unit if unit is not None else None)
        if total is None:
            missing.append(part_id)
        else:
            known += total
        rows.append({"part_id": part_id, "shortage": float(required),
                     "unit_cost_cad": float(unit) if unit is not None else None,
                     "purchase_cost_cad": float(total) if total is not None else None,
                     "source": quote[1] if quote is not None else "Missing price",
                     "required": float(requirements.get(part_id, required)),
                     "usable": float(usable.get(part_id, 0)), "incoming_used": float(po_used.get(part_id, 0))})
    return {"quantity": quantity, "known_spend_cad": float(known),
            "known_spend_per_unit_cad": float(known / quantity),
            "complete": not missing and not warnings, "missing_price_part_ids": missing,
            "warnings": warnings, "purchase_lines": rows, "subassemblies": subassemblies,
            "stock_consumed": {k: float(v) for k, v in consumed.items() if v}}


def _take_stock(available, pools, quantity):
    remaining = quantity
    for key in pools:
        used = min(remaining, max(Decimal(0), available.get(key, Decimal(0))))
        available[key] = available.get(key, Decimal(0)) - used
        remaining -= used
    return quantity - remaining


def _reserve_packages(needs, pools, package_stock, available):
    covered = Decimal(0)
    for need in sorted(needs, reverse=True):
        candidates = [
            (quantity, key, index)
            for key in pools
            for index, quantity in enumerate(package_stock.get(key, []))
            if quantity >= need
        ]
        if candidates:
            _, key, index = min(candidates, key=lambda candidate: candidate[0])
            package_stock[key][index] -= need
            available[key] -= need
            covered += need
    return covered
