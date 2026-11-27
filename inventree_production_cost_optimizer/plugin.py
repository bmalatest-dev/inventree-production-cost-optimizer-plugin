"""Read-only production planning in the Part detail view."""

from __future__ import annotations

import math
from collections import defaultdict

from django.core.exceptions import ValidationError
from part.models import Part, BomItem
from plugin import InvenTreePlugin
from plugin.mixins import ActionMixin, UserInterfaceMixin

from .engine import plan
from .policy import spillage_per_project, is_basic_passive_identity, normalize_footprint, PASSIVE_FOOTPRINT_SPILLAGE


def _field(obj, name, default=None):
    return getattr(obj, name, default)


def _locations():
    from stock.models import StockLocation
    return list(StockLocation.objects.values('id', 'name', 'parent_id').order_by('name'))


def _available_stock(part, excluded):
    from stock.models import StockItem
    totals = dict(physical=0, excluded=0, allocated=0, available=0, packages=[])
    for item in StockItem.objects.filter(part=part).iterator():
        qty = max(0, float(item.quantity))
        totals['physical'] += qty
        if item.location_id in excluded or item.belongs_to_id or item.consumed_by_id:
            totals['excluded'] += qty
            continue
        # InvenTree's stock allocation method includes build and sales allocations.
        allocated = item.allocation_count()
        allocated = min(qty, max(0, float(allocated or 0)))
        totals['allocated'] += allocated
        totals['available'] += qty - allocated
        packaging = str(item.packaging or '').lower().replace('-', ' ')
        if not packaging and item.supplier_part_id:
            packaging = str(getattr(item.supplier_part, 'packaging', '') or '').lower()
        if any(x in packaging for x in ('reel', 'spool', 'tape')):
            totals['packages'].append(qty - allocated)
    return totals


def _price(part, usd_rate, selected, eligible=None, scope=None, cached_variants=False):
    """Placed/completed PO, cancelled PO, supplier price, stock purchase price, internal price.

    USD is converted using the scenario rate; other currencies remain unpriced.
    """
    eligible = eligible or [part.pk]
    scope = part.pk if scope is None else scope
    from order.models import PurchaseOrderLineItem
    try:
        from django.db.models import Case, When, Value, IntegerField
        lines = PurchaseOrderLineItem.objects.filter(part__part_id__in=eligible, order__status__in=[20, 30, 40]).select_related(
            'order', 'part__part'
        ).annotate(priority=Case(When(order__status=40, then=Value(1)), default=Value(0), output_field=IntegerField())).order_by('priority', '-order__creation_date', '-pk')
        for line in lines:
            order = line.order
            price = _field(line, 'purchase_price', None)
            if price is None or float(getattr(price, 'amount', price)) <= 0:
                continue
            currency = str(getattr(price, 'currency', _field(line, 'purchase_price_currency', '')))
            pack = float(_field(line.part, 'pack_quantity', 1) or 1)
            if currency in ('CAD', 'USD') and pack > 0:
                selected[scope] = order.pk
                status = {20: 'Placed', 30: 'Completed', 40: 'Cancelled cost reference'}[int(order.status)]
                return (float(getattr(price, 'amount', price)) / pack * (usd_rate if currency == 'USD' else 1), f'{line.part.part.name}: {status} PO {order.reference}: {price} {currency} / pack {pack:g}')
    except (AttributeError, ValueError, TypeError):
        pass

    try:
        from company.models import SupplierPart
        suppliers = SupplierPart.objects.filter(part_id__in=eligible).select_related('part').prefetch_related('price_breaks')
        offers = []
        for supplier in suppliers:
            pack = float(_field(supplier, 'pack_quantity', 1) or 1)
            if pack <= 0:
                continue
            for price_break in supplier.price_breaks.all():
                price = _field(price_break, 'price', None)
                currency = str(getattr(price, 'currency', _field(price_break, 'price_currency', '')))
                if price is not None and float(getattr(price, 'amount', price)) > 0 and currency in ('CAD', 'USD'):
                    offers.append((float(getattr(price, 'amount', price)) / pack * (usd_rate if currency == 'USD' else 1), f'{supplier.part.name}: Supplier price break: {price} {currency} / pack {pack:g} (indicative)'))
        if offers:
            return min(offers)
    except (AttributeError, TypeError, ValueError):
        pass
    # Legacy stock can carry a unit purchase price without a linked PO.
    from stock.models import StockItem
    weighted_cost = total_qty = 0.0
    priced_names = set()
    for item in StockItem.objects.filter(part_id__in=eligible, quantity__gt=0).select_related('part').iterator():
        price = item.purchase_price
        if price is None:
            continue
        try:
            currency = str(getattr(price, 'currency', getattr(item, 'purchase_price_currency', '')))
            amount = float(getattr(price, 'amount', price))
            qty = float(item.quantity)
            if currency not in ('CAD', 'USD') or not math.isfinite(amount) or amount <= 0 or not math.isfinite(qty) or qty <= 0:
                continue
            weighted_cost += amount * qty * (usd_rate if currency == 'USD' else 1)
            total_qty += qty
            priced_names.add(item.part.name)
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
    if total_qty > 0:
        return weighted_cost / total_qty, f'Historical stock purchase price (weighted average; {total_qty:g} priced units): {", ".join(sorted(priced_names))}'

    # Exact-only estimates must exclude InvenTree's rolled-up variant prices.
    fields = {
        'purchase_cost_max': 'purchase history',
        'supplier_price_max': 'supplier',
        'internal_cost_max': 'internal',
        'bom_cost_max': 'BOM',
    }
    offers = []
    for candidate in Part.objects.filter(pk__in=eligible):
        pricing = candidate.pricing
        sources = dict(fields)
        if cached_variants and candidate.pk == part.pk:
            sources['variant_cost_max'] = 'variant range'
        override = _native_quote(candidate, pricing, 'override_max', 'override', usd_rate)
        if override:
            offers.append(override)
            continue
        for field, source in sources.items():
            quote = _native_quote(candidate, pricing, field, source, usd_rate)
            if quote:
                offers.append(quote)
    return max(offers, default=None)


