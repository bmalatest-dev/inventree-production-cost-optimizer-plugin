"""Read-only production planning in the Part detail view."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from part.models import Part, BomItem
from plugin import InvenTreePlugin
from plugin.mixins import ActionMixin, UserInterfaceMixin

from .engine import plan


def _field(obj, name, default=None):
    return getattr(obj, name, default)


def _available_stock(part):
    """Use InvenTree's unallocated availability where exposed.

    Do not count stock that is otherwise unusable as a purchasing offset.
    """
    from stock.models import StockItem
    from django.db.models import Sum, F
    # Core availability intentionally uses the available_stock property which
    # accounts for existing allocations; status is screened independently.
    total = 0
    for item in StockItem.objects.filter(part=part).iterator():
        if _field(item, 'belongs_to_id') or _field(item, 'consumed_by_id'):
            continue
        # Only usable statuses. This intentionally excludes rework, quarantine,
        # failed, returned, damaged, lost, and in-transit stock.
        if int(_field(item, 'status', -1)) != 10:
            continue
        if _field(item, 'status_custom_key') is not None:
            continue
        amount = _field(item, 'available_stock', None)
        if amount is None:
            allocated = _field(item, 'allocated', 0)
            amount = max(0, float(item.quantity) - float(allocated or 0))
        total += max(0, float(amount))
    return total


def _price(part):
    """CAD only: historical completed order price then supplier price fallback.

    No implicit FX conversion: other currencies are flagged as unpriced.
    """
    from order.models import PurchaseOrderLineItem
    try:
        lines = PurchaseOrderLineItem.objects.filter(supplier_part__part=part).select_related(
            'order', 'supplier_part'
        ).order_by('-pk')[:100]
        for line in lines:
            order = line.order
            # Explicitly only count orders marked complete (InvenTree status 30).
            if int(order.status) != 30:
                continue
            price = _field(line, 'purchase_price', None)
            if price is None or float(price) <= 0:
                continue
            currency = str(_field(line, 'purchase_price_currency', 'CAD'))
            pack = float(_field(line.supplier_part, 'pack_quantity', 1) or 1)
            if currency == 'CAD' and pack > 0:
                return (float(price) / pack, f'Completed PO {order.reference}')
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
                if price is not None and float(price) > 0 and currency == 'CAD':
                    offers.append(float(price) / pack)
        if offers:
            return min(offers), 'Supplier price break (indicative; MOQ not modeled)'
    except (AttributeError, TypeError, ValueError):
        pass
    return None


def _snapshot(root):
    """Load reachable BOM graph and inventory once per analysis request."""
    bom, stock, prices, labels, warnings = {}, {}, {}, {}, []
    visited = set()
    queue = [root]
    while queue and len(visited) < 10000:
        part = queue.pop()
        if part.pk in visited:
            continue
        visited.add(part.pk)
        labels[part.pk] = {'name': part.name, 'ipn': part.IPN or '', 'pk': part.pk}
        stock[part.pk] = _available_stock(part)
        quote = _price(part)
        if quote:
            prices[part.pk] = quote
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
            warnings.append(f'Assembly {part.pk} has no BOM; treated as purchasable')
    if queue:
        warnings.append('10,000-part traversal limit reached')
    return bom, stock, prices, labels, warnings


class ProductionCostOptimizerPlugin(ActionMixin, UserInterfaceMixin, InvenTreePlugin):
    NAME = 'Production Cost Optimizer'
    SLUG = 'production-cost-optimizer'
    TITLE = 'Production Cost Optimizer'
    DESCRIPTION = 'Read-only BOM and inventory-aware production purchasing scenarios.'
    VERSION = '0.1.1'
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
            'source': self.plugin_static_file('optimizer.js:renderOptimizer'),
            'context': {'part_id': part.pk, 'part_name': part.name, 'part_ipn': part.IPN or ''},
        }]

    def perform_action(self, user=None, data=None):
        data = data or {}
        try:
            part_id = int(data.get('part'))
            minimum = int(data.get('minimum', 1))
            maximum = int(data.get('maximum', minimum * 2))
        except (TypeError, ValueError) as exc:
            raise ValidationError('Part and build quantities must be integers') from exc
        if not (1 <= minimum <= maximum <= 200):
            raise ValidationError('Specify a range of 1–200 units, minimum <= maximum')
        try:
            root = Part.objects.get(pk=part_id)
        except Part.DoesNotExist as exc:
            raise ValidationError('Part does not exist') from exc
        bom, stock, prices, labels, warnings = _snapshot(root)
        scenarios = [plan(root.pk, qty, bom, stock, prices)
                     for qty in range(minimum, maximum + 1)]
        # Incremental spend and marginal increase; incomplete prices remain marked.
        previous = None
        for scenario in scenarios:
            scenario['incremental_next_unit_cad'] = (
                scenario['known_spend_cad'] - previous['known_spend_cad']
                if previous is not None and previous['complete'] and scenario['complete'] else None
            )
            scenario['complete'] = scenario['complete'] and not warnings
            scenario['warnings'].extend(warnings)
            for line in scenario['purchase_lines']:
                line['part'] = labels.get(line['part_id'], {'pk': line['part_id']})
            previous = scenario
        self._last_result = {'part': labels[root.pk], 'currency': 'CAD',
                             'scenarios': scenarios, 'read_only': True,
                             'limitations': [
                                 'Only unallocated normal-status stock is considered',
                                 'Optional BOM lines and substitutes are not automatically selected',
                                 'Supplier MOQs, package multiples and FX conversion are not yet modeled',
                                 'Reported spend is a lower bound when pricing is incomplete',
                             ]}

    def get_result(self, user=None, data=None):
        return getattr(self, '_last_result', {'status': 'no_result'})
