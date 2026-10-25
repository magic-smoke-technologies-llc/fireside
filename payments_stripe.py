"""Stripe - the first registered payment provider.

This is the only file in the tree that talks to Stripe. The host knows nothing
about it: the deployment registers it by import path
(`FIRESIDE_PAYMENT_PROVIDER=payments_stripe`) and the key is read from the
environment at that moment, never from this file and never from the repository.

It speaks the Stripe REST API over the standard library (urllib), so it adds no
package to the tree: a clean CI checkout with nothing installed still imports it.
The honesty rule is host/payments.py's: `charge` returns
ok=True only when Stripe says the charge actually settled, and `confirm` returns a
subscription only when Stripe says the checkout is complete and paid. Anything
else (requires_action, processing, a declined card, an abandoned session) is not
money and records nothing.

Test vs live is the key: a `sk_test_` key charges test cards, a `sk_live_` key
charges real ones. Card entry belongs in the client; until that lands, `charge`
accepts a `payment_method` from the caller and, in test mode only, falls back to
Stripe's canned test card. The recurring tier uses Stripe-hosted Checkout, so the
client never sees a card at all - it just opens the URL.
"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from host.payments import Checkout, Plan, Purchase, Receipt, Subscription

STRIPE_API = os.environ.get("STRIPE_API_BASE", "https://api.stripe.com").rstrip("/")

# How long a signed webhook stays acceptable. Stripe's own guidance is 5 minutes;
# nothing older is trusted, so a captured body cannot be replayed later.
WEBHOOK_TOLERANCE_SECONDS = 300


def verify_signature(raw, sig_header, secret, tolerance=WEBHOOK_TOLERANCE_SECONDS):
    """True when `sig_header` is a valid Stripe signature over `raw`.

    Stripe's scheme: the header is `t=<unix>,v1=<hex>[,v1=<hex>...]`, and the
    signed payload is `"<t>.<raw body>"` HMAC-SHA256'd with the endpoint's
    signing secret. Any `v1` may match (Stripe rotates). A timestamp older than
    `tolerance` is refused so a replay is not accepted. The comparison is
    constant-time.
    """
    if not raw or not sig_header or not secret:
        return False
    parts = {}
    for piece in sig_header.split(","):
        k, _, v = piece.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        t = int((parts.get("t") or [""])[0])
    except (TypeError, ValueError):
        return False
    if abs(time.time() - t) > tolerance:
        return False
    signed = (str(t) + ".").encode() + (raw if isinstance(raw, bytes)
                                         else str(raw).encode())
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    for cand in parts.get("v1") or []:
        if hmac.compare_digest(expected, cand):
            return True
    return False


def _basic_auth(key):
    # Stripe uses HTTP basic auth with the secret key as the username.
    return "Basic " + base64.b64encode((key + ":").encode()).decode()


class _Response:
    """The slice of a `requests` response the provider reads: status + .json()."""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return json.loads(self._body or "{}")


def _http_post(url, data=None, auth=None, timeout=None, headers=None):
    body = urllib.parse.urlencode(data or {}, doseq=True).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if auth:
        req.add_header("Authorization", _basic_auth(auth[0]))
    try:
        with urllib.request.urlopen(req, timeout=timeout or 20) as r:
            return _Response(getattr(r, "status", 200), r.read().decode())
    except urllib.error.HTTPError as e:
        # Stripe error bodies (card declined, etc.) arrive as HTTP 4xx with JSON.
        return _Response(e.code, e.read().decode())


def _http_get(url, params=None, auth=None, timeout=None):
    if params:
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, method="GET")
    if auth:
        req.add_header("Authorization", _basic_auth(auth[0]))
    try:
        with urllib.request.urlopen(req, timeout=timeout or 20) as r:
            return _Response(getattr(r, "status", 200), r.read().decode())
    except urllib.error.HTTPError as e:
        return _Response(e.code, e.read().decode())

# The plans this provider can sell. The provider is the sole authority on price.
# The platform tier is the recurring one; character capacity is not a plan - it is
# bought one-shot through charge(). The storage add-on is the second recurring
# plan: a monthly block you buy N of, which the host records as an add-on rather
# than a tier. Its byte size lives in host/quota.py (STORAGE_BLOCK_BYTES).
DEFAULT_PLANS = [
    Plan(id="platform", label="fireside platform", amount_cents=300,
         currency="usd", interval="month",
         description="Server, updates, and 2GB storage. The platform only."),
    Plan(id="storage", label="fireside storage +5 GB", amount_cents=200,
         currency="usd", interval="month",
         description="Add 5 GB of storage, monthly. Buy as many blocks as you need."),
]


class StripeProvider:
    id = "stripe"

    def __init__(self, secret_key, api_base=STRIPE_API, timeout=20, post=None,
                 plans=None, webhook_secret=""):
        self.key = secret_key
        self.api_base = (api_base or STRIPE_API).rstrip("/")
        self.timeout = timeout
        self._post = post or _http_post
        self._plans = list(plans) if plans is not None else list(DEFAULT_PLANS)
        self.webhook_secret = (webhook_secret or "").strip()

    # -- helpers -------------------------------------------------------------

    def _auth(self):
        # Stripe uses HTTP basic auth with the secret key as the username.
        return (self.key, "")

    def _get(self, path, params=None):
        fn = getattr(self._post, "get", None)
        if callable(fn):
            return fn(self.api_base + path, params=params, auth=self._auth(),
                      timeout=self.timeout)
        return _http_get(self.api_base + path, params=params,
                         auth=self._auth(), timeout=self.timeout)

    def _send(self, path, data, headers=None):
        """POST, carrying an Idempotency-Key when the caller asked for one.

        The injected transport used by the tests takes only (url, data, auth,
        timeout), so a header is added only on the real transport; a test that
        cares about the header patches `_http_post` instead of `post`.
        """
        fn = self._post
        if headers and fn is _http_post:
            return _http_post(self.api_base + path, data=data, auth=self._auth(),
                              timeout=self.timeout, headers=headers)
        return fn(self.api_base + path, data=data, auth=self._auth(),
                  timeout=self.timeout)

    def live(self):
        # A standard secret key is sk_live_/sk_test_; a restricted key is
        # rk_live_/rk_test_. Either is live when the mode segment says live.
        return self.key.startswith(("sk_live_", "rk_live_"))

    def account(self):
        """GET /v1/account - proves the key is real at startup, no charge made."""
        return self._get("/v1/account")

    # -- character capacity (one-shot) ---------------------------------------

    def _pm(self, meta):
        """The payment method to confirm with.

        A client sends `payment_method` (a token/element). If it does not, we only
        ever fall back to Stripe's canned test card, and only under a test key -
        a live key with no payment method must refuse, not guess.
        """
        pm = (meta or {}).get("payment_method")
        if pm:
            return pm
        if not self.live():
            return os.environ.get("STRIPE_TEST_PAYMENT_METHOD", "pm_card_visa")
        return ""

    def charge(self, usd, meta=None, idem=""):
        meta = meta or {}
        try:
            amount = int(round(float(usd) * 100))
        except (TypeError, ValueError):
            return Receipt(False, "", "bad amount")
        if amount <= 0:
            return Receipt(False, "", "amount must be greater than zero")

        pm = self._pm(meta)
        if not pm:
            return Receipt(False, "", "no payment method supplied")

        data = {
            "amount": amount,
            "currency": "usd",
            "confirm": "true",
            "payment_method": pm,
            "description": meta.get("description", "fireside character credits"),
            "metadata[user]": str(meta.get("user", "")),
            "metadata[provider]": str(meta.get("provider", "")),
            "metadata[chars]": str(meta.get("chars", "")),
        }
        try:
            r = self._send("/v1/payment_intents", data, _idem(idem))
        except (urllib.error.URLError, OSError) as e:
            return Receipt(False, "", "stripe unreachable: %s" % e)
        try:
            j = r.json()
        except ValueError:
            return Receipt(False, "", "stripe returned non-JSON (http %d)"
                           % r.status_code)
        if r.status_code != 200:
            msg = (j.get("error") or {}).get("message") or ("http %d" % r.status_code)
            return Receipt(False, "", msg)

        status = j.get("status", "")
        if status == "succeeded":
            return Receipt(True, j.get("id", ""), "succeeded")
        # requires_action / processing / requires_payment_method: the money has
        # NOT settled, so the host must not credit characters.
        return Receipt(False, j.get("id", ""), "not settled (status=%s)" % status)

    # -- the recurring platform tier -----------------------------------------

    def plans(self):
        return list(self._plans)

    def _plan(self, plan_id):
        for p in self._plans:
            if p.id == plan_id:
                return p
        return None

    def checkout(self, plan_id, meta=None, idem=""):
        """Create a Stripe-hosted Checkout Session, return its URL.

        Two shapes. A single plan: `plan_id` with `meta['quantity']` buying more
        than one of it (storage blocks). A cart: `meta['items']` is a list of
        `{"plan": id, "quantity": n}` line items, each priced from this
        provider's own price list. The tier and its storage add-on go out as one
        subscription, so a single payment settles both at once.
        """
        meta = meta or {}
        items = meta.get("items") or None
        if items:
            return self._checkout_items(items, meta, idem=idem)
        plan = self._plan(plan_id)
        if plan is None:
            return Checkout(False, detail="unknown plan %r" % plan_id)
        try:
            quantity = max(1, int(meta.get("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1
        return_url = (meta.get("return_url") or "").strip()
        if return_url:
            sep = "&" if "?" in return_url else "?"
            success_url = return_url + sep + "checkout={CHECKOUT_SESSION_ID}"
        else:
            success_url = ""
        data = {
            "mode": "subscription" if plan.interval else "payment",
            "metadata[user]": str(meta.get("user", "")),
            "metadata[plan]": plan.id,
            "metadata[quantity]": str(quantity),
        }
        _line(data, 0, plan, quantity)
        if success_url:
            data["success_url"] = success_url
            data["cancel_url"] = return_url
        return self._session(data, idem=idem)

    def _checkout_items(self, items, meta, idem=""):
        """A cart: one line item per entry, all on one subscription session.

        Only names this provider actually prices are accepted - an unknown plan
        refuses the whole session rather than charging a guessed amount. The
        items are echoed into metadata so confirm() can apply each one.
        """
        out = []
        for it in items:
            pid = (it or {}).get("plan") or ""
            plan = self._plan(pid)
            if plan is None:
                return Checkout(False, detail="unknown plan %r" % pid)
            try:
                qty = max(1, int(it.get("quantity") or 1))
            except (TypeError, ValueError):
                qty = 1
            out.append({"plan": plan.id, "quantity": qty, "interval": plan.interval,
                        "amount_cents": plan.amount_cents, "currency": plan.currency,
                        "label": plan.label or plan.id,
                        "price": getattr(plan, "price", "") or ""})
        if not out:
            return Checkout(False, detail="empty cart")
        recurring = all(bool(o["interval"]) for o in out)
        if not recurring:
            return Checkout(False, detail="cart mixes recurring and one-time items")
        return_url = (meta.get("return_url") or "").strip()
        if return_url:
            sep = "&" if "?" in return_url else "?"
            success_url = return_url + sep + "checkout={CHECKOUT_SESSION_ID}"
        else:
            success_url = ""
        data = {
            "mode": "subscription",
            "metadata[user]": str(meta.get("user", "")),
            "metadata[items]": json.dumps([{"plan": o["plan"], "quantity": o["quantity"]}
                                           for o in out]),
        }
        for i, o in enumerate(out):
            _line(data, i, o, o["quantity"])
        if success_url:
            data["success_url"] = success_url
            data["cancel_url"] = return_url
        return self._session(data, idem=idem)

    def _session(self, data, idem=""):
        # Stripe now turns Managed Payments on by default on some accounts, and a
        # session made while it is on demands a tax_code on every product and hands
        # Stripe the merchant of record (its 3.5% on top of processing, and it owns
        # the sale). fireside sells as itself: Magic Smoke is the merchant, the tax
        # position stays ours, and the fee stays at processing. Say so explicitly
        # per session instead of relying on an account-level default we do not own.
        data.setdefault("managed_payments[enabled]", "false")
        try:
            r = self._send("/v1/checkout/sessions", data, _idem(idem))
        except (urllib.error.URLError, OSError) as e:
            return Checkout(False, detail="stripe unreachable: %s" % e)
        try:
            j = r.json()
        except ValueError:
            return Checkout(False, detail="stripe returned non-JSON (http %d)"
                            % r.status_code)
        if r.status_code != 200:
            msg = (j.get("error") or {}).get("message") or ("http %d" % r.status_code)
            return Checkout(False, detail=msg)
        return Checkout(True, j.get("url", ""), j.get("id", ""))

    # -- character capacity, bought through Checkout --------------------------
    # A browser must never post a card to us, and a PaymentIntent built on the
    # server needs a payment method it does not have. So characters are bought
    # exactly like the tier: a hosted Checkout Session in payment mode, priced
    # by this provider, with the character count in metadata. The webhook (or
    # the browser's return) reads the session back and credits it once.

    def purchase_checkout(self, usd, meta=None, idem=""):
        meta = meta or {}
        try:
            amount = int(round(float(usd) * 100))
        except (TypeError, ValueError):
            return Checkout(False, detail="bad amount")
        if amount <= 0:
            return Checkout(False, detail="amount must be greater than zero")
        chars = int(meta.get("chars") or 0)
        if chars <= 0:
            return Checkout(False, detail="no characters priced for this amount")
        return_url = (meta.get("return_url") or "").strip()
        data = {
            "mode": "payment",
            "metadata[kind]": "characters",
            "metadata[user]": str(meta.get("user", "")),
            "metadata[provider]": str(meta.get("provider", "")),
            "metadata[chars]": str(chars),
            "metadata[usd]": "%.2f" % float(usd),
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": "usd",
            "line_items[0][price_data][unit_amount]": str(amount),
            "line_items[0][price_data][product_data][name]":
                meta.get("description") or "%d characters" % chars,
        }
        if return_url:
            sep = "&" if "?" in return_url else "?"
            data["success_url"] = return_url + sep + "checkout={CHECKOUT_SESSION_ID}"
            data["cancel_url"] = return_url
        return self._session(data, idem=idem)

    def purchase_confirm(self, reference):
        """Read a one-shot Checkout Session back and say what it bought."""
        ref = (reference or "").strip()
        if not ref:
            return Purchase(False, detail="no checkout reference")
        r = self._get("/v1/checkout/sessions/" + ref)
        try:
            j = r.json()
        except ValueError:
            return Purchase(False, reference=ref,
                            detail="stripe returned non-JSON (http %d)" % r.status_code)
        if r.status_code != 200:
            msg = (j.get("error") or {}).get("message") or ("http %d" % r.status_code)
            return Purchase(False, reference=ref, detail=msg)
        md = j.get("metadata") or {}
        user = md.get("user", "")
        try:
            chars = int(md.get("chars") or 0)
        except (TypeError, ValueError):
            chars = 0
        if md.get("kind") != "characters":
            return Purchase(False, user=user, reference=ref,
                            detail="not a character purchase")
        if j.get("status") != "complete":
            return Purchase(False, user=user, chars=chars, reference=ref,
                            detail="checkout not complete (status=%s)"
                                   % j.get("status", ""))
        if j.get("payment_status") not in ("paid", "no_payment_required"):
            return Purchase(False, user=user, chars=chars, reference=ref,
                            detail="not paid (status=%s)" % j.get("payment_status", ""))
        if chars <= 0:
            return Purchase(False, user=user, reference=ref, detail="no characters")
        return Purchase(True, user=user, chars=chars,
                        provider=md.get("provider", ""), reference=ref,
                        detail="ok")

    # -- managing a live subscription ----------------------------------------

    def cancel(self, subscription_id, at_period_end=True):
        """Stop the renewal. `at_period_end` keeps what was already paid for.

        Cancelling immediately would take away days the buyer paid for and, on a
        failed card, would not recover the money anyway; so the default is the
        one both honest and useful: no further invoices, access until the period
        they already bought ends.
        """
        sid = (subscription_id or "").strip()
        if not sid:
            return {"ok": False, "detail": "no subscription on file"}
        data = {"cancel_at_period_end": "true" if at_period_end else "false"}
        r = self._send("/v1/subscriptions/" + urllib.parse.quote(sid), data)
        try:
            j = r.json()
        except ValueError:
            return {"ok": False, "detail": "stripe returned non-JSON (http %d)"
                                           % r.status_code}
        if r.status_code != 200:
            msg = (j.get("error") or {}).get("message") or ("http %d" % r.status_code)
            return {"ok": False, "detail": msg}
        return {"ok": True,
                "status": j.get("status", ""),
                "cancel_at_period_end": bool(j.get("cancel_at_period_end")),
                "current_period_end": int(j.get("current_period_end") or 0)}

    def portal(self, customer, return_url=""):
        """A Stripe-hosted page where a buyer changes their own card or cancels.

        Card details never touch us, and a plan change is priced by Stripe from
        the Prices in the catalog. Needs the customer id for the subscription.
        """
        cid = (customer or "").strip()
        if not cid:
            return {"ok": False, "detail": "no customer on file"}
        data = {"customer": cid}
        if return_url:
            data["return_url"] = return_url
        r = self._send("/v1/billing_portal/sessions", data)
        try:
            j = r.json()
        except ValueError:
            return {"ok": False, "detail": "stripe returned non-JSON (http %d)"
                                           % r.status_code}
        if r.status_code != 200:
            msg = (j.get("error") or {}).get("message") or ("http %d" % r.status_code)
            return {"ok": False, "detail": msg}
        return {"ok": True, "url": j.get("url", "")}

    # -- lifecycle events -----------------------------------------------------
    # A subscription does not stay alive by itself. Stripe tells us when it
    # renews, when a card fails, and when it is cancelled; if we only ever acted
    # on checkout.session.completed the paid tier would never lapse and a failed
    # card would buy another month.

    def subscription_state(self, event):
        """The new state of a subscription, from a lifecycle event, or None.

        `invoice.*` events name a subscription too, and are the earliest signal
        that a renewal charged nothing. Everything is read from the verified
        event body only - no lookup, so applying one is cheap and a retry is a
        no-op.
        """
        typ = (event or {}).get("type", "")
        obj = ((event or {}).get("data") or {}).get("object") or {}
        sid = ""
        customer = obj.get("customer", "") or ""
        status = ""
        period_end = 0
        if typ.startswith("customer.subscription."):
            sid = obj.get("id", "") or ""
            status = obj.get("status", "") or ""
            period_end = int(obj.get("current_period_end") or 0)
            if isinstance(customer, dict):
                customer = customer.get("id", "") or ""
        elif typ in ("invoice.paid", "invoice.payment_failed",
                     "invoice.payment_action_required", "invoice.payment_succeeded"):
            sub = obj.get("subscription") or ""
            sid = sub.get("id", "") if isinstance(sub, dict) else str(sub or "")
            status = {"invoice.payment_failed": "past_due",
                      "invoice.payment_action_required": "past_due"}.get(typ, "active")
            period_end = int(obj.get("period_end") or 0)
            if isinstance(customer, dict):
                customer = customer.get("id", "") or ""
        if not sid:
            return None
        return {"subscription_id": sid, "customer": customer,
                "status": status, "current_period_end": period_end, "event": typ}


    # -- webhooks -------------------------------------------------------------
    # A buyer who closes the tab after paying must still get their plan. The
    # webhook is the server-to-server truth; the browser's /billing/confirm is
    # the fast path. Both land in the same place, and applying twice is a no-op.

    def verify_webhook(self, raw, sig_header):
        """(event, "") when the signature proves Stripe sent this body, else
        (None, reason). Nothing is trusted before the signature checks out, so a
        forged POST to the public webhook route changes nothing."""
        if not self.webhook_secret:
            return None, "webhook secret not configured"
        if not verify_signature(raw, sig_header or "", self.webhook_secret):
            return None, "signature verification failed"
        try:
            return json.loads(raw.decode("utf-8")), ""
        except (ValueError, UnicodeDecodeError, AttributeError):
            return None, "bad payload"

    def webhook_reference(self, event):
        """The Checkout Session id a settled event names, or "" for an event we
        do not act on. Only a completed (or asynchronously paid) checkout is a
        sale; every other event type is acknowledged and ignored."""
        typ = (event or {}).get("type", "")
        if typ not in ("checkout.session.completed",
                       "checkout.session.async_payment_succeeded"):
            return ""
        obj = ((event or {}).get("data") or {}).get("object") or {}
        return obj.get("id", "") or ""

    def confirm(self, reference):
        """Read a Checkout Session back and say whether it is paid + active."""
        ref = (reference or "").strip()
        if not ref:
            return Subscription(False, detail="no checkout reference")
        r = self._get("/v1/checkout/sessions/" + ref, params={"expand[]": "subscription"})
        try:
            j = r.json()
        except ValueError:
            return Subscription(False, detail="stripe returned non-JSON (http %d)"
                                % r.status_code)
        if r.status_code != 200:
            msg = (j.get("error") or {}).get("message") or ("http %d" % r.status_code)
            return Subscription(False, reference=ref, detail=msg)
        md = j.get("metadata") or {}
        plan = md.get("plan", "")
        user = md.get("user", "")
        items = []
        raw = md.get("items")
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    items = [{"plan": str((x or {}).get("plan", "")),
                              "quantity": int((x or {}).get("quantity") or 1)}
                             for x in parsed if (x or {}).get("plan")]
            except (ValueError, TypeError):
                items = []
        try:
            quantity = int(md.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        complete = j.get("status") == "complete"
        paid = j.get("payment_status") in ("paid", "no_payment_required")
        sub = j.get("subscription") or {}
        if isinstance(sub, str) or not sub:
            sub = {}
        period_end = int(sub.get("current_period_end") or 0)
        subscription_id = sub.get("id", "") or ""
        if isinstance(j.get("subscription"), str):
            subscription_id = j.get("subscription", "")
        status = sub.get("status") or ("complete" if complete else j.get("status", ""))
        if not complete:
            return Subscription(False, user=user, plan=plan, status=j.get("status", ""),
                                reference=ref, detail="checkout not complete")
        if not paid:
            return Subscription(False, user=user, plan=plan, status=status,
                                reference=ref, detail="not paid")
        return Subscription(True, user=user, plan=plan, status=status or "active",
                            current_period_end=period_end, reference=ref,
                            detail="ok", quantity=quantity, items=items,
                            customer=j.get("customer", "") or "",
                            subscription_id=subscription_id)


def _idem(key):
    """The header dict for a caller-supplied Idempotency-Key, or None."""
    key = (key or "").strip()
    return {"Idempotency-Key": key} if key else None


def _line(data, i, plan, quantity):
    """One checkout line item, from a stored Price when we have one.

    `plan` is a Plan or the plain dict the cart builds. With a `price` id the
    line points at the real Price object, so the Stripe dashboard shows a named
    product, renewals group, and the Customer Portal can reason about the
    subscription. Without one we fall back to an inline price, which works but
    leaves an anonymous one-off price behind on every checkout.
    """
    get = (lambda k, d=None: plan.get(k, d)) if isinstance(plan, dict) \
        else (lambda k, d=None: getattr(plan, k, d))
    b = "line_items[%d]" % i
    data[b + "[quantity]"] = str(quantity)
    price = get("price") or ""
    if price:
        data[b + "[price]"] = price
        return
    data[b + "[price_data][currency]"] = get("currency") or "usd"
    data[b + "[price_data][unit_amount]"] = str(get("amount_cents") or 0)
    data[b + "[price_data][product_data][name]"] = get("label") or get("id") or "fireside"
    if get("interval"):
        data[b + "[price_data][recurring][interval]"] = get("interval")

def from_env():
    """Build the provider from $FIRESIDE_STRIPE_KEY, or None if unset."""
    key = os.environ.get("FIRESIDE_STRIPE_KEY", "").strip()
    if not key:
        return None
    # sk_ = standard secret key, rk_ = restricted key. Both are valid server
    # credentials; the restricted one just carries fewer granted permissions.
    if not key.startswith(("sk_", "rk_")):
        raise ValueError("FIRESIDE_STRIPE_KEY is not a Stripe key (sk_... or rk_...)")
    whsec = (os.environ.get("FIRESIDE_STRIPE_WEBHOOK_SECRET")
             or os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip()
    return StripeProvider(key, webhook_secret=whsec, plans=_plans_from_env())


# The Price ids are deployment configuration, not code: one product per plan,
# created once in the Stripe dashboard or by create_prices(), then pinned here so
# the catalog in the app and the catalog in Stripe cannot drift apart.
PRICE_ENV = {"platform": "FIRESIDE_STRIPE_PRICE_PLATFORM",
             "storage": "FIRESIDE_STRIPE_PRICE_STORAGE"}


def _plans_from_env(environ=None):
    """The default plans, with any configured Stripe Price ids attached."""
    env = os.environ if environ is None else environ
    out = []
    for p in DEFAULT_PLANS:
        pid = (env.get(PRICE_ENV.get(p.id, ""), "") or "").strip()
        out.append(Plan(id=p.id, label=p.label, amount_cents=p.amount_cents,
                        currency=p.currency, interval=p.interval,
                        description=p.description, price=pid))
    return out


def create_prices(provider, name_prefix="fireside"):
    """Create the Product+Price pair for every default plan, print the ids.

    Run once per Stripe mode (a test-mode Price does not exist in live mode),
    then put the ids in FIRESIDE_STRIPE_PRICE_*. This is a deliberate one-off:
    the app never creates catalog objects while serving a checkout.
    """
    made = []
    for p in DEFAULT_PLANS:
        prod = provider._post(provider.api_base + "/v1/products",
                              data={"name": p.label or p.id,
                                    "description": p.description or ""},
                              auth=provider._auth(), timeout=provider.timeout)
        if prod.status_code >= 300:
            raise RuntimeError("product failed: %s" % prod.json())
        product_id = prod.json().get("id", "")
        data = {"product": product_id, "currency": p.currency,
                "unit_amount": str(p.amount_cents),
                "nickname": "%s_%s" % (name_prefix, p.id)}
        if p.interval:
            data["recurring[interval]"] = p.interval
        pr = provider._post(provider.api_base + "/v1/prices", data=data,
                            auth=provider._auth(), timeout=provider.timeout)
        if pr.status_code >= 300:
            raise RuntimeError("price failed: %s" % pr.json())
        made.append({"plan": p.id, "product": product_id,
                     "price": pr.json().get("id", ""),
                     "amount_cents": p.amount_cents,
                     "env": PRICE_ENV.get(p.id, "")})
    return made