def _native_quote(candidate, pricing, field, source, usd_rate):
    price = getattr(pricing, field, None)
    if price is None:
        return None
    try:
        currency = str(getattr(price, 'currency', ''))
        amount = float(getattr(price, 'amount', price))
    except (TypeError, ValueError, OverflowError):
        return None
    if currency in ('CAD', 'USD') and math.isfinite(amount) and amount > 0:
        return (amount * (usd_rate if currency == 'USD' else 1),
                f'{candidate.name}: InvenTree {source} maximum ({price}; indicative)')
    return None


def _incoming_stock(part):
    from order.models import PurchaseOrderLineItem
    return sum(
        max(0, float(line.quantity) - float(line.received or 0)) * float(line.part.pack_quantity or 1)
        for line in PurchaseOrderLineItem.objects.filter(part__part=part, order__status=20).select_related('part')
    )


def _snapshot(root, usd_rate, excluded, overrides):
    """Load reachable BOM graph and inventory once per analysis request."""
    bom, stock, prices, labels, warnings = {}, {}, {}, {}, []
    inventory, policies, incoming, selected = {}, {}, {}, {}
    visited = set()
    queue = [root]
    while queue and len(visited) < 10000:
        part = queue.pop()
        if part.pk in visited:
            continue
        visited.add(part.pk)
        labels[part.pk] = {'name': part.name, 'ipn': part.IPN or '', 'pk': part.pk}
        inventory[part.pk] = _available_stock(part, excluded)
        stock[part.pk] = inventory[part.pk]['available']
        quote = _price(part, usd_rate, selected)
        if part.pk in overrides:
            quote = (overrides[part.pk], 'Manual CAD override')
        if quote:
            prices[part.pk] = quote
        params = {}
        for param in part.parameters.all():
            params[str(param.template.name).casefold()] = str(getattr(param, 'data', '') or getattr(param, 'value', '') or '')
        footprint = params.get('case/package') or params.get('case package') or params.get('footprint') or params.get('package') or ''
        passive = is_basic_passive_identity(part.name, part.description, part.category)
        hand = params.get('hand placement', '').lower() in ('yes', 'true', '1', 'y')
        spill, rule = spillage_per_project(footprint, quote[0] if quote else 0, passive_hint=passive)
        # Apply electronic component allowance only, not mechanical or PCB services.
        identity = (part.name + ' ' + str(part.category or '')).lower()
        electronic = not any(x in identity for x in ('mechanical', '-mech', '-bare', '-asm')) and not part.assembly
        policies[part.pk] = {'spill': float(spill) if electronic else 0, 'single_package': electronic and passive and not hand and normalize_footprint(footprint) in PASSIVE_FOOTPRINT_SPILLAGE}
        incoming[part.pk] = _incoming_stock(part)
        bom[part.pk] = []
        for line in BomItem.objects.filter(part=part).select_related('sub_part'):
            child = line.sub_part
            bom[part.pk].append({
                'part': child.pk, 'quantity': float(line.quantity),
                'attrition': float(_field(line, 'attrition', 0) or 0),
                'setup_quantity': float(_field(line, 'setup_quantity', 0) or 0),
                'rounding_multiple': float(_field(line, 'rounding_multiple', 0) or 0),
                'optional': bool(_field(line, 'optional', False)),
                'allow_variants': bool(_field(line, 'allow_variants', False)),
            })
            if child.pk not in visited:
                queue.append(child)
        if not bom[part.pk] and bool(_field(part, 'assembly', False)):
            warnings.append(f'Assembly {part.name} (ID {part.pk}) has no BOM; treated as purchasable')
    if queue:
        warnings.append('10,000-part traversal limit reached')
    # Reserve explicitly referenced parts for their own demand. Assign each
    # remaining variant inventory pool to at most one BOM part.
    flags = defaultdict(set)
    for lines in bom.values():
        for line in lines:
            if not line['optional']:
                flags[line['part']].add(line['allow_variants'])
    claimed = set(flags) | {root.pk}
    mixed = set()
    for part_id, permissions in sorted(flags.items()):
        if True not in permissions:
            continue
        part = Part.objects.get(pk=part_id)
        descendants = list(part.get_descendants(include_self=False).order_by('pk'))
        variants = [variant for variant in descendants if variant.active]
        if not variants:
            continue
        scope = part_id
        if False in permissions:
            mixed.add(part_id)
            scope = (part_id, True)
            inventory[scope] = dict(physical=0, excluded=0, allocated=0, available=0, packages=[])
            incoming[scope] = 0
            policies[scope] = policies[part_id].copy()
        eligible = [part_id] + [variant.pk for variant in variants]
        selected.pop(scope, None)
        quote = _price(part, usd_rate, selected, eligible, scope, len(variants) == len(descendants))
        if part_id in overrides:
            quote = (overrides[part_id], 'Manual CAD override')
        if quote:
            prices[scope] = quote
        for variant in variants:
            if variant.pk in claimed:
                warnings.append(f"Variant {variant.name} reserved for another BOM requirement; not pooled into {part.name}")
                continue
            claimed.add(variant.pk)
            totals = _available_stock(variant, excluded)
            for key in ('physical', 'excluded', 'allocated', 'available'):
                inventory[scope][key] += totals[key]
            inventory[scope]['packages'].extend(totals['packages'])
            incoming[scope] += _incoming_stock(variant)
        stock[scope] = inventory[scope]['available']
        # Recalculate price-sensitive spillage using the resolved variant quote.
        params = {str(p.template.name).casefold(): str(getattr(p, 'data', '') or getattr(p, 'value', '') or '') for p in part.parameters.all()}
        footprint = params.get('case/package') or params.get('case package') or params.get('footprint') or params.get('package') or ''
        passive = is_basic_passive_identity(part.name, part.description, part.category)
        identity = (part.name + ' ' + str(part.category or '')).lower()
        if not part.assembly and not any(x in identity for x in ('mechanical', '-mech', '-bare', '-asm')):
            policies[scope]['spill'] = float(spillage_per_project(footprint, quote[0] if quote else 0, passive_hint=passive)[0])
    return bom, stock, prices, labels, warnings, inventory, policies, incoming, selected, mixed


