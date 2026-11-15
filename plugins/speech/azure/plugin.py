"""v2 speech plugin: azure. The v1 implementation behind the v2 contract.

The proven v1 object is unchanged, plus the default quote/report and an adapter
that coerces the v1 Audio (.bytes) into a host.contracts.Audio (.data). The
plugin tree is what the host discovers; nothing above host/pluginhost.py imports
a plugin by name.

Resale (docs/PRICING.md): a connection can be the designated SALE connection,
where its voices are resold as character capacity. The config screen gains two
fields - the markup over cost and a per-1M-chars infra fee - and this plugin
becomes the sole authority on the sale rate and on how many characters a payment
buys. The host never sees a rate: it holds a per-user character balance, holds
the estimated characters before a run, and applies the plugin's reported count
after. `sale` on the quote/report context is what tells the plugin the run is
metered; every other run is pass-through and quotes the raw provider rate.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from host.contracts import Quote, Report, to_audio
from providers import Field
from providers.azure import Azure as _Impl, PRICE_PER_1M_USD
from providers._accounting import AccountingMixin

# Defaults for the two sale fields. Both are connection settings, so an operator
# can tune them per deployment without a code change.
DEFAULT_MARKUP = 1.25
DEFAULT_INFRA_PER_1M = 0.0

# What the pricing page offers. The plugin, not the host, decides the bundles
# and how many characters each buys (rounded down).
BUNDLE_USD = (5, 10, 25, 50)


def _num(v, default):
    """A settings value that may be a string from a web form, or absent."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f


class Speech(_Impl, AccountingMixin):
    def settings(self):
        # The provider's own fields (key, region), then the resale fields. Only
        # the sale connection uses the last two; on any other connection they are
        # inert. They are not sensitive: they are price knobs, not secrets.
        return list(_Impl.settings(self)) + [
            # The markup is not a switch a reader-operator should be flicking on
            # a settings screen: it is our price, and the price belongs in the
            # one table we publish (Chris, 11-14). The field stays declared, so
            # a deployment can still set it, and the plugin still reads it.
            Field("markup", "Resale markup", "number", default=DEFAULT_MARKUP,
                  help="Price multiple over provider + infra cost for resold "
                       "characters. Only used on the designated sale connection.",
                  shown=False),
            Field("infra_per_1m", "Infra fee per 1M chars", "number",
                  default=DEFAULT_INFRA_PER_1M,
                  help="Per-1M-chars fee added before the markup. Only used on "
                       "the designated sale connection."),
        ]

    def synth(self, text, voice, opts, cred):
        return to_audio(_Impl.synth(self, text, voice, opts, cred))

    # ---- resale pricing: the plugin is the authority -----------------------

    def sale_rate_per_1m(self, cred):
        """USD per 1M chars to SELL at: (provider rate + infra) * markup.

        The fee lives here, not in the host, exactly as docs/PRICING.md requires:
        the user compares this rate against the provider's public price and sees
        the difference is our fee, with no hidden credit abstraction.
        """
        cred = cred or {}
        markup = _num(cred.get("markup"), DEFAULT_MARKUP)
        infra = _num(cred.get("infra_per_1m"), DEFAULT_INFRA_PER_1M)
        if markup <= 0:
            markup = DEFAULT_MARKUP
        if infra < 0:
            infra = 0.0
        return (PRICE_PER_1M_USD + infra) * markup

    def chars_for_payment(self, usd, cred):
        """Characters a payment of `usd` buys, rounded DOWN.

        We never owe more characters than the money covers, so the rounding
        error lands on us, not the buyer (Chris, 2026-10-20).
        """
        usd = _num(usd, 0.0)
        rate = self.sale_rate_per_1m(cred)
        if usd <= 0 or rate <= 0:
            return 0
        return int(usd / rate * 1_000_000)

    def bundles(self, cred):
        """The priced bundles the client shows: a USD price and the character
        count it buys at the current rate."""
        return [{"usd": float(a), "chars": self.chars_for_payment(a, cred)}
                for a in BUNDLE_USD]

    # ---- accounting: metered in characters on the sale connection ----------

    def quote(self, ctx):
        ctx = ctx or {}
        chars = int(ctx.get("chars_est") or 0)
        if not ctx.get("sale"):
            # pass-through: the raw provider rate, in USD, no reserve in chars
            return AccountingMixin.quote(self, ctx)
        # A metered run reserves its estimated characters. `price` is the amount
        # the host holds, and its unit is `currency` - here, characters. The host
        # moves the number without knowing what it counts.
        return Quote(chars_est=chars, price=float(chars), currency="chars",
                     eligible=True, authoritative=True)

    def report(self, ctx):
        ctx = ctx or {}
        if not ctx.get("sale"):
            return AccountingMixin.report(self, ctx)
        actual = int(ctx.get("chars_actual") or 0)
        return Report(charge="charged", chars_actual=actual,
                      cost=float(actual), currency="chars")
