"""What /credits says is for sale, and when it says nothing.

A sale connection existing is not the same as something being for sale. Before
this, the route emitted a sale dict for any speech connection and left
rate_per_1m_chars as a null, so the client had to guess whether a null meant
"this server sells packages" or "this server is not selling". One capability,
two fields, allowed to disagree. Leo found it in the panel at ui/index.html
(2026-10-25): price-unknown sentence directly above priced buttons.

_sale_offer answers one question, the same one the purchase route asks: can this
plugin turn a dollar into characters, and can it show a price?
"""

import unittest

from host.app import _sale_offer


class Speech:
    def __init__(self, rate=None, bundles=None, chars_per_dollar=None):
        if rate is not None:
            self.sale_rate_per_1m = lambda cred: rate
        if bundles is not None:
            self.bundles = lambda cred: bundles
        if chars_per_dollar is not None:
            self.chars_for_payment = lambda usd, cred: int(usd * chars_per_dollar)


class TestSaleOffer(unittest.TestCase):
    def test_rate_and_bundles(self):
        s = Speech(rate=19.63, bundles=[{"usd": 5, "chars": 254_700}],
                   chars_per_dollar=50_942)
        out = _sale_offer("azure", s, {})
        self.assertEqual(out["provider"], "azure")
        self.assertAlmostEqual(out["rate_per_1m_chars"], 19.63)
        self.assertEqual(out["currency"], "USD")
        self.assertEqual(out["bundles"], [{"usd": 5, "chars": 254_700}])

    def test_bundles_only_is_still_a_sale_with_no_rate(self):
        # Chargeable and priceable, just not priced per million. The client now
        # has a shape to branch on instead of a null to interpret.
        s = Speech(bundles=[{"usd": 5, "chars": 254_700}], chars_per_dollar=50_942)
        out = _sale_offer("azure", s, {})
        self.assertIsNotNone(out)
        self.assertIsNone(out["rate_per_1m_chars"])
        self.assertEqual(len(out["bundles"]), 1)

    def test_rate_without_bundles_and_without_a_way_to_charge_is_not_a_sale(self):
        # A rate is a number; if a dollar buys nothing, there is nothing to buy.
        s = Speech(rate=19.63, chars_per_dollar=0)
        self.assertIsNone(_sale_offer("azure", s, {}))

    def test_chargeable_but_unpriceable_is_not_a_sale(self):
        # It could take money, but there is no rate and no package to show, so
        # the screen would have nothing true to put next to a button.
        s = Speech(chars_per_dollar=50_942)
        self.assertIsNone(_sale_offer("azure", s, {}))

    def test_a_plugin_with_no_pricing_at_all_sells_nothing(self):
        self.assertIsNone(_sale_offer("plain", Speech(), {}))

    def test_zero_and_negative_rates_are_treated_as_absent(self):
        for bad in (0, -1, -19.63):
            with self.subTest(rate=bad):
                s = Speech(rate=bad, chars_per_dollar=50_942)
                self.assertIsNone(_sale_offer("azure", s, {}))

    def test_a_rate_that_is_not_a_number_is_treated_as_absent(self):
        class Weird:
            def sale_rate_per_1m(self, cred):
                return "lots"

            def chars_for_payment(self, usd, cred):
                return 1000

        self.assertIsNone(_sale_offer("weird", Weird(), {}))

    def test_bundles_without_a_price_are_not_shown(self):
        s = Speech(bundles=[{"usd": None, "chars": 100}, {"chars": 200},
                            {"usd": 5, "chars": 254_700}],
                   chars_per_dollar=50_942)
        out = _sale_offer("azure", s, {})
        self.assertEqual(out["bundles"], [{"usd": 5, "chars": 254_700}])

    def test_a_bundle_list_that_is_not_dicts_does_not_crash(self):
        s = Speech(bundles=["nonsense", None], chars_per_dollar=50_942)
        self.assertIsNone(_sale_offer("azure", s, {}))

    def test_pricing_reads_the_stored_cred_not_a_users_override(self):
        seen = {}

        class Spy:
            def sale_rate_per_1m(self, cred):
                seen["cred"] = cred
                return 19.63

            def chars_for_payment(self, usd, cred):
                return 1000

        _sale_offer("azure", Spy(), {"markup": "1.3"})
        self.assertEqual(seen["cred"], {"markup": "1.3"})


if __name__ == "__main__":
    unittest.main()
