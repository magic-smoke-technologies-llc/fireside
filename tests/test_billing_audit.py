"""The money path, audited: six holes found before payments were switched on.

Each class names the defect it was written against, because the point of these
tests is not coverage - it is that the same mistake cannot come back quietly:

  * a stored subscription with no id, so nothing could cancel it (166)
  * a paid tier that never lapsed because renewals never arrived (167)
  * characters that a live key could never actually buy (168)
  * a retried purchase that charged or credited twice (169)
  * a checkout confirm that applied anyone's session to anyone (170)
  * unmetered doors that spend money or send mail (171)
"""

import json
import os
import tempfile
import unittest

import payments_stripe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from host import accounting, billing, connections, payments
from host.app import App
from host.db import connect
from host.payments import Checkout, Purchase, Subscription


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeTransport:
    """Records every call, answers from a per-path table."""

    def __init__(self, posts=None, gets=None):
        self.posts = posts or {}
        self.gets = gets or {}
        self.calls = []

    def __call__(self, url, data=None, auth=None, timeout=None, headers=None):
        self.calls.append({"url": url, "data": data, "headers": headers or {}})
        for frag, resp in self.posts.items():
            if frag in url:
                return resp
        return _Resp(200, {})

    def get(self, url, params=None, auth=None, timeout=None):
        self.calls.append({"url": url, "params": params, "method": "GET"})
        for frag, resp in self.gets.items():
            if frag in url:
                return resp
        return _Resp(200, {})


# --------------------------------------------------------------------------
# 166 + 167: the ids and the lapse
# --------------------------------------------------------------------------

