"""Accessibility is a measurement, not a mood.

The page had nineteen aria-attributes and no way to switch theme, and nothing
here could tell you whether a colour could be read or a state could be told
apart. These tests parse the page that is actually served and assert the things
a reader would notice: contrast of every token in every theme, a skip link that
lands somewhere, a name on every button and landmark, an announcement for what a
sighted reader can see, and status marks that do not rely on hue alone.

Colour is computed from the served CSS, not from a copy of the palette, so a
new theme cannot quietly ship a token nobody checked.

Everything lives on a unittest.TestCase on purpose: CI runs unittest discover,
so a bare `def test_x()` is collected by nothing and reports nothing.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "ui" / "index.html"

# AA for normal text. Body copy is 15px, so it is not "large text" (3.0).
AA_TEXT = 4.5

TOKENS = ["bg", "surface", "raise", "line", "text", "soft", "faint",
          "flame", "on-flame", "ready", "warn", "ring"]

FOREGROUNDS = ["text", "soft", "faint", "flame", "ready", "warn"]
BACKGROUNDS = ["bg", "surface", "raise"]


def _page():
    return PAGE.read_text(encoding="utf-8")


def _lum(hexcolour):
    h = hexcolour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    channels = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _block(name):
    """The declarations of one selector block, e.g. `[data-theme="hc"]{...}`."""
    css = _page()
    if name == ":root":
        m = re.search(r":root\{(.*?)\}", css, re.S)
    else:
        m = re.search(re.escape(name) + r"\{(.*?)\}", css, re.S)
    assert m, "no %s block in the page" % name
    return m.group(1)


def _tokens(block):
    out = {}
    for k, v in re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{3,6})", block):
        out[k] = v
    for k, ref in re.findall(r"--([a-z-]+):\s*var\(--([a-z-]+)\)", block):
        out[k] = ("var", ref)
    return out


def _resolve(tokens, key, depth=0):
    if depth > 5:
        raise AssertionError("token loop at %s" % key)
    v = tokens[key]
    if isinstance(v, tuple):
        return _resolve(tokens, v[1], depth + 1)
    return v


THEME_SELECTORS = [":root", '[data-theme="light"]', '[data-theme="hc"]',
                  '[data-theme="cvd"]']


class TestContrast(unittest.TestCase):

    def test_every_theme_defines_the_whole_palette(self):
        # A theme missing a token falls back to the dark value, which is how a
        # light theme quietly ships black helper text on white.
        root = _tokens(_block(":root"))
        for sel in THEME_SELECTORS[1:]:
            tokens = _tokens(_block(sel))
            missing = [t for t in TOKENS if t not in tokens and t not in root]
            assert not missing, "%s is missing %r" % (sel, missing)

    def test_every_theme_passes_aa_for_text(self):
        root = _tokens(_block(":root"))
        failures = []
        for sel in THEME_SELECTORS:
            own = _tokens(_block(sel))
            tokens = dict(root) if sel == ":root" else {**root, **own}
            for fg in FOREGROUNDS:
                for bg in BACKGROUNDS:
                    ratio = _contrast(_resolve(tokens, fg), _resolve(tokens, bg))
                    if ratio < AA_TEXT:
                        failures.append("%s %s on %s = %.2f" % (sel, fg, bg, ratio))
        assert not failures, "under AA: %r" % failures

    def test_the_bright_on_accent_pair_still_reads(self):
        # A button is --on-flame on --flame; a light theme that keeps the dark
        # theme's near-black on-flame turns every button into a dark blob.
        root = _tokens(_block(":root"))
        for sel in THEME_SELECTORS:
            own = _tokens(_block(sel))
            tokens = dict(root) if sel == ":root" else {**root, **own}
            ratio = _contrast(_resolve(tokens, "on-flame"), _resolve(tokens, "flame"))
            assert ratio >= AA_TEXT, "%s on-flame/flame = %.2f" % (sel, ratio)

    def test_helper_text_is_not_the_hardest_text_to_read(self):
        # The regression this exists for: --faint measured 4.04 on --bg and
        # 3.42 on --raise while carrying the header subtitle and every hint.
        root = _tokens(_block(":root"))
        for sel in THEME_SELECTORS:
            own = _tokens(_block(sel))
            tokens = dict(root) if sel == ":root" else {**root, **own}
            faint_on_raise = _contrast(_resolve(tokens, "faint"), _resolve(tokens, "raise"))
            assert faint_on_raise >= AA_TEXT, "%s faint on raise = %.2f" % (sel, faint_on_raise)


# Deuteranope and protanope simulation, Machado 2009 at severity 1.0, applied to
# linear RGB. Not a claim that a simulation equals a person: it is enough to tell
# whether a palette collapsed the two verdicts into one colour.
DEUTERANOPIA = [[0.367322, 0.860646, -0.227968],
                [0.280085, 0.672501, 0.047413],
                [-0.011820, 0.042940, 0.968881]]
PROTANOPIA = [[0.152286, 1.052583, -0.204860],
              [0.114503, 0.786281, 0.099216],
              [-0.003882, -0.048116, 1.051998]]

# dE76. Ready and warn measured 10.0 / 7.4 in the default palette and 31.4 / 20.7
# in the colour-vision-safe one, so 15 is the line the default fails by design.
CVD_SEPARATION = 15.0


def _linear(hexcolour):
    h = hexcolour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    out = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return out


def _lab(hexcolour, matrix=None):
    r, g, b = _linear(hexcolour)
    if matrix:
        r, g, b = [max(0.0, min(1.0, sum(matrix[i][j] * v
                    for j, v in enumerate((r, g, b))))) for i in range(3)]
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _delta_e(a, b, matrix=None):
    la, lb = _lab(a, matrix), _lab(b, matrix)
    return sum((x - y) ** 2 for x, y in zip(la, lb)) ** 0.5


def _theme_tokens(sel):
    root = _tokens(_block(":root"))
    own = _tokens(_block(sel))
    return dict(root) if sel == ":root" else {**root, **own}


class TestColourBlindness(unittest.TestCase):

    def test_the_colour_vision_theme_keeps_the_verdicts_apart(self):
        # Green against red is one colour to a deuteranope; this theme buys its
        # separation from the blue/purple axis instead, and it has to hold under
        # both simulations, not just the one that is easier.
        tokens = _theme_tokens('[data-theme="cvd"]')
        ready = _resolve(tokens, "ready")
        warn = _resolve(tokens, "warn")
        for name, matrix in [("deuteranopia", DEUTERANOPIA), ("protanopia", PROTANOPIA)]:
            d = _delta_e(ready, warn, matrix)
            assert d >= CVD_SEPARATION, "%s ready/warn dE = %.1f" % (name, d)

    def test_the_accent_is_not_confused_with_a_verdict(self):
        # The theme is worthless if the button orange and the error colour land on
        # top of each other under the same simulation.
        tokens = _theme_tokens('[data-theme="cvd"]')
        flame = _resolve(tokens, "flame")
        for key in ("ready", "warn"):
            other = _resolve(tokens, key)
            d = _delta_e(flame, other, DEUTERANOPIA)
            assert d >= CVD_SEPARATION, "flame/%s dE = %.1f" % (key, d)

    def test_the_default_palette_is_the_one_that_needs_this(self):
        # Guards the reason the theme exists. If the default ever clears the line,
        # a second palette is surface nobody asked to maintain.
        tokens = _theme_tokens(":root")
        d = _delta_e(_resolve(tokens, "ready"), _resolve(tokens, "warn"), PROTANOPIA)
        assert d < CVD_SEPARATION, "default now separates: dE = %.1f" % d

    def test_the_colour_vision_theme_only_moves_colour(self):
        # It is the dark neutrals with different meaning colours. A different
        # background would make it a second design to keep accessible.
        root, cvd = _tokens(_block(":root")), _tokens(_block('[data-theme="cvd"]'))
        for key in ("bg", "surface", "raise", "line", "text", "soft", "faint"):
            assert cvd.get(key, root.get(key)) == root[key], key


class TestStatusIsNotHueOnly(unittest.TestCase):

    def test_the_two_verdicts_are_told_apart_without_colour(self):
        # ready (green) and warn (red) sit at nearly the same lightness: 1.09
        # between them in normal vision, 1.01 under deuteranopia. Every state
        # mark also has to differ in border style, fill or pattern.
        css = _page()
        rules = {}
        for cls, body in re.findall(r"\.spine i\.([a-z]+)\{([^}]*)\}", css):
            # First rule wins: the reduced-motion override repeats a class name
            # with less in it and would otherwise replace the pattern.
            rules.setdefault(cls, body)
        assert "ready" in rules and "fail" in rules, rules.keys()
        signatures = {}
        for cls, body in rules.items():
            border = "dashed" if "dashed" in body else "dotted" if "dotted" in body else "solid"
            fill = ("hatch" if "repeating-linear-gradient" in body else
                    "half" if "linear-gradient" in body else
                    "solid" if "background:" in body else "none")
            signatures[cls] = (border, fill)
        assert signatures["ready"] != signatures["fail"], signatures
        states = [c for c in signatures if c != "cur"]
        for i, a in enumerate(states):
            for b in states[i + 1:]:
                assert signatures[a] != signatures[b], (a, b, signatures)

    def test_a_chapter_spine_says_what_it_means(self):
        # role=img with a count, or a screen reader gets a row of empty <i>s.
        js = _page()
        assert "d.setAttribute('role', 'img')" in js
        assert "of ' + states.length + ' chapters" in js


class TestNamesAndLandmarks(unittest.TestCase):

    def test_a_skip_link_lands_on_the_content(self):
        html = _page()
        m = re.search(r'<a class="skip" href="#([\w-]+)"', html)
        assert m, "no skip link"
        target = m.group(1)
        assert re.search(r'<[a-z]+ id="%s"' % target, html), target
        # It must be the first thing Tab reaches, so it sits first in the body.
        body = html.split("<body", 1)[1]
        first_tag = re.search(r"<(a|button|input|select)\b", body)
        assert first_tag and first_tag.group(1) == "a", "skip link is not first"

    def test_every_landmark_has_a_name(self):
        # Two unnamed <nav>s are one ambiguous landmark to a screen reader.
        html = _page()
        navs = re.findall(r"<nav\b([^>]*)>", html)
        assert navs
        for attrs in navs:
            assert "aria-label" in attrs or "aria-labelledby" in attrs, attrs

    def test_every_button_has_an_accessible_name(self):
        html = _page()
        nameless = []
        for attrs, inner in re.findall(r"<button\b([^>]*)>(.*?)</button>", html, re.S):
            text = re.sub(r"<[^>]+>", "", inner).strip()
            if text:
                continue
            if "aria-label" in attrs or "aria-labelledby" in attrs:
                continue
            nameless.append(attrs[:70])
        assert not nameless, "no name: %r" % nameless

    def test_the_current_tab_is_announced(self):
        # `.on` is a class a screen reader cannot see.
        html = _page()
        assert re.search(r'<button data-v="lib" class="on" aria-current="page"', html)
        assert "setAttribute('aria-current', 'page')" in html


class TestAnnouncements(unittest.TestCase):

    def test_failures_are_announced_and_confirmations_are_not_loud(self):
        html = _page()
        assert re.search(r'<div id="toasts" role="status" aria-live="polite"', html)
        assert "if (kind === 'bad') t.setAttribute('role', 'alert')" in html

    def test_the_player_says_its_state(self):
        html = _page()
        assert 'id="tstatus"' in html
        assert 'role="status"' in html.split('id="tstatus"')[1][:80]
        assert "sayState()" in html

    def test_the_clock_does_not_talk_every_second(self):
        # A live region updated on timeupdate would read the whole book's
        # timeline out loud, four times a second.
        html = _page()
        update = html.split("addEventListener('timeupdate'", 1)[1].split("});", 1)[0]
        assert "sayState" not in update


class TestThemeBeforePaint(unittest.TestCase):

    def test_the_chosen_theme_is_applied_before_the_body_renders(self):
        # A theme applied after paint is a flash of the theme the reader could
        # not use. The head script has to come before <body>.
        html = _page()
        head = html.split("</head>", 1)[0]
        assert "localStorage.getItem('fs.theme')" in head
        assert "setAttribute('data-theme'" in head

    def test_the_picker_offers_the_themes_that_exist(self):
        html = _page()
        picker = html.split('id="themesel"', 1)[1].split("</select>", 1)[0]
        offered = set(re.findall(r'<option value="([a-z]*)"', picker))
        assert "" in offered, "no follow-the-device option"
        # Choosing dark has to beat a light system setting, so dark is a real
        # choice even though its tokens are the :root defaults.
        for name in ["light", "dark", "hc", "cvd"]:
            assert name in offered, (name, offered)
        for name in [n for n in offered if n]:
            assert name == "dark" or ('[data-theme="%s"]' % name) in html, name


if __name__ == "__main__":
    unittest.main()
