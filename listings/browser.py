"""A real browser window (Microsoft Edge, or Chrome on Linux) for reading rental sites.

Funda, Pararius and Huurwoningen answer scripts with a bot-check page but serve normal pages to a normal browser.
This opens one visible browser window with its own profile (~/.cache/rental-finder/edge-profile), reads pages
slowly (one every few seconds) and closes it when the refresh is done. It does not try to solve CAPTCHAs: if a
site shows one, the refresh waits and asks you to complete it in the window.
"""
import os
import platform
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE = Path(os.environ.get("RF_BROWSER_PROFILE", Path.home() / ".cache" / "rental-finder" / "edge-profile"))
CHALLENGE = re.compile(r"Je bent bijna op de pagina|Even geduld|Just a moment|Access Denied", re.I)
PAGE_GAP_S = 3.0          # at least this long between two page loads
AUTO_CLEAR_S = 8          # give a check a moment to clear by itself before asking you
HUMAN_WAIT_S = 600        # then wait this long for you to complete the check in the window


class Blocked(Exception):
    pass


class Browser:
    def __init__(self, log=print):
        self.log = log
        self._pw = self._ctx = self._page = None
        self._last = 0.0

    def __enter__(self):
        PROFILE.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        channel = os.environ.get("RF_BROWSER_CHANNEL") or ("msedge" if platform.system() == "Windows" else "chrome")
        kwargs = dict(headless=False, locale="nl-NL", viewport={"width": 1280, "height": 860},
                      args=["--window-size=1300,900"])
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(str(PROFILE), channel=channel, **kwargs)
        except Exception:  # no Edge/Chrome installed: Playwright's own Chromium
            self._ctx = self._pw.chromium.launch_persistent_context(str(PROFILE), **kwargs)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return self

    def __exit__(self, *exc):
        try:
            self._ctx and self._ctx.close()
        finally:
            self._pw and self._pw.stop()

    def get(self, url: str, ready: str | None = None) -> tuple[int, str]:
        """Load a page and return (status, html) once the real page is there (not a bot check)."""
        wait = PAGE_GAP_S - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        resp = self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        status = resp.status if resp else 0
        t0, asked = time.time(), False
        while True:
            try:
                html, title = self._page.content(), self._page.title()
            except Exception:  # page navigating (challenge redirect)
                time.sleep(1)
                continue
            challenged = bool(CHALLENGE.search(title))
            waited = time.time() - t0
            if not challenged:
                if asked:
                    self.log(f"  Check on {url.split('/')[2]} passed, continuing")
                    asked = False
                if ready is None or ready in html:
                    return (200 if status in (403, 429) else status), html  # check cleared after a 403
                if status in (404, 410):
                    return status, html
                if waited > 20:  # loaded, but not the page we expected
                    return status, html
                time.sleep(1)
                continue
            if waited > AUTO_CLEAR_S and not asked:
                try:
                    self._page.bring_to_front()
                except Exception:
                    pass
                self.log(f"ACTION: {url.split('/')[2]} is asking you to verify you are human. Complete the check in the Edge window; the refresh continues by itself afterwards (waiting up to {HUMAN_WAIT_S // 60} min).")
                asked = True
            if waited > AUTO_CLEAR_S + HUMAN_WAIT_S:
                raise Blocked(f"{url.split('/')[2]} kept showing its bot check")
            time.sleep(1)