class TestStoredSubscriptionIds(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")

    def test_a_checkout_stores_the_ids_a_cancel_needs(self):
        billing.upsert(self.conn, "u1", "platform", "active", provider="stripe",
                       reference="cs_1", current_period_end=2000000000,
                       customer="cus_1", subscription_id="sub_1")
        row = billing.get(self.conn, "u1")
        self.assertEqual(row["subscription_id"], "sub_1")
        self.assertEqual(row["customer"], "cus_1")
        self.assertEqual(billing.by_subscription(self.conn, "sub_1"), "u1")

    def test_a_later_event_without_ids_does_not_blank_them(self):
        # a lifecycle event knows the status but not the checkout session; it
        # must not wipe the ids that make a cancel possible
        billing.upsert(self.conn, "u1", "platform", "active", reference="cs_1",
                       current_period_end=2000000000, customer="cus_1",
                       subscription_id="sub_1")
        billing.upsert(self.conn, "u1", "platform", "past_due",
                       current_period_end=2000000000)
        row = billing.get(self.conn, "u1")
        self.assertEqual(row["subscription_id"], "sub_1")
        self.assertEqual(row["customer"], "cus_1")
        self.assertEqual(row["status"], "past_due")

    def test_a_known_subscription_maps_to_its_account_from_either_table(self):
        billing.upsert_addon(self.conn, "u2", billing.STORAGE_ADDON, 2, "active",
                             customer="cus_2", subscription_id="sub_2")
        self.assertEqual(billing.by_subscription(self.conn, "sub_2"), "u2")
        self.assertEqual(billing.by_subscription(self.conn, "sub_nope"), "")


class TestPaidTierLapses(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")

    def test_a_period_that_ended_is_not_active_forever(self):
        billing.upsert(self.conn, "u1", "platform", "active",
                       current_period_end=1_700_000_000)      # 2023
        self.assertFalse(billing.active(self.conn, "u1"))

    def test_the_grace_window_survives_a_delivery_outage(self):
        end = 1_700_000_000
        billing.upsert(self.conn, "u1", "platform", "active",
                       current_period_end=end)
        just_after = end + billing.LAPSE_GRACE_SECONDS - 60
        self.assertTrue(billing.active(self.conn, "u1", now_ts=just_after))
        self.assertFalse(billing.active(
            self.conn, "u1", now_ts=end + billing.LAPSE_GRACE_SECONDS + 60))

    def test_the_free_tier_has_no_period_and_never_lapses(self):
        billing.assign_free(self.conn, "u1")
        self.assertTrue(billing.active(self.conn, "u1", now_ts=4_000_000_000))

    def test_paid_storage_blocks_lapse_with_their_period(self):
        billing.upsert_addon(self.conn, "u1", billing.STORAGE_ADDON, 3, "active",
                             current_period_end=1_700_000_000)
        self.assertEqual(billing.blocks(self.conn, "u1"), 0)


# --------------------------------------------------------------------------
# 166 + 167: what the provider can now be asked to do
# --------------------------------------------------------------------------

class TestStripeManagement(unittest.TestCase):
    def test_cancel_stops_the_renewal_and_keeps_the_paid_period(self):
        t = FakeTransport(posts={"/v1/subscriptions/sub_1": _Resp(200, {
            "id": "sub_1", "status": "active", "cancel_at_period_end": True,
            "current_period_end": 2000000000})})
        p = payments_stripe.StripeProvider("sk_test_x", post=t)
        out = p.cancel("sub_1")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["cancel_at_period_end"])
        self.assertEqual(out["current_period_end"], 2000000000)
        self.assertEqual(t.calls[0]["data"]["cancel_at_period_end"], "true")

    def test_cancel_with_nothing_on_file_refuses(self):
        p = payments_stripe.StripeProvider("sk_test_x", post=FakeTransport())
        self.assertFalse(p.cancel("")["ok"])

    def test_portal_needs_a_customer_and_returns_a_url(self):
        t = FakeTransport(posts={"/v1/billing_portal/sessions": _Resp(
            200, {"url": "https://billing.stripe.com/p/session_1"})})
        p = payments_stripe.StripeProvider("sk_test_x", post=t)
        self.assertFalse(p.portal("")["ok"])
        out = p.portal("cus_1", "https://readfireside.com/")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["url"], "https://billing.stripe.com/p/session_1")
        self.assertEqual(t.calls[0]["data"]["customer"], "cus_1")

    def test_confirm_keeps_the_ids_so_a_cancel_is_possible_later(self):
        t = FakeTransport(gets={"/v1/checkout/sessions/cs_1": _Resp(200, {
            "id": "cs_1", "status": "complete", "payment_status": "paid",
            "customer": "cus_1",
            "metadata": {"user": "u1", "plan": "platform"},
            "subscription": {"id": "sub_1", "status": "active",
                             "current_period_end": 2000000000}})})
        p = payments_stripe.StripeProvider("sk_test_x", post=t)
        s = p.confirm("cs_1")
        self.assertTrue(s.ok, s.detail)
        self.assertEqual(s.subscription_id, "sub_1")
        self.assertEqual(s.customer, "cus_1")

    def test_a_deleted_subscription_becomes_a_lifecycle_state(self):
        p = payments_stripe.StripeProvider("sk_test_x", post=FakeTransport())
        ev = {"type": "customer.subscription.deleted",
              "data": {"object": {"id": "sub_1", "customer": "cus_1",
                                  "status": "canceled",
                                  "current_period_end": 2000000000}}}
        st = p.subscription_state(ev)
        self.assertEqual(st["subscription_id"], "sub_1")
        self.assertEqual(st["status"], "canceled")

    def test_a_failed_invoice_is_past_due_not_a_sale(self):
        p = payments_stripe.StripeProvider("sk_test_x", post=FakeTransport())
        ev = {"type": "invoice.payment_failed",
              "data": {"object": {"subscription": "sub_1", "customer": "cus_1",
                                  "period_end": 2000000000}}}
        st = p.subscription_state(ev)
        self.assertEqual(st["status"], "past_due")

    def test_an_event_with_no_subscription_is_ignored(self):
        p = payments_stripe.StripeProvider("sk_test_x", post=FakeTransport())
        self.assertIsNone(p.subscription_state(
            {"type": "charge.refunded", "data": {"object": {"id": "ch_1"}}}))


