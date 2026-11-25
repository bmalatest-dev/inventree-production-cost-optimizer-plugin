import unittest

from inventree_production_cost_optimizer.engine import plan


class PlannerTests(unittest.TestCase):
    def test_recursive_and_existing_subassemblies(self):
        bom = {1: [{'part': 2, 'quantity': 1}], 2: [{'part': 3, 'quantity': 4}]}
        result = plan(1, 10, bom, {2: 3, 3: 10}, {3: (5, 'test')})
        self.assertEqual(result['purchase_lines'][0]['shortage'], 18)
        self.assertEqual(result['known_spend_cad'], 90)

    def test_shared_component_not_double_consumed(self):
        bom = {1: [{'part': 2, 'quantity': 1}, {'part': 4, 'quantity': 1}],
               2: [{'part': 3, 'quantity': 1}], 4: [{'part': 3, 'quantity': 2}]}
        result = plan(1, 10, bom, {3: 20}, {3: (100, 'test')})
        self.assertEqual(result['purchase_lines'][0]['shortage'], 10)

    def test_shared_component_at_different_depths(self):
        bom = {1: [{'part': 3, 'quantity': 1}, {'part': 2, 'quantity': 1}],
               2: [{'part': 3, 'quantity': 1}]}
        result = plan(1, 10, bom, {3: 5}, {3: (1, 'test')})
        self.assertEqual(result['purchase_lines'][0]['shortage'], 15)

    def test_finished_stock_does_not_reduce_target(self):
        result = plan(1, 10, {1: [{'part': 2, 'quantity': 1}]}, {1: 100}, {2: (2, 'test')})
        self.assertEqual(result['known_spend_cad'], 20)

    def test_missing_cost_explicit(self):
        result = plan(1, 2, {1: [{'part': 2, 'quantity': 1}]}, {}, {})
        self.assertFalse(result['complete'])
        self.assertEqual(result['missing_price_part_ids'], [2])

    def test_attrition_and_multiple(self):
        result = plan(1, 10, {1: [{'part': 2, 'quantity': 2, 'attrition': 10,
                                    'setup_quantity': 1, 'rounding_multiple': 5}]}, {}, {2: (1, 'test')})
        self.assertEqual(result['purchase_lines'][0]['shortage'], 25)

    def test_cycle_is_warning(self):
        result = plan(1, 2, {1: [{'part': 2}], 2: [{'part': 1}]}, {}, {})
        self.assertFalse(result['complete'])
        self.assertTrue(any('Circular' in w for w in result['warnings']))


if __name__ == '__main__':
    unittest.main()
