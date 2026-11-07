"""Check that paired efficiency rounds balance framework execution positions."""

import unittest
from collections import Counter

from efficiency.driver import variant_order


class OrderingTests(unittest.TestCase):
    def test_paired_rounds_reverse_order_for_two_three_and_four_variants(self):
        for count in (2, 3, 4):
            names = list(range(count))
            for index in range(0, count * 2, 2):
                first = variant_order(names, index)
                second = variant_order(names, index + 1)
                self.assertEqual(second, first[::-1])
                self.assertCountEqual(first, names)

    def test_each_variant_occupies_every_position_equally(self):
        for count in (2, 3, 4):
            names = list(range(count))
            positions = Counter(
                (name, position)
                for index in range(count * 2)
                for position, name in enumerate(variant_order(names, index))
            )
            self.assertEqual(set(positions.values()), {2})
            self.assertEqual(len(positions), count * count)


if __name__ == "__main__":
    unittest.main()