# --------------------------------------------------------------------------
# 168 + 169: buying characters without a card on our server, once
# --------------------------------------------------------------------------

class TestCharacterCheckout(unittest.TestCase):
    def test_a_purchase_is_a_payment_mode_checkout_with_the_count_pinned(self):
        t = FakeTransport(posts={"/v1/checkout/sessions": _Resp(
            200, {"id": "cs_9", "url": "https://pay.stripe/cs_9"})})
        p = payments_stripe.StripeProvider("sk_test_x", post=t)
        co = p.purchase_checkout(5.0, {"user": "u1", "chars": 333333,
                                       "provider": "azure"})
        self.assertTrue(co.ok, co.detail)
        d = t.calls[0]["data"]
        self.assertEqual(d["mode"], "payment")
        self.assertEqual(d["metadata[kind]"], "characters")
        self.assertEqual(d["metadata[chars]"], "333333")
        self.assertEqual(d["line_items[0][price_data][unit_amount]"], "500")
        self.assertNotIn("line_items[0][price_data][recurring][interval]", d)

    def test_an_amount_that_buys_nothing_is_refused(self):
        p = payments_stripe.StripeProvider("sk_test_x", post=FakeTransport())
        self.assertFalse(p.purchase_checkout(5.0, {"chars": 0}).ok)

    def test_reading_a_purchase_back_requires_paid_and_complete(self):
        t = FakeTransport(gets={"/v1/checkout/sessions/cs_9": _Resp(200, {
            "id": "cs_9", "status": "complete", "payment_status": "paid",
            "metadata": {"kind": "characters", "user": "u1", "chars": "333333",
                         "provider": "azure"}})})
        p = payments_stripe.StripeProvider("sk_test_x", post=t)
        buy = p.purchase_confirm("cs_9")
        self.assertTrue(buy.ok)
        self.assertEqual(buy.chars, 333333)
        self.assertEqual(buy.user, "u1")

    def test_an_unpaid_session_buys_nothing(self):
        t = FakeTransport(gets={"/v1/checkout/sessions/cs_9": _Resp(200, {
            "id": "cs_9", "status": "open", "payment_status": "unpaid",
            "metadata": {"kind": "characters", "user": "u1", "chars": "333333"}})})
        p = payments_stripe.StripeProvider("sk_test_x", post=t)
        self.assertFalse(p.purchase_confirm("cs_9").ok)

    def test_a_subscription_session_is_not_a_character_purchase(self):
        t = FakeTransport(gets={"/v1/checkout/sessions/cs_9": _Resp(200, {
            "id": "cs_9", "status": "complete", "payment_status": "paid",
            "metadata": {"user": "u1", "plan": "platform"}})})
        p = payments_stripe.StripeProvider("sk_test_x", post=t)
        self.assertFalse(p.purchase_confirm("cs_9").ok)


# --------------------------------------------------------------------------
# the host end: a fake provider that behaves like Stripe's two shapes
# --------------------------------------------------------------------------

