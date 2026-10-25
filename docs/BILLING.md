# Billing: the money path, and what it must never do

Written after a pre-launch audit (2026-10-25) of every route that moves money,
before the live key went on. Six holes were real. This file is the shape they
fixed, so the next change can be checked against it instead of against a memory.

## The one rule

**A sale is settled money, and nothing else is a sale.** `ok` comes from the
provider, never from a form post: a declined card, `requires_action`, an
abandoned checkout, an unpaid invoice — none of those credit anything and none
of them grant a tier.

## Two things money buys, two provider shapes

| what | how it is bought | what the host stores |
|---|---|---|
| character capacity | hosted Checkout, `mode=payment`, the character count pinned in metadata | a character balance, and one `payment_events` row per session |
| the platform tier (+ storage blocks) | hosted Checkout, `mode=subscription`, priced per line item | plan, status, period end, **customer** and **subscription** ids |

Card details never reach this server. The browser is sent to the provider and
comes back with a session id, which the host reads back before it believes
anything.

## The lifecycle (what a subscription actually needs)

A subscription does not stay alive on its own.

- `checkout.session.completed` — the sale. Confirms, stores the ids, applies.
- `customer.subscription.updated` / `.deleted` — renewals, plan changes, and
  cancellations. Applied to the row that names that subscription id.
- `invoice.payment_failed` / `invoice.paid` — a card that stopped working, and
  the renewal that worked. `payment_failed` becomes `past_due`, which is not an
  active tier.

And a belt behind the webhooks: `billing.active()` treats a row as lapsed once
`current_period_end` is more than three days past. Webhooks are the authority,
but a delivery that never arrives must not buy a free month.

**Stored ids matter as much as stored status.** A cancel names the
subscription, a portal session names the customer, and neither can be derived
from the checkout session id. An event that arrives without them never blanks
the ones already stored (`COALESCE(NULLIF(excluded.x,''), table.x)`).

## Idempotency, both ends

- The client sends an `Idempotency-Key` on every purchase and checkout. Stripe
  dedupes on its side; the host remembers the first answer for that key and
  replays it, so a double-tap starts one session.
- Every credit is claimed by `payment_events.id` (the provider object id) before
  it is applied. A webhook retry, a browser reload, and the two of them racing
  each other all credit once.
- Applying a subscription is an upsert, so the browser confirm and the webhook
  can both land in either order.

## Ownership

A session id is not a capability. `/billing/confirm` and `/credits/confirm`
compare the session's `metadata.user` (or the subscription's account) with the
signed-in user, and refuse otherwise. Return URLs are restricted to our own
origin — a caller-supplied URL would let Stripe bounce a real, paid session id
to someone else's site.

## Leaving

- Cancel sets `cancel_at_period_end`: no further invoices, access until the end
  of the period already paid for.
- The Customer Portal handles a new card.
- Deleting an account cancels the subscription **first**, then purges. If the
  cancel fails the deletion still happens (a person must be able to leave), but
  a `cancel_failed` row is left behind so an operator can finish it by hand.

## Meters

Register, login, forgot, purchase, and checkout are metered. They are the doors
that write a row, send a mail, or start a charge, and unmetered they are someone
else's tool.

## What an operator must set before turning sales on

1. `FIRESIDE_PAYMENT_PROVIDER=payments_stripe` and `FIRESIDE_STRIPE_KEY` (live
   key; a restricted key with the right scopes is preferred).
2. The webhook endpoint in Stripe: `https://<host>/webhook/stripe`, subscribed to
   `checkout.session.completed`, `customer.subscription.*`, and `invoice.paid` /
   `invoice.payment_failed` / `invoice.payment_action_required`. Its signing
   secret goes in `FIRESIDE_STRIPE_WEBHOOK_SECRET`.
3. `FIRESIDE_BASE_URL=https://<host>` — or trust `X-Forwarded-Proto`, which the
   ingress sets; without one of them every return URL is minted as `http://`.
4. Optionally the Price ids (`FIRESIDE_STRIPE_PRICE_*`) so the dashboard and the
   Customer Portal see named products.
