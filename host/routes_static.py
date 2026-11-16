"""The page itself and its static neighbours: the app shell, legal pages, PWA files.

A mixin of host.app.App: its methods are the handlers the route tables name,
moved here verbatim so no one file holds the whole server. They use the App's
own state (self.conn, self.host, ...) and each other through self.
"""

import json
import os
from host import connections, i18n, payments, plans, quota
from host.appsupport import ASSETS_DIR, HOST_VERSION, UI_DIR


class StaticRoutes:
    def ui(self, q, body):
        """Serve the browser client. The host owns its own UI; plugins ship none.

        /pricing is this same page, opened on the price list: one design and
        one checkout, instead of a second static page that drifted from the
        app it was selling.
        """
        with open(os.path.join(UI_DIR, "index.html"), "r", encoding="utf-8") as f:
            page = (f.read().replace("__HOST_VERSION__", HOST_VERSION)
                    .replace("__COMPANY__", os.environ.get("FIRESIDE_COMPANY", "Fireside"))
                    .replace("__CONTACT__", os.environ.get(
                        "FIRESIDE_CONTACT_EMAIL", "support@readfireside.com")))
        # The door's fire is licensed media that may not be in this build yet,
        # so the render says once whether it exists. The alternative is a
        # request on every door load that ends in 404 on a stock checkout.
        hearth = ""
        poster = ""
        if os.path.exists(os.path.join(ASSETS_DIR, "hearth.mp4")):
            hearth = "1"
            if os.path.exists(os.path.join(ASSETS_DIR, "hearth-poster.jpg")):
                poster = ' poster="/hearth-poster.jpg"'
        page = page.replace("__HEARTH_POSTER__", poster).replace("__HEARTH__", hearth)
        # Analytics is the operator's, not the product's: a self-hosted copy
        # ships no tag because there is no id in its environment. The page
        # refuses anything that is not a GA4 measurement id, so a typo is an
        # absent tag rather than a broken one.
        page = page.replace("__GA_ID__", (os.environ.get("FIRESIDE_GA_ID") or "").strip()[:32])
        # localisable: the handler picks the language and splices the catalog in,
        # because only the request knows the cookie and Accept-Language.
        # The theme rides the same render: applied after paint it is a flash of
        # the wrong colours, which is exactly what someone who needs the
        # contrast cannot use.
        return 200, {"_html": page, "_locale": True,
                     "_theme": connections.get_theme(self.conn, (q or {}).get("_user") or "")}

    def i18n_js(self, q, body):
        """The client half of i18n: T(), data-t application, language switch.

        Cached hard: it changes only when we ship, and it is on the critical
        path of the first paint.
        """
        with open(os.path.join(UI_DIR, "i18n.js"), "r", encoding="utf-8") as f:
            src = f.read().encode("utf-8")
        return 200, {"_raw": src, "_mime": "application/javascript; charset=utf-8",
                     "_cache": "public, max-age=600"}

    def _legal(self, name):
        """A public, static legal page a store listing or payment processor can mine."""
        with open(os.path.join(UI_DIR, name), "r", encoding="utf-8") as f:
            page = f.read()
        page = (page
                .replace("__COMPANY__", os.environ.get("FIRESIDE_COMPANY", "Fireside"))
                .replace("__CONTACT__", os.environ.get("FIRESIDE_CONTACT_EMAIL",
                                                      "support@readfireside.com")))
        # Sole operator details, kept in env so a legal page names a real
        # address without hardcoding one in the source. An unset address is
        # said plainly rather than left as a placeholder, and an unset EU
        # representative drops the sentence instead of naming nobody.
        contact = os.environ.get("FIRESIDE_CONTACT_EMAIL", "support@readfireside.com")
        address = (os.environ.get("FIRESIDE_ADDRESS") or "").strip()
        page = page.replace("__ADDRESS__",
                            address or ("available on request from " + contact))
        rep = (os.environ.get("FIRESIDE_EU_REP") or "").strip()
        if rep:
            page = page.replace("__EU_REP__", rep)
        else:
            page = page.replace(
                " Where the law requires us to name a\n  representative in the EU or the UK, it is: __EU_REP__.",
                "")
        return 200, {"_html": page}

    def pricing(self, q, body):
        """The public price list, served as the app opening on its own price view.

        A payment processor asks for pricing that is visible before an account
        exists, so the page cannot sit behind the sign-in screen the way /plans
        used to: a stranger gets the shell with only the price list in it.
        """
        return self.ui(q, body)

    def checkout(self, q, body):
        """The checkout page, live when a provider is.

        A payment processor's reviewer needs to see this page, and so does a
        buyer who was sent here: the plan, the real price, and one button. The
        page never collects a card number; the button opens the provider's own
        form with the price id the server hands it.

        The prices are rendered from the same catalog the app reads, not from
        numbers typed into the page. The static version still quoted the
        launch ladder ($3/$8/$20 a month) after the ladder changed, which is
        the kind of drift a reviewer reads as a bait and switch.
        """
        with open(os.path.join(UI_DIR, "checkout.html"), "r", encoding="utf-8") as f:
            page = f.read()
        page = (page
                .replace("__COMPANY__", os.environ.get("FIRESIDE_COMPANY", "Fireside"))
                .replace("__CONTACT__", os.environ.get("FIRESIDE_CONTACT_EMAIL",
                                                      "support@readfireside.com")))
        pp = payments.get()
        conf = {}
        fn = getattr(pp, "public_config", None)
        if callable(fn):
            try:
                conf = fn() or {}
            except Exception:
                conf = {}
        cat = plans.catalog(pp)
        tiers = {}
        for t in (cat or {}).get("tiers", []):
            if t.get("id") == quota.FREE_PLAN:
                continue
            tiers[t["id"]] = {"label": t.get("label") or t["id"],
                              "month_cents": t.get("list_amount_cents"),
                              "year_cents": t.get("list_annual_cents"),
                              "included_usd": t.get("included_usd"),
                              "included_bytes": t.get("included_bytes")}
        data = {"configured": bool(conf.get("ok")),
                "provider": conf.get("provider") or "",
                "client_token": conf.get("client_token") or "",
                "env": conf.get("env") or "",
                "prices": conf.get("prices") or {},
                "tiers": tiers}
        blob = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
        return 200, {"_html": page.replace("__CHECKOUT_JSON__", blob)}

    def privacy(self, q, body):
        """The privacy policy. Public, static, and store-listing mineable."""
        return self._legal("privacy.html")

    def about(self, q, body):
        """What this is, who runs it, and what it costs. Public: a store listing
        and a stranger deciding whether to sign up both need it without an
        account, which is the same reason the price list is public."""
        return self._legal("about.html")

    def kokoro(self, q, body):
        """How to run your own voice server, and connect it here.

        Public, because the person deciding whether fireside is worth paying
        for is the person who needs to know they can run it for nothing. The
        commands are the upstream projects' own; the page says so."""
        return self._legal("kokoro.html")

    def contact(self, q, body):
        """The one address, on a page rather than only at the bottom of a
        policy. Public because the people who need it most are the ones who
        cannot get in."""
        return self._legal("contact.html")

    def terms(self, q, body):
        """The terms of service. Public, static, and store-listing mineable."""
        return self._legal("terms.html")

    def refunds(self, q, body):
        """The refund policy on its own URL.

        Payment processors ask for a refund policy URL at onboarding and a
        reviewer should not have to find the paragraph inside a wall of terms.
        The policy is the same text the terms carry; this page is the front
        door to it."""
        return self._legal("refunds.html")

    def delete_account(self, q, body):
        """How to erase your account. Public and static: a store listing has to
        point at a URL, and it must be reachable by someone who already
        uninstalled the app."""
        return self._legal("delete_account.html")

    def favicon(self, q, body):
        """The browser-tab icon: the same mark the install screen shows."""
        with open(os.path.join(ASSETS_DIR, "favicon.ico"), "rb") as f:
            return 200, {"_raw": f.read(), "_mime": "image/x-icon"}


    # ---- PWA ------------------------------------------------------------------
    # Static bits a browser fetches outside any session: the manifest, the
    # service worker (it must live at the root to claim the whole scope), and the
    # install icons. All are public routes, because Chrome fetches them before a
    # session exists.

    def _asset(self, name, mime):
        with open(os.path.join(ASSETS_DIR, name), "rb") as f:
            return 200, {"_raw": f.read(), "_mime": mime}

    def _media(self, name, mime):
        """An optional file: absent is an ordinary state, not a 500."""
        try:
            with open(os.path.join(ASSETS_DIR, name), "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return 404, {"error": "no %s in this build" % name}
        return 200, {"_raw": data, "_mime": mime, "_cache": "public, max-age=86400"}

    def loader(self, q, body):
        """The boot mark: a line-art fire, glasses and book that draw in.

        Served as a file rather than inlined, because its own stylesheet uses
        bare class names and would land in the page's global CSS if it were
        pasted in. In an <img> the document is isolated and its
        prefers-reduced-motion query still applies."""
        return self._media("loader.svg", "image/svg+xml")

    def hearth(self, q, body):
        """The fireplace loop behind the door's welcome pane.

        Licensed media the owner drops in, so "not there yet" is a normal
        state: 404, and the door keeps its own glow rather than showing a
        broken player. Swapping the licensed cut in is replacing this one file.
        """
        return self._media("hearth.mp4", "video/mp4")

    def hearth_poster(self, q, body):
        return self._media("hearth-poster.jpg", "image/jpeg")

    def manifest(self, q, body):
        with open(os.path.join(UI_DIR, "manifest.webmanifest"), "rb") as f:
            return 200, {"_raw": f.read(), "_mime": "application/manifest+json"}

    def service_worker(self, q, body):
        with open(os.path.join(UI_DIR, "sw.js"), "rb") as f:
            return 200, {"_raw": f.read(), "_mime": "text/javascript; charset=utf-8"}

    def icon192(self, q, body):
        return self._asset("icon-192.png", "image/png")

    def icon512(self, q, body):
        return self._asset("icon-512.png", "image/png")

    def icon_maskable(self, q, body):
        return self._asset("icon-maskable-512.png", "image/png")