class FakeStripe:
    id = "stripe"

    def __init__(self):
        self.checkout_calls = []
        self.purchases = {}
        self.cancelled = []
        self.credited_sessions = {}

    def plans(self):
        return []

    def checkout(self, plan, meta, idem=""):
        self.checkout_calls.append((plan, meta, idem))
        return Checkout(True, "https://pay.example/co_1", "cs_sub_1")

    def confirm(self, reference):
        return Subscription(True, user="u1", plan="platform", status="active",
                            current_period_end=2000000000, reference=reference,
                            customer="cus_1", subscription_id="sub_1")

    def purchase_checkout(self, usd, meta, idem=""):
        self.checkout_calls.append((usd, meta, idem))
        return Checkout(True, "https://pay.example/cs_buy", "cs_buy")

    def purchase_confirm(self, reference):
        row = self.purchases.get(reference)
        if row is None:
            return Purchase(False, reference=reference, detail="unknown session")
        return Purchase(True, user=row["user"], chars=row["chars"],
                        provider="azure", reference=reference, detail="ok")

    def cancel(self, subscription_id):
        self.cancelled.append(subscription_id)
        return {"ok": True, "status": "active", "cancel_at_period_end": True,
                "current_period_end": 2000000000}

    def portal(self, customer, return_url=""):
        return {"ok": True, "url": "https://billing.example/cus_1"}

    def verify_webhook(self, raw, sig):
        return json.loads(raw.decode("utf-8")), ""

    def webhook_reference(self, event):
        if event.get("type") in ("checkout.session.completed",):
            return ((event.get("data") or {}).get("object") or {}).get("id", "")
        return ""

    def subscription_state(self, event):
        typ = event.get("type", "")
        obj = ((event.get("data") or {}).get("object") or {})
        if typ.startswith("customer.subscription."):
            return {"subscription_id": obj.get("id", ""),
                    "customer": obj.get("customer", ""),
                    "status": obj.get("status", ""),
                    "current_period_end": int(obj.get("current_period_end") or 0),
                    "event": typ}
        return None


SPEECH = """
from host.contracts import Audio, Quote, Report


class Plugin:
    id = "sale"
    ext = "mp3"
    mime = "audio/mpeg"
    needs_key = False

    def settings(self):
        return [{"key": "api_key", "label": "Key", "type": "password"}]

    def list_voices(self, cred):
        return [{"id": "v", "label": "V"}]

    def synth(self, text, voice, opts, cred):
        return Audio(data=b"x")

    def cost_per_1m_chars(self, cred):
        return 12.0

    def sale_rate_per_1m(self, cred):
        return 15.0

    def chars_for_payment(self, usd, cred):
        return int(float(usd) / 15.0 * 1_000_000)

    def bundles(self, cred):
        return [{"usd": 5, "chars": 333333}]

    def quote(self, ctx):
        n = ctx.get("chars_est", 0)
        return Quote(chars_est=n, price=float(n), currency="chars")

    def report(self, ctx):
        return Report(charge="unknown", chars_actual=0)
"""


def _write_plugin(root, kind, name, body):
    d = os.path.join(root, kind, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "plugin.py"), "w") as f:
        f.write(body)
    with open(os.path.join(d, "plugin.json"), "w") as f:
        json.dump({"id": name, "kind": kind, "label": name, "version": "1",
                   "entry": "plugin.py", "class": "Plugin"}, f)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = os.path.join(self.tmp, "plugins")
        _write_plugin(root, "speech", "sale", SPEECH)
        # the host's own local storage plugin, so an account can be erased
        import shutil
        shutil.copytree(os.path.join(ROOT, "plugins", "storage", "local"),
                        os.path.join(root, "storage", "local"))
        self.app = App(root, db_path=os.path.join(self.tmp, "db.sqlite"),
                       storage_id="local")
        # a limiter that never refuses, so a test about billing is not a test
        # about the token bucket; the limiter gets its own cases below
        self.app.buy_limiter = None
        self.app.door_limiter = None
        self.conn = self.app.conn
        for uid in ("u1", "u2"):
            self.conn.execute(
                "INSERT OR IGNORE INTO users(id,name,created_at) VALUES(?,?,?)",
                (uid, uid, "2026-01-01"))
        self.conn.commit()
        cid = connections.upsert(self.conn, "u1", "sale", "c", {"api_key": "k"})
        self.app.sale_connection = {"connection_id": cid, "voice": "v"}
        self.fake = FakeStripe()
        payments.REGISTRY.clear()
        payments.register(self.fake)

    def tearDown(self):
        payments.REGISTRY.clear()