def _part_id(scope):
    return scope[0] if isinstance(scope, tuple) else scope


def _manufacturing(part):
    return any(str(x or '').upper().endswith(('-BARE', '-ASM')) for x in (part.name, part.IPN))


def _nre_candidates(selected, labels, usd_rate):
    from order.models import PurchaseOrderLineItem, PurchaseOrder
    groups = defaultdict(list)
    for scope, order_id in selected.items():
        label = labels[_part_id(scope)]
        if any(str(label[k]).upper().endswith(('-BARE', '-ASM')) for k in ('name', 'ipn')):
            groups[order_id].append(scope)
    rows = []
    for order_id, scopes in groups.items():
        parts = sorted({_part_id(scope) for scope in scopes})
        order = PurchaseOrder.objects.get(pk=order_id)
        for line in PurchaseOrderLineItem.objects.filter(order_id=order_id).select_related('part__part'):
            if line.part_id and _manufacturing(line.part.part):
                continue
            price = line.purchase_price
            currency = str(getattr(line, 'purchase_price_currency', ''))
            cost = float(getattr(price, 'amount', price)) * float(line.quantity) * (usd_rate if currency == 'USD' else 1) if price is not None and currency in ('CAD', 'USD') else None
            rows.append(dict(id='line:' + str(line.pk), order_id=order_id, po=order.reference, description=str(line.part.part.name if line.part_id else getattr(line, 'reference', 'Additional line')), part_ids=parts, cost=cost, scopes=scopes))
        for line in order.extra_lines.all():
            price = line.price
            currency = str(getattr(line, 'price_currency', ''))
            cost = float(getattr(price, 'amount', price)) * float(line.quantity) * (usd_rate if currency == 'USD' else 1) if price is not None and currency in ('CAD', 'USD') else None
            rows.append(dict(id='extra:' + str(line.pk), order_id=order_id, po=order.reference, description=str(getattr(line, 'description', '') or getattr(line, 'reference', '') or 'Extra charge'), part_ids=parts, cost=cost, scopes=scopes))
    return rows


