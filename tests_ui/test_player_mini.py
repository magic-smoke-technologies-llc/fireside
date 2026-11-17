"""The player bar can be rolled away, and stays away.

Chris, 2026-11-17: the bottom play bar blocked some buttons, and there was no
way out of it. A fixed 55px bar across the width covers whatever sits under it.
The collapse button is the answer to that: it moves the player into the
bottom-right corner, leaves play, and remembers the choice.

The point of the test is not the class name. It is that a control at the bottom
of the page becomes clickable again while playback is still going.
"""
import unittest

from tests_ui.support import BrowserTest


class PlayerMiniTest(BrowserTest):
    def _playing(self):
        self.page.evaluate("""() => {
            const t = document.getElementById('transport');
            t.classList.add('on');
            document.body.classList.add('hastransport');
            document.getElementById('tnowbook').textContent = 'A Certain Level of Society · ch 2';
            // a control the bar used to sit on top of
            const probe = document.createElement('button');
            probe.id = 'probe';
            probe.textContent = 'Convert';
            probe.style.cssText = 'position:fixed;left:50%;transform:translateX(-50%);'
                + 'bottom:12px;width:120px;height:36px;z-index:1';
            probe.onclick = () => { probe.dataset.clicked = '1'; };
            document.body.appendChild(probe);
        }""")
        self.page.wait_for_timeout(200)

    def test_collapse_frees_the_bottom_and_remembers_it(self):
        self.register("mini_player")
        self.wait_in_app()
        self._playing()

        # before: the bar is over the button, so a tap at that point misses it
        hit = self.page.evaluate("""() => {
            const b = document.getElementById('probe').getBoundingClientRect();
            const el = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
            return el ? (el.id || el.className) : null;
        }""")
        self.assertNotEqual(hit, "probe", "the bar is the thing on top of that button")

        self.page.click("#tmin")
        self.page.wait_for_timeout(200)
        self.assertTrue(self.page.evaluate(
            "() => document.body.classList.contains('transportmini')"))
        # play is still reachable, the wide controls are gone
        self.assertTrue(self.page.is_visible("#tplay"))
        for sel in ("#tprev", "#tnext", "#tqueue", ".vol"):
            self.assertFalse(self.page.is_visible(sel), "%s should be put away" % sel)

        # a real click lands on the button underneath
        self.page.click("#probe")
        self.assertEqual(self.page.get_attribute("#probe", "data-clicked"), "1")

        # and it survives a reload (the reader's own choice, not a per-visit mood)
        self.page.reload()
        self.wait_in_app()
        self._playing()          # nothing is playing after a reload; draw the bar
        self.assertTrue(self.page.evaluate(
            "() => document.body.classList.contains('transportmini')"),
            "the collapsed state should persist")

        # the way back out works and is announced to a screen reader
        self.page.click("#tmin")
        self.page.wait_for_timeout(150)
        self.assertFalse(self.page.evaluate(
            "() => document.body.classList.contains('transportmini')"))
        self.assertEqual(self.page.get_attribute("#tmin", "aria-label"), "Hide player")


if __name__ == "__main__":
    unittest.main()