class TestCreditsThroughCheckout(Base):
    def test_a_purchase_returns_a_url_and_credits_nothing_yet(self):
        s, out = self.app.credits_purchase({}, {"user": "u1", "usd": 15})
        self.assertEqual(s, 200, out)
        self.assertEqual(out["status"], "awaiting_payment")
        self.assertTrue(out["url"].startswith("https://pay.example/"))
        self.assertEqual(out["chars"], 1_000_000)
        self.assertEqual(accounting.balance(self.conn, "u1"), 0)

    def test_confirming_it_credits_once_even_on_a_reload(self):
        self.fake.purchases["cs_buy"] = {"user": "u1", "chars": 1_000_000}
        s, out = self.app.credits_confirm({}, {"user": "u1", "reference": "cs_buy"})
        self.assertEqual(s, 200, out)
        self.assertTrue(out["credited"])
        self.assertEqual(out["balance"], 1_000_000)
        s, out = self.app.credits_confirm({}, {"user": "u1", "reference": "cs_buy"})
        self.assertEqual(s, 200, out)
        self.assertFalse(out["credited"])
        self.assertEqual(accounting.balance(self.conn, "u1"), 1_000_000)

    def test_another_accounts_purchase_cannot_be_applied_to_me(self):
        self.fake.purchases["cs_buy"] = {"user": "u1", "chars": 1_000_000}
        s, out = self.app.credits_confirm({}, {"user": "u2", "reference": "cs_buy"})
        self.assertEqual(s, 403, out)
        self.assertEqual(accounting.balance(self.conn, "u2"), 0)

    def test_a_webhook_lands_the_same_credits_once(self):
        self.fake.purchases["cs_buy"] = {"user": "u1", "chars": 1_000_000}
        event = {"type": "checkout.session.completed",
                 "data": {"object": {"id": "cs_buy",
                                     "metadata": {"kind": "characters"}}}}
        s, out = self.app.payment_webhook(
            json.dumps(event).encode(), "sig", "stripe")
        self.assertEqual(s, 200, out)
        self.assertTrue(out["applied"], out)
        s, out = self.app.payment_webhook(
            json.dumps(event).encode(), "sig", "stripe")
        self.assertFalse(out["applied"])
        s, out = self.app.credits_confirm({}, {"user": "u1", "reference": "cs_buy"})
        self.assertFalse(out["credited"])
        self.assertEqual(accounting.balance(self.conn, "u1"), 1_000_000)

    def test_a_retried_request_with_the_same_key_is_one_session(self):
        s1, a = self.app.credits_purchase({"_idem": "k-1"},
                                          {"user": "u1", "usd": 15})
        s2, b = self.app.credits_purchase({"_idem": "k-1"},
                                          {"user": "u1", "usd": 15})
        self.assertEqual((s1, s2), (200, 200))
        self.assertEqual(a["reference"], b["reference"])
        self.assertEqual(len(self.fake.checkout_calls), 1)

    def test_a_second_key_is_a_second_session(self):
        self.app.credits_purchase({"_idem": "k-1"}, {"user": "u1", "usd": 15})
        self.app.credits_purchase({"_idem": "k-2"}, {"user": "u1", "usd": 15})
        self.assertEqual(len(self.fake.checkout_calls), 2)