class ProductionCostOptimizerPlugin(ActionMixin, UserInterfaceMixin, InvenTreePlugin):
    NAME = 'Production Cost Optimizer'
    SLUG = 'production-cost-optimizer'
    TITLE = 'Production Cost Optimizer'
    DESCRIPTION = 'Read-only BOM and inventory-aware production purchasing scenarios.'
    VERSION = '0.3.6'
    AUTHOR = 'Per Vices Corporation'
    LICENSE = 'MIT'
    ACTION_NAME = 'production_cost_optimizer'

    def get_ui_panels(self, request, context, **kwargs):
        context = context or {}
        if context.get('target_model') != 'part':
            return []
        try:
            part = Part.objects.get(pk=context.get('target_id'))
        except (Part.DoesNotExist, ValueError, TypeError):
            return []
        return [{
            'key': 'production-cost-optimizer-panel',
            'title': 'Production Cost Optimizer',
            'description': 'Recursive BOM purchasing scenarios',
            'icon': 'ti:calculator:outline',
            'source': self.plugin_static_file('optimizer_v033.js:renderOptimizer'),
            'context': {'part_id': part.pk, 'part_name': part.name, 'part_ipn': part.IPN or '', 'locations': _locations()},
        }]

    def perform_action(self, user=None, data=None):
        data = data or {}
        try:
            part_id = int(data.get('part'))
            minimum = int(data.get('minimum', 1))
            maximum = int(data.get('maximum', minimum * 2))
            usd_rate = float(data.get('usd_rate', 1.40))
        except (TypeError, ValueError) as exc:
            raise ValidationError('Part and build quantities must be integers') from exc
        if not math.isfinite(usd_rate) or usd_rate <= 0:
            raise ValidationError('USD to CAD rate must be positive')
        if not (1 <= minimum <= maximum <= 200):
            raise ValidationError('Specify a range of 1–200 units, minimum <= maximum')
        try:
            root = Part.objects.get(pk=part_id)
        except Part.DoesNotExist as exc:
            raise ValidationError('Part does not exist') from exc
        try:
            excluded = {int(x) for x in data.get('excluded_locations', [])}
            overrides = {int(k): float(v) for k, v in data.get('overrides', {}).items()}
            if any(not math.isfinite(v) or v < 0 for v in overrides.values()):
                raise ValueError('Invalid override')
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError('Invalid location or price override') from exc
        if data.get('exclude_children', True):
            locations = _locations()
            while True:
                expanded = excluded | {x['id'] for x in locations if x['parent_id'] in excluded}
                if expanded == excluded:
                    break
                excluded = expanded
        bom, stock, prices, labels, warnings, inventory, policies, incoming, selected, mixed = _snapshot(root, usd_rate, excluded, overrides)
        variant_enabled = {line['part'] for lines in bom.values() for line in lines if line['allow_variants'] and not line['optional']}
        names = [x['name'].upper() + ' ' + x['ipn'].upper() for x in labels.values()]
        for suffix, label in [('-BARE', 'PCB fabrication'), ('-ASM', 'PCB assembly')]:
            if not any(suffix in name for name in names):
                warnings.append(f'{label} costs excluded: no {suffix} parts in BOM')
        if any('MECH' in labels[k]['name'].upper() and not bom[k] and k not in prices for k in labels):
            warnings.append('Mechanical costs excluded: unpriced mechanical assembly without BOM')
        settings = {'spillage': bool(data.get('spillage', True)), 'single_package': bool(data.get('single_package', True)), 'include_incoming': bool(data.get('include_incoming', False)), 'use_subassemblies': bool(data.get('use_subassemblies', True)), 'include_nre': bool(data.get('include_nre', True))}
        scenarios = [plan(root.pk, qty, bom, stock, prices, policies, {k: v['packages'] for k,v in inventory.items()}, incoming if settings['include_incoming'] else {}, settings['spillage'], settings['single_package'], settings['use_subassemblies'], mixed=mixed)
                     for qty in range(minimum, maximum + 1)]
        candidates = _nre_candidates(selected, labels, usd_rate)
        try:
            edits = data.get('nre_edits') or {}
            for row in candidates:
                edit = edits.get(row['id'], {})
                row['included'] = bool(edit.get('included', True))
                if edit.get('cost') is not None:
                    row['cost'] = float(edit['cost'])
                if row['cost'] is not None and not math.isfinite(row['cost']):
                    raise ValueError(f"Invalid NRE cost for {row['po']} / {row['description']}: {row['cost']}")
            for i, row in enumerate(data.get('manual_nre') or []):
                part_id = int(row['part_id'])
                cost = float(row['cost'])
                if part_id not in labels or not math.isfinite(cost) or cost < 0:
                    raise ValueError('Invalid manual NRE')
                candidates.append(dict(id='manual:' + str(i), po='Manual', description=str(row['description']), part_ids=[part_id], cost=cost, included=True))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValidationError(f'Invalid NRE settings: {type(exc).__name__}: {exc}') from exc
        # Incremental spend and marginal increase; incomplete prices remain marked.
        previous = None
        for scenario in scenarios:
            shortages = {p['part_id'] for p in scenario['purchase_lines'] if p['shortage'] > 0}
            shortage_parts = {_part_id(scope) for scope in shortages}
            scenario['recurring_spend_cad'] = scenario['known_spend_cad']
            scenario['nre_lines'] = [
                row for row in candidates
                if settings['include_nre'] and row['included']
                and (shortages.intersection(row['scopes']) if 'scopes' in row
                     else shortage_parts.intersection(row['part_ids']))
            ]
            scenario['nre_spend_cad'] = sum(r['cost'] for r in scenario['nre_lines'] if r['cost'] is not None)
            if any(r['cost'] is None for r in scenario['nre_lines']):
                scenario['complete'] = False
                scenario['warnings'].append('One or more applicable NRE charges are unpriced')
            scenario['known_spend_cad'] += scenario['nre_spend_cad']
            scenario['known_spend_per_unit_cad'] = scenario['known_spend_cad'] / scenario['quantity']
            scenario['incremental_next_unit_cad'] = (
                scenario['known_spend_cad'] - previous['known_spend_cad']
                if previous is not None else None
            )
            scenario['complete'] = scenario['complete'] and not any('traversal limit' in w for w in warnings)
            scenario['warnings'].extend(warnings)
            for line in scenario['subassemblies']:
                line['part'] = labels[line['part_id']]
            scenario['missing_price_part_ids'] = sorted({_part_id(scope) for scope in scenario['missing_price_part_ids']})
            for line in scenario['purchase_lines']:
                scope = line['part_id']
                part_id = _part_id(scope)
                line['part_id'] = part_id
                line['part'] = labels[part_id]
                line.update({key: value for key, value in inventory[scope].items() if key != 'packages'})
                if part_id in mixed:
                    demand_type = 'Variant-eligible' if scope != part_id else 'Exact only'
                    line['source'] = f"{demand_type}: {line['source']}"
                if line['shortage'] > 0 and line['unit_cost_cad'] is None:
                    part_name = line['part']['ipn'] or line['part']['name']
                    allows_variants = scope != part_id or (part_id not in mixed and part_id in variant_enabled)
                    eligibility = 'exact part and eligible active variants' if allows_variants else 'exact part only'
                    scenario['warnings'].append(
                        f'{part_name} (ID {part_id}): no positive CAD/USD price for {eligibility} '
                        'in purchase orders, supplier pricing, stock purchase prices, or InvenTree pricing'
                    )
            previous = scenario
        for row in candidates:
            row.pop('scopes', None)
        self._last_result = {'part': labels[root.pk], 'currency': 'CAD', 'usd_rate': usd_rate,
                             'scenarios': scenarios, 'read_only': True, 'excluded_locations': sorted(excluded), 'exclude_children': data.get('exclude_children', True), 'overrides': overrides, 'settings': settings, 'nre_candidates': candidates, 'part_labels': labels,
                             'limitations': [
                                 'Estimation only: all stock statuses assumed usable; existing allocations deducted; selected locations excluded',
                                 'Optional BOM lines and explicit substitutes are not automatically selected; overlapping variant pools are reserved conservatively',
                                 'Supplier MOQs, package multiples and currencies other than CAD/USD are not yet modeled',
                                 'Reported spend is a lower bound when pricing is incomplete',
                             ]}

    def get_result(self, user=None, data=None):
        return getattr(self, '_last_result', {'status': 'no_result'})
