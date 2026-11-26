"""Read-only production planning in the Part detail view."""

from __future__ import annotations

import math

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


def _price(part, usd_rate):
    """Historical completed order price then supplier price fallback.

    USD is converted using the scenario rate; other currencies remain unpriced.
    """
    from order.models import PurchaseOrderLineItem
    try:
        lines = PurchaseOrderLineItem.objects.filter(part__part=part, order__status=30).select_related(
            'order', 'part'
        ).order_by('-order__complete_date', '-pk')
        for line in lines:
            order = line.order
            # Explicitly only count orders marked complete (InvenTree status 30).
            if int(order.status) != 30:
                continue
            price = _field(line, 'purchase_price', None)
            if price is None or float(getattr(price, 'amount', price)) <= 0:
                continue
            currency = str(_field(line, 'purchase_price_currency', 'CAD'))
            pack = float(_field(line.part, 'pack_quantity', 1) or 1)
            if currency in ('CAD', 'USD') and pack > 0:
                return (float(getattr(price, 'amount', price)) / pack * (usd_rate if currency == 'USD' else 1), f'Completed PO {order.reference}: {price} {currency} / pack {pack:g}')
    except (AttributeError, ValueError, TypeError):
        pass

    try:
        from company.models import SupplierPart
        suppliers = SupplierPart.objects.filter(part=part).prefetch_related('price_breaks')
        offers = []
        for supplier in suppliers:
            pack = float(_field(supplier, 'pack_quantity', 1) or 1)
            if pack <= 0:
                continue
            for price_break in supplier.price_breaks.all():
                price = _field(price_break, 'price', None)
                currency = str(_field(price_break, 'price_currency', 'CAD'))
                if price is not None and float(getattr(price, 'amount', price)) > 0 and currency in ('CAD', 'USD'):
                    offers.append((float(getattr(price, 'amount', price)) / pack * (usd_rate if currency == 'USD' else 1), f'Supplier price break: {price} {currency} / pack {pack:g} (indicative)'))
        if offers:
            return min(offers)
    except (AttributeError, TypeError, ValueError):
        pass
    return None


def _snapshot(root, usd_rate, excluded, overrides):
    """Load reachable BOM graph and inventory once per analysis request."""
    bom, stock, prices, labels, warnings = {}, {}, {}, {}, []
    inventory, policies, incoming = {}, {}, {}
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
        quote = (overrides[part.pk], 'Manual CAD override') if part.pk in overrides else _price(part, usd_rate)
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
        from order.models import PurchaseOrderLineItem
        incoming[part.pk] = sum(max(0, float(line.quantity) - float(line.received or 0)) * float(line.part.pack_quantity or 1) for line in PurchaseOrderLineItem.objects.filter(part__part=part, order__status=20).select_related('part'))
        bom[part.pk] = []
        for line in BomItem.objects.filter(part=part).select_related('sub_part'):
            child = line.sub_part
            bom[part.pk].append({
                'part': child.pk, 'quantity': float(line.quantity),
                'attrition': float(_field(line, 'attrition', 0) or 0),
                'setup_quantity': float(_field(line, 'setup_quantity', 0) or 0),
                'rounding_multiple': float(_field(line, 'rounding_multiple', 0) or 0),
                'optional': bool(_field(line, 'optional', False)),
            })
            if child.pk not in visited:
                queue.append(child)
        if not bom[part.pk] and bool(_field(part, 'assembly', False)):
            warnings.append(f'Assembly {part.name} (ID {part.pk}) has no BOM; treated as purchasable')
    if queue:
        warnings.append('10,000-part traversal limit reached')
    return bom, stock, prices, labels, warnings, inventory, policies, incoming


class ProductionCostOptimizerPlugin(ActionMixin, UserInterfaceMixin, InvenTreePlugin):
    NAME = 'Production Cost Optimizer'
    SLUG = 'production-cost-optimizer'
    TITLE = 'Production Cost Optimizer'
    DESCRIPTION = 'Read-only BOM and inventory-aware production purchasing scenarios.'
    VERSION = '0.2.3'
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
            'source': self.plugin_static_file('optimizer_v022.js:renderOptimizer'),
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
        bom, stock, prices, labels, warnings, inventory, policies, incoming = _snapshot(root, usd_rate, excluded, overrides)
        names = [x['name'].upper() + ' ' + x['ipn'].upper() for x in labels.values()]
        for suffix, label in [('-BARE', 'PCB fabrication'), ('-ASM', 'PCB assembly')]:
            if not any(suffix in name for name in names):
                warnings.append(f'{label} costs excluded: no {suffix} parts in BOM')
        if any('MECH' in labels[k]['name'].upper() and not bom[k] and k not in prices for k in labels):
            warnings.append('Mechanical costs excluded: unpriced mechanical assembly without BOM')
        settings = {'spillage': bool(data.get('spillage', True)), 'single_package': bool(data.get('single_package', True)), 'include_incoming': bool(data.get('include_incoming', False)), 'use_subassemblies': bool(data.get('use_subassemblies', True))}
        scenarios = [plan(root.pk, qty, bom, stock, prices, policies, {k: v['packages'] for k,v in inventory.items()}, incoming if settings['include_incoming'] else {}, settings['spillage'], settings['single_package'], settings['use_subassemblies'])
                     for qty in range(minimum, maximum + 1)]
        # Incremental spend and marginal increase; incomplete prices remain marked.
        previous = None
        for scenario in scenarios:
            scenario['incremental_next_unit_cad'] = (
                scenario['known_spend_cad'] - previous['known_spend_cad']
                if previous is not None else None
            )
            scenario['complete'] = scenario['complete'] and not any('traversal limit' in w for w in warnings)
            scenario['warnings'].extend(warnings)
            for line in scenario['subassemblies']:
                line['part'] = labels[line['part_id']]
            for line in scenario['purchase_lines']:
                line['part'] = labels.get(line['part_id'], {'pk': line['part_id']})
                line.update({k:v for k,v in inventory[line['part_id']].items() if k != 'packages'})
            previous = scenario
        self._last_result = {'part': labels[root.pk], 'currency': 'CAD', 'usd_rate': usd_rate,
                             'scenarios': scenarios, 'read_only': True, 'excluded_locations': sorted(excluded), 'exclude_children': data.get('exclude_children', True), 'overrides': overrides, 'settings': settings,
                             'limitations': [
                                 'Estimation only: all stock statuses assumed usable; existing allocations deducted; selected locations excluded',
                                 'Optional BOM lines and substitutes are not automatically selected',
                                 'Supplier MOQs, package multiples and currencies other than CAD/USD are not yet modeled',
                                 'Reported spend is a lower bound when pricing is incomplete',
                             ]}

    def get_result(self, user=None, data=None):
        return getattr(self, '_last_result', {'status': 'no_result'})