class TestTierRouting(Base):
    def test_confirm_refuses_a_session_that_belongs_to_someone_else(self):
        class Other(FakeStripe):
            def confirm(self, reference):
                return Subscription(True, user="u2", plan="platform",
                                    status="active", reference=reference)
        payments.REGISTRY.clear()
        payments.register(Other())
        s, out = self.app.billing_confirm({}, {"user": "u1", "reference": "cs_x"})
        self.assertEqual(s, 403, out)
        self.assertIsNone(billing.get(self.conn, "u1"))

    def test_a_paid_session_with_no_plan_grants_no_tier(self):
        class Blank(FakeStripe):
            def confirm(self, reference):
                return Subscription(True, user="u1", plan="", status="active",
                                    reference=reference)
        payments.REGISTRY.clear()
        payments.register(Blank())
        s, out = self.app.billing_confirm({}, {"user": "u1", "reference": "cs_x"})
        self.assertEqual(s, 200, out)
        self.assertIn("ignored", out)
        self.assertIsNone(billing.get(self.conn, "u1"))

    def test_the_return_url_must_be_our_own_origin(self):
        app = self.app
        self.assertEqual(app._safe_return("https://evil.example/", "https://readfireside.com"),
                         "https://readfireside.com")
        self.assertEqual(app._safe_return("https://readfireside.com/?x=1",
                                          "https://readfireside.com"),
                         "https://readfireside.com/?x=1")
        self.assertEqual(
            app._safe_return("https://readfireside.com.evil.example/",
                             "https://readfireside.com"),
            "https://readfireside.com")

    def test_cancel_uses_the_stored_subscription_id(self):
        billing.upsert(self.conn, "u1", "platform", "active", provider="stripe",
                       reference="cs_1", current_period_end=2000000000,
                       customer="cus_1", subscription_id="sub_1")
        s, out = self.app.billing_cancel({}, {"user": "u1"})
        self.assertEqual(s, 200, out)
        self.assertEqual(self.fake.cancelled, ["sub_1"])

    def test_cancel_with_nothing_on_file_says_so(self):
        s, out = self.app.billing_cancel({}, {"user": "u1"})
        self.assertEqual(s, 409, out)

    def test_portal_opens_for_a_customer_we_stored(self):
        billing.upsert(self.conn, "u1", "platform", "active", provider="stripe",
                       customer="cus_1", subscription_id="sub_1")
        s, out = self.app.billing_portal({}, {"user": "u1"})
        self.assertEqual(s, 200, out)
        self.assertIn("url", out)

    def test_a_lifecycle_event_lapses_the_row(self):
        billing.upsert(self.conn, "u1", "platform", "active", provider="stripe",
                       customer="cus_1", subscription_id="sub_1",
                       current_period_end=2000000000)
        self.assertTrue(billing.active(self.conn, "u1"))
        event = {"type": "customer.subscription.deleted",
                 "data": {"object": {"id": "sub_1", "customer": "cus_1",
                                     "status": "canceled",
                                     "current_period_end": 2000000000}}}
        s, out = self.app.payment_webhook(
            json.dumps(event).encode(), "sig", "stripe")
        self.assertEqual(s, 200, out)
        self.assertTrue(out["applied"], out)
        self.assertFalse(billing.active(self.conn, "u1"))

    def test_an_event_for_a_subscription_we_never_stored_changes_nothing(self):
        event = {"type": "customer.subscription.updated",
                 "data": {"object": {"id": "sub_ghost", "status": "canceled",
                                     "current_period_end": 1}}}
        s, out = self.app.payment_webhook(
            json.dumps(event).encode(), "sig", "stripe")
        self.assertEqual(s, 200, out)
        self.assertFalse(out["applied"])

    def test_deleting_an_account_stops_the_recurring_charge(self):
        billing.upsert(self.conn, "u1", "platform", "active", provider="stripe",
                       customer="cus_1", subscription_id="sub_1",
                       current_period_end=2000000000)
        s, out = self.app.account_delete(
            {"_user": "u1"}, {"confirm": "delete my account"})
        self.assertEqual(s, 200, out)
        self.assertEqual(self.fake.cancelled, ["sub_1"])
        self.assertTrue(out["billing"]["cancelled"])


class TestMeteredDoors(Base):
    def test_a_purchase_loop_is_refused_after_the_burst(self):
        from host.ratelimit import RateLimiter
        self.app.buy_limiter = RateLimiter(rate_per_min=1, burst=2)
        codes = [self.app.credits_purchase({}, {"user": "u1", "usd": 15})[0]
                 for _ in range(4)]
        self.assertEqual(codes[:2], [200, 200])
        self.assertEqual(codes[2:], [429, 429])

    def test_the_checkout_door_is_metered_too(self):
        from host.ratelimit import RateLimiter
        self.app.buy_limiter = RateLimiter(rate_per_min=1, burst=1)
        self.assertEqual(self.app.billing_checkout(
            {}, {"user": "u1", "plan": "platform"})[0], 200)
        self.assertEqual(self.app.billing_checkout(
            {}, {"user": "u1", "plan": "platform"})[0], 429)
