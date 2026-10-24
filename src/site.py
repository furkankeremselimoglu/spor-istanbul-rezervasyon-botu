"""High-level site interactions for online.spor.istanbul (ASP.NET WebForms postbacks).

Every navigation-causing action acquires the RateLimiter first and wraps the
triggering click/select in page.expect_navigation(wait_until="domcontentloaded").
After each navigation, _check_session() raises LoggedOut on login-page signs.

`Site` tracks the (branch, facility) it last applied as `self.current`; `switch_to`
and `refresh` verify against that rather than against cfg, since a single Site/page
now serves multiple (branch, facility) targets over its lifetime (GUI multi-target
mode). The branch/facility parameters on ensure_ready / ensure_ready_or_login /
scan_facilities are optional ONLY for backwards compatibility with callers
(runner.py/bot.py) that haven't been updated to pass an explicit target yet; when
omitted they fall back to cfg.site.branch_text / cfg.site.facility_text (which may
themselves be None once config.py's site section becomes optional).
"""
from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Iterator, Literal

from bs4 import BeautifulSoup, Tag
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from src.browser import (
    Artifacts,
    AttemptLimiter,
    BrowserClosed,
    LoggedOut,
    RateLimiter,
    SiteError,
    SiteUnavailable,
    Stopped,
    save_cookies_now,
)
from src.grid import Grid, SessionCard, Status, find_card, is_selection_page, parse_grid

if TYPE_CHECKING:
    from src.reservations import Reservation
    from src.config import Config

_RESULT_MESSAGE_SELECTOR = (
    ".alert, .modal.show, .swal2-popup, [id*=lblMesaj], [id*=lblHata], [id*=lblSonuc]"
)
_SMS_CODE_SELECTOR = "#pageContent_txtDogrulamaKodu"
_SMS_SUBMIT_SELECTOR = "#btnCepTelDogrulamaGonder"


@dataclass(frozen=True)
class SaveOutcome:
    """Outcome of Site.click_save / submit_sms / check_booked.

    kind:
        "booked" -- the card re-parsed as Status.BOOKED, no SMS step pending.
        "sms"    -- an SMS verification field is now visible; the caller must obtain a
                    code (from the human) and call submit_sms, WITHOUT clicking Kaydet again.
        "failed" -- neither of the above; the site rejected the attempt or the state is
                    unclear. The caller must not retry Kaydet on the same card automatically.
    """

    kind: Literal["sms", "booked", "failed"]
    message: str
    card: SessionCard | None
    dialogs: list[str]

    @staticmethod
    def failed(message: str, card: SessionCard | None = None) -> "SaveOutcome":
        return SaveOutcome(kind="failed", message=message, card=card, dialogs=[])


@dataclass
class ScanItem:
    """One step yielded by Site.iter_scan(), for GUI progress reporting.

    kind "branches": `names` is the full branch list, branch/facility/grid are None.
    kind "facilities": `names` is the facility list for `branch`; grid is None.
    kind "grid": one facility's grid; `names` is [facility] for uniformity.
    `done`/`total` count completed/expected steps for the whole iter_scan() call.
    """

    kind: Literal["branches", "facilities", "grid"]
    branch: str | None
    facility: str | None
    names: list[str]
    grid: Grid | None
    done: int
    total: int


def is_success_message(text: str, card: SessionCard) -> bool:
    """True for the site's post-SMS confirmation for this card, observed as (on Seanslarım):
    "25.09.2026 - Cuma 16:00:00 - 19:00:00 seansı hesabınıza başarıyla tanımlandı."
    """
    return (
        "başarıyla tanımlandı" in text
        and card.date.strftime("%d.%m.%Y") in text
        and f"{card.start}:00" in text
    )


# -- pure helpers (no Playwright; testable against fixture HTML) -----------------

def _decide_save_outcome(
    *,
    sms_visible: bool,
    success_texts: list[str],
    updated_card: SessionCard | None,
    card: SessionCard,
    dialogs: list[str],
    messages: list[str],
) -> SaveOutcome:
    """Pure decision core shared by Site._classify_save_page (live inputs, computed via
    Playwright locators) and classify_save_page_html (the same inputs computed from a
    saved HTML string -- see below). No Playwright, no I/O.

    Priority: SMS field visible (kind="sms") wins over a stale BOOKED read from before
    the postback; then a matching .alert-success message; then a BOOKED card re-parsed
    from the grid; else "failed".
    """
    parts = [m for m in (*dialogs, *messages) if m]

    if sms_visible:
        message = " | ".join(parts) if parts else "SMS doğrulama kodu gönderildi."
        return SaveOutcome(kind="sms", message=message, card=card, dialogs=list(dialogs))

    if any(is_success_message(t, card) for t in success_texts):
        return SaveOutcome(
            kind="booked", message=" | ".join(success_texts), card=card, dialogs=list(dialogs)
        )

    if updated_card is not None and updated_card.status == Status.BOOKED:
        message = " | ".join(parts) if parts else "Rezervasyon tamamlandı."
        return SaveOutcome(kind="booked", message=message, card=updated_card, dialogs=list(dialogs))

    message = " | ".join(parts) if parts else "Sonuç belirlenemedi."
    return SaveOutcome(kind="failed", message=message, card=updated_card or card, dialogs=list(dialogs))


_DISPLAY_NONE_RE = re.compile(r"display\s*:\s*none", re.I)
_VISIBILITY_HIDDEN_RE = re.compile(r"visibility\s*:\s*hidden", re.I)


def _sms_field_visible_in_html(html: str) -> bool:
    """Best-effort, Playwright-free proxy for Site._sms_field_visible(): the SMS code
    input (#pageContent_txtDogrulamaKodu) exists in the DOM and isn't inside an inline
    `display:none`/`visibility:hidden`/`hidden`-attribute ancestor.

    This can't fully replicate Playwright's real (CSS-computed) visibility check --
    that's exercised separately, against the same real fixture HTML, in a headless
    browser by tools/ci_smoke.py --browser.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    el = soup.find(id="pageContent_txtDogrulamaKodu")
    if el is None:
        return False
    node = el
    while node is not None and isinstance(node, Tag):
        style = node.get("style") or ""
        if _DISPLAY_NONE_RE.search(style) or _VISIBILITY_HIDDEN_RE.search(style):
            return False
        if node.has_attr("hidden"):
            return False
        node = node.parent
    return True


def _visible_success_texts_in_html(html: str) -> list[str]:
    """Best-effort, Playwright-free proxy for Site._visible_texts(".alert-success")."""
    soup = BeautifulSoup(html or "", "html.parser")
    texts: list[str] = []
    for el in soup.select(".alert-success"):
        style = el.get("style") or ""
        if _DISPLAY_NONE_RE.search(style):
            continue
        text = el.get_text(" ", strip=True)
        if text:
            texts.append(text)
    return texts


def _clean_option_texts(texts: Iterable[str]) -> list[str]:
    """Strip, drop blanks and the ASP.NET placeholder ("-- Seçiniz --" etc)."""
    return [t.strip() for t in texts if t.strip() and "Seçiniz" not in t]


def select_option_texts(html: str, select_id: str) -> list[str]:
    """Pure: cleaned <option> texts of `#select_id` in a page's HTML. [] if not found."""
    soup = BeautifulSoup(html, "html.parser")
    select = soup.find(id=select_id)
    if select is None:
        return []
    return _clean_option_texts(" ".join(opt.get_text().split()) for opt in select.find_all("option"))


def _product_column_index(soup: BeautifulSoup) -> int | None:
    """Index of the "Ürün (Türü)" column in the Seanslarım membership table's <thead>."""
    thead = soup.find("thead")
    if thead is None:
        return None
    for i, th in enumerate(thead.find_all("th")):
        if "Ürün" in th.get_text(" ", strip=True):
            return i
    return None


def _row_label(tr, product_idx: int | None) -> str:
    """Short label for a membership row: its "Ürün" cell if that column is identifiable
    and non-empty, else the whole row's text, whitespace-normalized."""
    if product_idx is not None:
        tds = tr.find_all("td", recursive=False)
        if product_idx < len(tds):
            text = tds[product_idx].get_text(" ", strip=True)
            if text:
                return text
    return " ".join(tr.get_text(" ", strip=True).split())


def membership_row_labels(html: str) -> list[str]:
    """Pure: labels of every Seanslarım row that carries a Seans Seç link, in page order.

    Used by Site.list_memberships() for the GUI's membership picker.
    """
    soup = BeautifulSoup(html, "html.parser")
    idx = _product_column_index(soup)
    labels: list[str] = []
    for tr in soup.find_all("tr"):
        if tr.find("a", id=re.compile(r"lbtnSeansSecim")) is None:
            continue
        labels.append(_row_label(tr, idx))
    return labels


def member_name_from_html(html: str) -> str | None:
    """Pure: the logged-in member's full name from the site header (`span#lblAdSoyad`,
    inside `li#liUye`), whitespace-normalized. None if missing/empty.

    See Site.member_name()/Site.last_member_name for the page-reading counterpart.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    el = soup.find(id="lblAdSoyad")
    if el is None:
        return None
    name = " ".join(el.get_text(" ", strip=True).split())
    return name or None


def detect_unavailable_reason(title: str, html: str) -> str | None:
    """Pure: Turkish reason string if `title`/`html` look like a Cloudflare challenge or a
    maintenance page, else None. Checked after every navigation (see Site._check_session)
    so the engine can raise a distinct SiteUnavailable instead of misreading the page as a
    login/application error.
    """
    if "Just a moment" in (title or ""):
        return "Cloudflare meydan okuması görünüyor (başlık: \"Just a moment\")"
    soup = BeautifulSoup(html or "", "html.parser")
    if soup.find(id="challenge-form") is not None:
        return "Cloudflare meydan okuması görünüyor (#challenge-form)"
    if "bakım" in (html or "").lower():
        return "Site bakımda görünüyor (\"bakım\" metni bulundu)"
    return None


def _is_target_closed(exc: BaseException) -> bool:
    """Playwright's "Target page, context or browser has been closed" (TargetClosedError)."""
    return isinstance(exc, PlaywrightError) and "has been closed" in str(exc)


def _needs_page(method):
    """Make an entry point that navigates the page survive a closed bot-browser tab.

    Before the call: replace a tab that is already known to be closed. During the call:
    the sync Playwright API only learns that a tab was closed while it is busy, so a tab
    the user closed while the engine sat idle still looks open and the first navigation
    fails with TargetClosedError -- then open a fresh tab and run the method once more.
    A browser that is gone altogether raises BrowserClosed (the owner relaunches it).
    Generator methods only get the "before" check (they can't be safely re-run).
    """
    import functools
    import inspect

    if inspect.isgeneratorfunction(method):
        @functools.wraps(method)
        def gen_wrapper(self, *args, **kwargs):
            self.ensure_page()
            return method(self, *args, **kwargs)

        return gen_wrapper

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        self.ensure_page()
        try:
            return method(self, *args, **kwargs)
        except PlaywrightError as e:
            if not _is_target_closed(e):
                raise
            self.log.warning("Bot tarayıcısının sekmesi kapanmış (%s); yeni sekmeyle tekrar deneniyor",
                             method.__name__)
            self._replace_page()
            return method(self, *args, **kwargs)

    return wrapper


def _credentials_fingerprint(tc: str, password: str) -> str:
    """Short one-way fingerprint so we can tell whether the saved credentials changed."""
    import hashlib

    return hashlib.sha256(f"{tc}\0{password}".encode("utf-8")).hexdigest()[:16]


class Site:
    """Wraps one authenticated Playwright page with the site's reservation flow."""

    # Guard against account lockout: at most this many auto-login attempts in any
    # rolling window, shared across every Site instance in the process (class-level).
    _AUTO_LOGIN_MAX_ATTEMPTS = 3
    _AUTO_LOGIN_WINDOW_S = 15 * 60
    _auto_login_limiter = AttemptLimiter(_AUTO_LOGIN_MAX_ATTEMPTS, _AUTO_LOGIN_WINDOW_S)
    # Consecutive refusals of the saved credentials (wrong/changed password). After this
    # many, auto-login stops for those exact credentials until new ones are saved.
    CREDENTIAL_REJECTIONS_LIMIT = 2
    _credential_rejections = 0
    credentials_rejected_fp: "str | None" = None
    last_rejection_message = ""

    def __init__(
        self,
        page: Page,
        cfg: "Config",
        artifacts: Artifacts,
        limiter: RateLimiter,
        membership_text: str | None = None,
        window: object | None = None,
    ) -> None:
        self.page = page
        self.cfg = cfg
        self.artifacts = artifacts
        self.limiter = limiter
        self.window = window
        self.membership_text = (
            membership_text
            if membership_text is not None
            else getattr(getattr(cfg, "site", None), "membership_text", None)
        )
        # (branch, facility) last successfully applied via ensure_ready/switch_to.
        self.current: tuple[str, str] | None = None
        self.log = logging.getLogger("ibbspor.site")
        # Set after a successful check_logged_in()/auto_login()/wait_for_login() (see
        # member_name()); cleared on logout(). Never logged -- see src.worker.AccountInfo.
        self.last_member_name: str | None = None

    # -- target resolution (backwards compat for callers not yet passing branch/facility)

    def _resolve_target(self, branch: str | None, facility: str | None) -> tuple[str, str]:
        if branch is None:
            branch = getattr(self.cfg.site, "branch_text", None)
        if facility is None:
            facility = getattr(self.cfg.site, "facility_text", None)
        if not branch or not facility:
            raise SiteError("hedef branş/tesis belirtilmedi")
        return branch, facility

    def _resolve_branch(self, branch: str | None) -> str:
        if branch is None:
            branch = getattr(self.cfg.site, "branch_text", None)
        if not branch:
            raise SiteError("hedef branş/tesis belirtilmedi")
        return branch

    # -- stop-aware sleeping -------------------------------------------------

    def _sleep(self, seconds: float, stop: "threading.Event | None" = None) -> None:
        """time.sleep(seconds), or stop.wait(seconds) (returns promptly once stop is set)."""
        if stop is not None:
            stop.wait(seconds)
        else:
            time.sleep(seconds)

    @staticmethod
    def _stopped(stop: "threading.Event | None") -> bool:
        return stop is not None and stop.is_set()

    # -- window hooks (browser.py's BrowserWindow; optional) -----------------

    def _show_window(self) -> None:
        if self.window is not None:
            try:
                self.window.show()
            except Exception:
                self.log.exception("window.show() failed")

    def _minimize_window(self) -> None:
        if self.window is not None:
            try:
                self.window.minimize()
            except Exception:
                self.log.exception("window.minimize() failed")

    # -- page lifecycle ----------------------------------------------------

    def ensure_page(self) -> None:
        """Make sure `self.page` is usable before navigating.

        If the user closed the bot browser's tab/window (on macOS the browser keeps
        running without windows), open a fresh tab in the same browser and carry on.
        If the whole browser is gone, raise BrowserClosed so the owner relaunches it.
        """
        page = self.page
        try:
            closed = page.is_closed()
        except Exception:
            closed = True
        if not closed:
            return
        self.log.warning("Bot tarayıcısının sekmesi kapanmış; yeni sekme açılıyor")
        self._replace_page()

    def _replace_page(self) -> None:
        """Open a fresh tab in the same browser and point everything at it."""
        old_page = self.page
        try:
            new_page = old_page.context.new_page()
        except Exception as e:
            raise BrowserClosed("Bot tarayıcısı kapanmış") from e
        self.page = new_page
        self.current = None
        if self.window is not None:
            try:
                self.window.page = new_page
            except Exception:
                self.log.debug("window.page could not be updated", exc_info=True)
        self._minimize_window()

    # -- session / login -------------------------------------------------

    def member_name(self) -> str | None:
        """member_name_from_html() of the currently loaded page (no navigation)."""
        try:
            html = self.page.content()
        except Exception:
            return None
        return member_name_from_html(html)

    @_needs_page
    def check_logged_in(self) -> bool:
        """Navigate to /uyespor and report whether the session looks authenticated."""
        t0 = time.monotonic()
        self.limiter.acquire()
        self.page.goto(f"{self.cfg.site.base_url}/uyespor", wait_until="domcontentloaded")
        # We just left the selection grid: the next switch_to must reopen it instead of
        # refresh() waiting 30 s for #ddlTesisFiltre on /uyespor (seen live at a hot start).
        self.current = None
        self.artifacts.save(self.page, "check_logged_in")

        logged_in = self.page.locator("a[id*=lbtnSeansSecim]").count() > 0
        if logged_in:
            self.last_member_name = self.member_name()
        self.log.info(
            "check_logged_in -> %s (%.0fms)", logged_in, (time.monotonic() - t0) * 1000
        )
        return logged_in

    def _login_signals(self) -> bool:
        """Does the current page look like a login page?

        Pages we expect when logged in (the seans seçim grid, or Seanslarım with its
        Seans Seç links) short-circuit to False. Deliberately no text heuristics: the
        whole ASP.NET page is one <form>, so words like "Giriş" appear on normal pages.
        """
        if self.page.locator("#ddlBransFiltre, a[id*=lbtnSeansSecim]").count() > 0:
            return False
        cur_url = self.page.url.lower()
        if any(token in cur_url for token in ("login", "giris", "uyegiris")):
            return True
        pw = self.page.locator("input[type=password]")
        return any(pw.nth(i).is_visible() for i in range(pw.count()))

    def _check_session(self) -> None:
        """Call after every navigation; raises SiteUnavailable (Cloudflare/maintenance)
        or LoggedOut on login-page signs."""
        try:
            title = self.page.title()
        except Exception:
            title = ""
        try:
            html = self.page.content()
        except Exception:
            html = ""
        reason = detect_unavailable_reason(title, html)
        if reason:
            raise SiteUnavailable(reason)
        if self._login_signals():
            raise LoggedOut("oturum kapalı görünüyor (giriş sayfası işaretleri bulundu)")

    # -- navigation flow ---------------------------------------------------

    @_needs_page
    def open_membership(self, membership_text: str | None = None) -> None:
        """Go to /uyespor, find a membership row, click its Seans Seç link.

        `membership_text` (else self.membership_text): if truthy, the row containing that
        text; if falsy, the FIRST row that has a Seans Seç link.
        """
        t0 = time.monotonic()
        text = membership_text if membership_text is not None else self.membership_text

        self.limiter.acquire()
        self.page.goto(f"{self.cfg.site.base_url}/uyespor", wait_until="domcontentloaded")
        self.artifacts.save(self.page, "open_membership")

        has_seans_link = self.page.locator("a[id*=lbtnSeansSecim]").count() > 0
        if not has_seans_link and self._login_signals():
            raise LoggedOut("giriş yapılmamış (üyelik sayfasında seans seç bağlantısı yok)")

        if text:
            rows = self.page.locator("tr").filter(has_text=text)
            if rows.count() == 0:
                self.artifacts.save(self.page, "error_membership_not_found", force=True)
                raise SiteError(f"membership row not found: {text!r}")
            link = rows.first.locator("a[id*=lbtnSeansSecim]")
            if link.count() == 0:
                self.artifacts.save(self.page, "error_membership_no_link", force=True)
                raise SiteError("membership row found but has no seans seçim link")
        else:
            link = self.page.locator("a[id*=lbtnSeansSecim]")
            if link.count() == 0:
                self.artifacts.save(self.page, "error_membership_no_link", force=True)
                raise SiteError("üyelik listesinde seans seçim bağlantısı yok")

        self.limiter.acquire()
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            link.first.click()
        self._check_session()

        if not is_selection_page(self.page.content()):
            self.artifacts.save(self.page, "error_not_selection_page", force=True)
            raise SiteError("navigated after Seans Seç but page is not the seans seçim page")

        self.log.info("open_membership done in %.0fms", (time.monotonic() - t0) * 1000)

    @_needs_page
    def list_memberships(self) -> list[str]:
        """Go to /uyespor; return the labels of every row with a Seans Seç link, for the GUI."""
        self.limiter.acquire()
        self.page.goto(f"{self.cfg.site.base_url}/uyespor", wait_until="domcontentloaded")
        self.artifacts.save(self.page, "list_memberships")
        return membership_row_labels(self.page.content())

    def _ensure_on_selection_page(self) -> None:
        try:
            html = self.page.content()
        except Exception:
            html = ""
        if not is_selection_page(html):
            self.open_membership()

    @_needs_page
    def list_branches(self) -> list[str]:
        """Branch names offered on the seans seçim page (skips empty/"Seçiniz")."""
        self._ensure_on_selection_page()
        names = select_option_texts(self.page.content(), "ddlBransFiltre")
        self.artifacts.save(self.page, "list_branches")
        return names

    @_needs_page
    def list_facilities(self, branch: str) -> list[str]:
        """Facility names offered for `branch` (does one branch postback)."""
        self._ensure_on_selection_page()
        self._select_branch(branch)
        names = select_option_texts(self.page.content(), "ddlTesisFiltre")
        # Only the branch was selected (facility dropdown was just repopulated), so
        # (branch, facility) no longer reflects reality; force a full switch_to next time.
        self.current = None
        self.artifacts.save(self.page, "list_facilities")
        return names

    def _select_branch(self, branch: str) -> Grid:
        self.limiter.acquire()
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            self.page.locator("#ddlBransFiltre").select_option(label=branch)
        self._check_session()
        grid = parse_grid(self.page.content())
        if grid.branch_selected != branch:
            self.artifacts.save(self.page, "error_branch_mismatch", force=True)
            raise SiteError(
                f"branch selection mismatch: expected {branch!r}, got {grid.branch_selected!r}"
            )
        return grid

    def _select_facility(self, facility: str) -> Grid:
        self.limiter.acquire()
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            self.page.locator("#ddlTesisFiltre").select_option(label=facility)
        self._check_session()
        grid = parse_grid(self.page.content())
        if grid.facility_selected != facility:
            self.artifacts.save(self.page, "error_facility_mismatch", force=True)
            raise SiteError(
                f"facility selection mismatch: expected {facility!r}, got {grid.facility_selected!r}"
            )
        return grid

    def apply_filters(self, branch: str, facility: str) -> Grid:
        """Select branch, wait for its postback, then select facility. Verifies both."""
        t0 = time.monotonic()
        self._select_branch(branch)
        # Facility options are repopulated by the branch postback; only select facility
        # after that navigation has fully finished (see _select_branch).
        grid = self._select_facility(facility)
        self.artifacts.save(self.page, "filters")
        self.log.info("apply_filters done in %.0fms", (time.monotonic() - t0) * 1000)
        return grid

    @_needs_page
    def ensure_ready(self, branch: str | None = None, facility: str | None = None) -> Grid:
        """open_membership + apply_filters(branch, facility); saves artifacts (forced) on
        failure. branch/facility default to cfg.site.branch_text/facility_text when omitted
        (backwards compat for callers not yet passing a target explicitly)."""
        branch, facility = self._resolve_target(branch, facility)
        try:
            self.open_membership()
            grid = self.apply_filters(branch, facility)
            self.current = (branch, facility)
            self._log_grid_range(grid)
            return grid
        except Exception as exc:
            self.artifacts.save(self.page, f"error_{type(exc).__name__}", force=True)
            raise

    def _log_grid_range(self, grid: Grid) -> None:
        """INFO-log the grid's visible date range on every full load (see
        analysis/open-rule-2026-10/README.md: whether next week's sessions become visible
        before Monday 00:00 is unknown -- this gives us the data to find out)."""
        if not grid.dates:
            return
        lo, hi = min(grid.dates), max(grid.dates)
        self.log.info("tablo: %s–%s", lo.strftime("%d.%m"), hi.strftime("%d.%m"))

    @_needs_page
    def wait_for_login(
        self, timeout_s: float = 600, stop: "threading.Event | None" = None
    ) -> bool:
        """Let the user log in by hand in this window; True once /uyespor shows Seans Seç links.

        Polls without navigating while a login form is on screen (so typing is never
        disturbed); when the page stops looking like a login page, verifies via /uyespor.
        Polls in <=1s steps via self._sleep so a given `stop` event is honoured promptly.
        Shows `self.window` (if given) at the start -- a manual login needs a visible
        window -- and minimizes it again once login is detected.
        """
        from src.notify import notify

        self._show_window()

        if not self._login_signals():
            self.limiter.acquire()
            self.page.goto(f"{self.cfg.site.base_url}/uyespor", wait_until="domcontentloaded")
        notify("Giriş gerekli", "Bot penceresinde spor.istanbul hesabınıza giriş yapın")
        # log, not print: under pythonw (Windows GUI) sys.stdout is None
        self.log.info("elle giriş bekleniyor (en fazla %d dk)", timeout_s // 60)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._stopped(stop):
                return False
            try:
                if self.page.locator("a[id*=lbtnSeansSecim]").count() > 0:
                    self.last_member_name = self.member_name()
                    self._minimize_window()
                    return True
                if not self._login_signals() and self.check_logged_in():
                    self._minimize_window()
                    return True
            except Exception as e:
                if not _is_target_closed(e):
                    self.log.debug("login poll: page busy", exc_info=True)
                else:
                    # The user closed the login window: reopen the login page in a new tab
                    # (BrowserClosed propagates if the whole browser is gone).
                    self.log.warning("giriş penceresi kapatılmış; yeniden açılıyor")
                    self._replace_page()
                    self._show_window()
                    try:
                        self.limiter.acquire()
                        self.page.goto(f"{self.cfg.site.base_url}/uyespor", wait_until="domcontentloaded")
                    except Exception:
                        self.log.debug("login page reopen failed", exc_info=True)
            self._sleep(1.0, stop)
        self.log.info("elle giriş algılanamadı (zaman aşımı veya durduruldu)")
        return False

    @_needs_page
    def ensure_ready_or_login(
        self,
        branch: str | None = None,
        facility: str | None = None,
        timeout_s: float = 600,
        stop: "threading.Event | None" = None,
    ) -> Grid:
        """ensure_ready(branch, facility); if logged out, try auto_login() first, else wait
        for manual login."""
        branch, facility = self._resolve_target(branch, facility)
        try:
            return self.ensure_ready(branch, facility)
        except LoggedOut:
            self.log.warning("logged out; trying auto_login before manual wait")
            if self.auto_login():
                self.log.info("auto_login succeeded, continuing")
                return self.ensure_ready(branch, facility)
            self.log.warning("auto_login unavailable/failed; waiting up to %ds for manual login", timeout_s)
            if not self.wait_for_login(timeout_s, stop=stop):
                raise
            self.log.info("login detected, continuing")
            return self.ensure_ready(branch, facility)

    @_needs_page
    def login_if_needed(
        self, timeout_s: float = 600, stop: "threading.Event | None" = None
    ) -> bool:
        """check_logged_in() -> auto_login() -> wait_for_login(). True once a session is live.

        The single helper every read-only command (scan/reservations/cancel) should call
        instead of the old `site.check_logged_in() or site.wait_for_login()` pattern, so
        they also get a chance at auto-login before falling back to a manual wait.
        """
        if self.check_logged_in():
            return True
        if self.auto_login():
            return True
        return self.wait_for_login(timeout_s, stop=stop)

    @_needs_page
    def auto_login(self) -> bool:
        """Try to log in using credentials saved in the OS keychain (src/credentials.py).

        Returns False immediately, without touching the page, if no credentials are saved
        or the rolling auto-login attempt limit has been reached (account-lockout guard).
        Never types credentials anywhere except #txtTCPasaport / #txtSifre. Saves an
        "auto_login_ok" artifact on success, a forced "error_auto_login" artifact on failure.

        Only a FAILED attempt counts toward the rolling lockout limiter -- a successful
        login carries no lockout risk (see src.browser.AttemptLimiter.blocked()/record()).
        On success, cookies are saved immediately (src.browser.save_cookies_now) so the
        session survives a crash/kill right afterward instead of only a clean shutdown.
        """
        from src.credentials import load as load_credentials
        from src.credentials import mask_tc

        creds = load_credentials()
        if creds is None:
            return False
        tc, password = creds
        fp = _credentials_fingerprint(tc, password)
        if fp == Site.credentials_rejected_fp:
            # The site refused exactly these credentials repeatedly; retrying them forever
            # could lock the account. Wait until the user saves new ones (fingerprint changes).
            self.log.warning("auto_login: kayıtlı bilgiler site tarafından reddedildi; yeni bilgi bekleniyor")
            return False

        if Site._auto_login_limiter.blocked():
            self.log.warning(
                "auto_login: deneme sınırına ulaşıldı (%d/%ds); hesap kilidini önlemek için atlanıyor",
                Site._AUTO_LOGIN_MAX_ATTEMPTS,
                Site._AUTO_LOGIN_WINDOW_S,
            )
            return False

        t0 = time.monotonic()
        self.log.info("auto_login: deneniyor (TC: %s)", mask_tc(tc))
        message = ""
        try:
            self.limiter.acquire()
            self.page.goto(f"{self.cfg.site.base_url}/uyegiris", wait_until="domcontentloaded")

            self.page.locator("#txtTCPasaport").fill(tc)
            self.page.locator("#txtSifre").fill(password)

            self.limiter.acquire()
            try:
                with self.page.expect_navigation(wait_until="domcontentloaded", timeout=15_000):
                    self.page.locator("#btnGirisYap").click()
            except PlaywrightTimeoutError:
                self.log.warning("auto_login: Giriş Yap sonrası 15s içinde yönlendirme olmadı")

            # Still on the login page (or the password field is still visible) -> treat as a
            # failed attempt; grab any visible error text before check_logged_in() navigates
            # away to do the reliable verification.
            if self._login_signals():
                message = " | ".join(self._visible_texts(".alert, [id*=lbl]"))

            ok = self.check_logged_in()
        except Exception:
            self.log.exception("auto_login: beklenmeyen hata")
            self.artifacts.save(self.page, "error_auto_login", force=True)
            Site._auto_login_limiter.record()
            return False

        if ok:
            # ok came from check_logged_in() above, which already set last_member_name.
            Site._credential_rejections = 0
            Site.credentials_rejected_fp = None
            self.artifacts.save(self.page, "auto_login_ok")
            self.log.info("auto_login: başarılı (%.0fms)", (time.monotonic() - t0) * 1000)
            with contextlib.suppress(Exception):
                save_cookies_now(self.page.context)
        else:
            self.artifacts.save(self.page, "error_auto_login", force=True)
            self.log.warning("auto_login: başarısız%s", f" ({message})" if message else "")
            Site._auto_login_limiter.record()
            if message or self._login_signals():
                # Still on the login form: the site refused the credentials (a network or
                # page error raises above instead and is not counted here).
                Site._credential_rejections += 1
                Site.last_rejection_message = message
                if Site._credential_rejections >= Site.CREDENTIAL_REJECTIONS_LIMIT:
                    Site.credentials_rejected_fp = fp
                    self.log.error("auto_login: bilgiler %d kez reddedildi; otomatik giriş durduruldu",
                                   Site._credential_rejections)
        return ok

    @_needs_page
    def logout(self) -> None:
        """Click #lbtnCikisYap if present (tolerates absence/timeouts); clears self.current."""
        try:
            link = self.page.locator("#lbtnCikisYap")
            if link.count() > 0:
                # The link sits in a hidden header dropdown: a real click waits 30 s for it
                # to become visible and then gives up (seen live) -- so run its postback.
                self.limiter.acquire()
                try:
                    with self.page.expect_navigation(wait_until="domcontentloaded", timeout=15_000):
                        self.page.evaluate("__doPostBack('ctl00$lbtnCikisYap','')")
                    self.log.info("logout: çıkış yapıldı")
                except PlaywrightTimeoutError:
                    self.log.warning("logout: çıkış sonrası yönlendirme olmadı")
            else:
                self.log.info("logout: çıkış bağlantısı bulunamadı")
        except Exception:
            self.log.exception("logout: beklenmeyen hata")
        self.current = None
        self.last_member_name = None

    def grid(self) -> Grid:
        """Parse the currently loaded page without navigating."""
        return parse_grid(self.page.content())

    @_needs_page
    def refresh(self) -> Grid:
        """Re-fetch the grid from the server: deselect + re-select the facility (2 postbacks).

        Verifies against `self.current` (the (branch, facility) last applied, NOT cfg).
        Falls back to the full open_membership + apply_filters flow if the refreshed
        page's selects no longer match self.current, or it isn't the selection page anymore.
        """
        t0 = time.monotonic()
        if self.current is None:
            raise SiteError("refresh: self.current is None; call ensure_ready/switch_to first")
        branch, facility = self.current
        try:
            # A postback that keeps the same facility selected does NOT re-query the
            # grid: ASP.NET only fires SelectedIndexChanged when the value changes, so
            # the page re-renders the old grid from ViewState (measured live: 30
            # refreshes showed no change while a fresh load did). Deselect, then
            # re-select, so the server really reloads the sessions.
            self.limiter.acquire()
            with self.page.expect_navigation(wait_until="domcontentloaded"):
                self.page.locator("#ddlTesisFiltre").select_option(index=0)
            self._check_session()
            self.limiter.acquire()
            with self.page.expect_navigation(wait_until="domcontentloaded"):
                self.page.locator("#ddlTesisFiltre").select_option(label=facility)
            self._check_session()

            html = self.page.content()
            if not is_selection_page(html):
                self.log.warning("refresh: no longer on selection page, falling back")
                return self.ensure_ready(branch, facility)

            grid = parse_grid(html)
            if grid.branch_selected != branch or grid.facility_selected != facility:
                self.log.warning("refresh: filters drifted from current, falling back")
                return self.ensure_ready(branch, facility)

            self.log.info("refresh done in %.0fms", (time.monotonic() - t0) * 1000)
            return grid
        except LoggedOut:
            raise
        except Exception:
            self.log.exception("refresh failed, falling back to full flow")
            return self.ensure_ready(branch, facility)

    @_needs_page
    def switch_to(self, branch: str, facility: str) -> Grid:
        """Get to (branch, facility)'s grid with minimal postbacks.

        same as self.current -> refresh(); same branch -> one facility postback;
        otherwise -> branch postback (wait nav) then facility postback; if not currently
        on the selection page -> full open_membership + apply_filters. Verifies the
        selected texts after each postback; on a mismatch, falls back to the full flow
        once (via ensure_ready, which itself raises SiteError if that also fails).
        """
        t0 = time.monotonic()
        target = (branch, facility)
        if self.current == target:
            return self.refresh()

        try:
            on_selection_page = is_selection_page(self.page.content())
        except Exception:
            on_selection_page = False

        if not on_selection_page:
            grid = self.ensure_ready(branch, facility)
            self.log.info("switch_to (full open) done in %.0fms", (time.monotonic() - t0) * 1000)
            return grid

        try:
            if self.current is not None and self.current[0] == branch:
                grid = self._select_facility(facility)
            else:
                self._select_branch(branch)
                grid = self._select_facility(facility)
            self.current = target
            self.log.info("switch_to done in %.0fms", (time.monotonic() - t0) * 1000)
            return grid
        except LoggedOut:
            raise
        except Exception:
            self.log.warning("switch_to: minimal postback mismatch, falling back to full flow once")
            grid = self.ensure_ready(branch, facility)
            self.log.info("switch_to (fallback) done in %.0fms", (time.monotonic() - t0) * 1000)
            return grid

    # -- catalog scan (I/O only; src/catalog.py owns the Catalog model) ------

    @_needs_page
    def iter_scan(
        self,
        branches: list[str] | None,
        depth: str,
        stop: "threading.Event | None" = None,
    ) -> Iterator[ScanItem]:
        """Walk branches -> facilities -> grids, yielding progress as ScanItem.

        depth "branches": one ScanItem(kind="branches") with every branch name.
        depth "facilities": that, plus one ScanItem(kind="facilities") per requested
            branch (default: every branch).
        depth "grids": that, plus one ScanItem(kind="grid") per facility of every
            requested branch (visited via switch_to).
        Checks `stop` between steps; every navigation goes through self.limiter.acquire()
        (inside list_branches/list_facilities/switch_to).
        """
        if depth not in ("branches", "facilities", "grids"):
            raise ValueError(f"unknown scan depth: {depth!r}")
        if self._stopped(stop):
            return

        all_branches = self.list_branches()

        if depth == "branches":
            yield ScanItem(
                kind="branches", branch=None, facility=None, names=list(all_branches),
                grid=None, done=1, total=1,
            )
            return

        requested = [b for b in branches] if branches else list(all_branches)
        yield ScanItem(
            kind="branches", branch=None, facility=None, names=list(all_branches),
            grid=None, done=0, total=len(requested),
        )

        branch_facilities: dict[str, list[str]] = {}
        for i, branch in enumerate(requested, start=1):
            if self._stopped(stop):
                return
            facilities = self.list_facilities(branch)
            branch_facilities[branch] = facilities
            yield ScanItem(
                kind="facilities", branch=branch, facility=None, names=facilities,
                grid=None, done=i, total=len(requested),
            )

        if depth == "facilities":
            return

        total = sum(len(v) for v in branch_facilities.values())
        done = 0
        for branch, facilities in branch_facilities.items():
            for facility in facilities:
                if self._stopped(stop):
                    return
                grid = self.switch_to(branch, facility)
                done += 1
                yield ScanItem(
                    kind="grid", branch=branch, facility=facility, names=[facility],
                    grid=grid, done=done, total=total,
                )

    @_needs_page
    def scan_facilities(self, branch: str | None = None) -> list[tuple[str, Grid]]:
        """Read-only: every facility offered for `branch` (default cfg.site.branch_text).

        Returns (facility name, grid) per facility. Never selects a session. Built on
        iter_scan(depth="grids") -- kept for the CLI `scan` subcommand.
        """
        branch = self._resolve_branch(branch)
        results: list[tuple[str, Grid]] = []
        for item in self.iter_scan([branch], depth="grids"):
            if item.kind == "grid" and item.grid is not None and item.facility is not None:
                results.append((item.facility, item.grid))
        return results

    # -- session card actions ----------------------------------------------

    def select_card(self, card: SessionCard) -> Grid:
        """Click a card's checkbox and verify it becomes SELECTED."""
        t0 = time.monotonic()
        if not card.checkbox_id:
            raise SiteError(f"card {card.describe()} has no checkbox (status={card.status})")

        self.artifacts.save(self.page, "found")

        self.limiter.acquire()
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            self.page.locator(f'[id="{card.checkbox_id}"]').click()
        self._check_session()

        grid = parse_grid(self.page.content())
        updated = self._same_card(grid, card)
        if updated is None or updated.status != Status.SELECTED:
            self.artifacts.save(self.page, "error_select_failed", force=True)
            status = updated.status if updated is not None else "missing"
            raise SiteError(f"card {card.describe()} did not become SELECTED (status={status})")

        self.artifacts.save(self.page, "selected")
        self.log.info(
            "select_card %s done in %.0fms", card.describe(), (time.monotonic() - t0) * 1000
        )
        return grid

    def unselect_card(self, card: SessionCard) -> Grid:
        """Click a SELECTED card's checkbox again to release it. Used by dry-run."""
        t0 = time.monotonic()
        if not card.checkbox_id:
            raise SiteError(f"card {card.describe()} has no checkbox to unselect")

        self.limiter.acquire()
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            self.page.locator(f'[id="{card.checkbox_id}"]').click()
        self._check_session()

        grid = parse_grid(self.page.content())
        updated = self._same_card(grid, card)
        if updated is not None and updated.status == Status.SELECTED:
            self.artifacts.save(self.page, "error_unselect_failed", force=True)
            raise SiteError(f"card {card.describe()} still SELECTED after unselect click")

        self.log.info(
            "unselect_card %s done in %.0fms", card.describe(), (time.monotonic() - t0) * 1000
        )
        return grid

    def click_save(self, card: SessionCard) -> SaveOutcome:
        """Check the confirm box and click Kaydet ONCE. Handles dialogs and result banners.

        This is the ONLY place in the codebase allowed to click #lbtnKaydet -- each click
        can trigger an SMS to the user's phone (see incident notes on the caller side, which
        must enforce a hard cap on calls to this method per run).

        Tolerates the save click producing no navigation within 15s (e.g. blocked on
        a JS dialog) rather than raising. Never clicks anything after Kaydet itself.
        """
        t0 = time.monotonic()
        self._dismiss_cookie_banner()
        self.artifacts.save(self.page, "before_save")

        dialogs: list[str] = []

        def _on_dialog(dialog) -> None:
            dialogs.append(dialog.message)
            try:
                dialog.accept()
            except Exception:
                self.log.exception("failed to accept dialog: %s", dialog.message)

        self.page.on("dialog", _on_dialog)
        try:
            onay = self.page.locator("#pageContent_cboxOnay")
            if not onay.is_checked():
                onay.check()
            if not onay.is_checked():
                raise SiteError("confirm checkbox (#pageContent_cboxOnay) could not be checked")

            self.limiter.acquire()
            try:
                with self.page.expect_navigation(wait_until="domcontentloaded", timeout=15_000):
                    self.page.locator("#lbtnKaydet").click()
            except PlaywrightTimeoutError:
                self.log.warning("click_save: no navigation within 15s after Kaydet click")

            self._check_session()
        finally:
            self.page.remove_listener("dialog", _on_dialog)

        messages = self._collect_result_messages()
        self.artifacts.save(self.page, "after_save")

        outcome = self._classify_save_page(card, dialogs, messages, "error_save_not_confirmed")
        self.log.info(
            "click_save %s -> %s in %.0fms",
            card.describe(),
            outcome.kind,
            (time.monotonic() - t0) * 1000,
        )
        return outcome

    def sms_pending(self) -> bool:
        """Is the SMS verification code field present & visible on the current page?

        Safe to call at any time, including while the human is mid-interaction with the
        page or a postback is in flight -- never raises, returns False on any error.
        """
        return self._sms_field_visible()

    def submit_sms(self, code: str, card: SessionCard) -> SaveOutcome:
        """Fill the SMS code field and click 'Kodunu Doğrula'. NEVER clicks Kaydet.

        Tolerates no navigation within 15s, same as click_save. Wrong/expired codes are
        expected to leave the SMS field visible (kind="sms") rather than raise -- the
        caller should let the human try again without touching Kaydet.
        """
        t0 = time.monotonic()
        self.limiter.acquire()
        try:
            with self.page.expect_navigation(wait_until="domcontentloaded", timeout=15_000):
                self.page.locator(_SMS_CODE_SELECTOR).fill(code)
                self.page.locator(_SMS_SUBMIT_SELECTOR).click()
        except PlaywrightTimeoutError:
            self.log.warning("submit_sms: no navigation within 15s after doğrula click")

        self._check_session()

        messages = self._collect_result_messages()
        self.artifacts.save(self.page, "sms_submitted")

        outcome = self._classify_save_page(card, [], messages, "error_sms_not_confirmed")
        self.log.info(
            "submit_sms %s -> %s in %.0fms",
            card.describe(),
            outcome.kind,
            (time.monotonic() - t0) * 1000,
        )
        return outcome

    def check_booked(self, card: SessionCard) -> SaveOutcome:
        """Re-parse the current page WITHOUT navigating (the human may have submitted the
        SMS code themselves, in the bot's own browser window). Never clicks anything."""
        messages = self._collect_result_messages()
        return self._classify_save_page(card, [], messages, "error_check_booked")

    def _classify_save_page(
        self, card: SessionCard, dialogs: list[str], messages: list[str], error_step: str
    ) -> SaveOutcome:
        """Shared result classifier for click_save/submit_sms/check_booked (live,
        Playwright-locator-backed inputs). The actual decision is the pure
        `_decide_save_outcome` below -- see `classify_save_page_html` for the same
        decision made from a saved HTML string (fixtures, tools/ci_smoke.py), with no
        Playwright/browser involved.
        """
        sms_visible = self._sms_field_visible()
        success_texts = self._visible_texts(".alert-success")
        html = self.page.content()
        grid = parse_grid(html) if is_selection_page(html) else None
        updated = self._same_card(grid, card) if grid is not None else None

        outcome = _decide_save_outcome(
            sms_visible=sms_visible,
            success_texts=success_texts,
            updated_card=updated,
            card=card,
            dialogs=dialogs,
            messages=messages,
        )
        if outcome.kind == "failed":
            self.artifacts.save(self.page, error_step, force=True)
        return outcome

    def _visible_texts(self, selector: str) -> list[str]:
        texts: list[str] = []
        try:
            loc = self.page.locator(selector)
            for i in range(loc.count()):
                el = loc.nth(i)
                if el.is_visible():
                    texts.append(el.inner_text().strip())
        except Exception:
            self.log.debug("could not read %s", selector, exc_info=True)
        return texts

    def _sms_field_visible(self) -> bool:
        try:
            loc = self.page.locator(_SMS_CODE_SELECTOR)
            return loc.count() > 0 and loc.first.is_visible()
        except Exception:
            return False

    # -- existing reservations (Seanslarım > Detay) ---------------------------

    @_needs_page
    def open_reservations(self) -> list["Reservation"]:
        """Open Seanslarım, expand the membership row's detail table, return its reservations.

        Row selection follows open_membership's rule: self.membership_text if truthy, else
        the first row that has a detail (İptal/Detay) link.
        """
        from src.reservations import parse_reservations

        self.limiter.acquire()
        self.page.goto(f"{self.cfg.site.base_url}/uyespor", wait_until="domcontentloaded")
        if self.page.locator("a[id*=lbtnSeansSecim]").count() == 0 and self._login_signals():
            raise LoggedOut("giriş yapılmamış (Seanslarım sayfası açılmadı)")

        if self.membership_text:
            row = self.page.locator("tr").filter(has_text=self.membership_text)
            link = row.first.locator("a[id*=lbtnDetayGoster]")
            if row.count() == 0 or link.count() == 0:
                self.artifacts.save(self.page, "error_reservations_no_detail", force=True)
                raise SiteError("üyelik satırında detay bağlantısı bulunamadı")
        else:
            link = self.page.locator("a[id*=lbtnDetayGoster]")
            if link.count() == 0:
                self.artifacts.save(self.page, "error_reservations_no_detail", force=True)
                raise SiteError("üyelik satırında detay bağlantısı bulunamadı")

        self.limiter.acquire()
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            link.first.click()
        self._check_session()
        self.artifacts.save(self.page, "reservations")
        return parse_reservations(self.page.content())

    def cancel_reservation(
        self,
        res: "Reservation",
        sms_timeout_s: float = 300,
        stop: "threading.Event | None" = None,
    ) -> tuple[bool, str]:
        """Click this reservation's İptal Et ONCE, then verify via a fresh detail table.

        Accepts (and records) any JS confirm dialog. If the site asks for an SMS code, waits
        for the human to enter it in the bot window (never clicks İptal Et again) -- the wait
        is stop-aware (via `stop`, honoured in <=1s steps), raising Stopped promptly instead
        of polling to the full `sms_timeout_s` once Stop is requested.
        Must be called right after open_reservations() (the link lives on that page).
        """
        from src.reservations import find_reservation

        if not res.cancel_id:
            raise SiteError(f"bu rezervasyon iptal edilemez: {res.describe()}")
        dialogs: list[str] = []

        def _on_dialog(dialog) -> None:
            dialogs.append(dialog.message)
            dialog.accept()

        self._dismiss_cookie_banner()
        self.artifacts.save(self.page, "before_cancel")
        self.page.on("dialog", _on_dialog)
        try:
            self.limiter.acquire()
            try:
                with self.page.expect_navigation(wait_until="domcontentloaded", timeout=15_000):
                    self.page.locator(f'[id="{res.cancel_id}"]').click()
            except PlaywrightTimeoutError:
                self.log.warning("cancel: no navigation within 15s after İptal Et click")
            self._check_session()
        finally:
            self.page.remove_listener("dialog", _on_dialog)
        messages = self._collect_result_messages()
        self.artifacts.save(self.page, "after_cancel", force=True)

        if self._sms_field_visible():
            from src.notify import alert, notify

            notify("SMS kodu gerekli", "İptal için gelen kodu bot penceresine girip doğrulayın")
            alert(3)
            deadline = time.monotonic() + sms_timeout_s
            while self._sms_field_visible() and time.monotonic() < deadline:
                self._sleep(1.0, stop)
                if self._stopped(stop):
                    raise Stopped("iptal SMS beklerken durduruldu")
            if self._sms_field_visible():
                return False, "SMS kodu zamanında girilmedi"
            messages += self._collect_result_messages()

        parts = [m for m in (*dialogs, *messages) if m]
        after = find_reservation(self.open_reservations(), res.date, res.start)
        ok = after is None or after.cancel_id is None and after.status != res.status
        return ok, " | ".join(parts) or ("İptal edildi." if ok else "Sonuç belirlenemedi.")

    # -- small helpers -------------------------------------------------------

    @staticmethod
    def _same_card(grid: Grid, card: SessionCard) -> SessionCard | None:
        """Find `card` again after a postback: same id AND court at the same slot, else
        the same-court card at that slot (find_card).

        Court equality is required before trusting an id match: the repeater's ids are
        index-based, so after a postback re-renders the grid in a different order, the
        same id can now belong to a DIFFERENT court's card -- matching by id alone could
        silently confirm the wrong card as "found again" (see the hard safety rule:
        never select/confirm a non-matching card). find_card (date/start/court) is still
        used as a last resort when no id+court match exists.
        """
        for c in grid.cards:
            if (
                c.card_id == card.card_id
                and c.date == card.date
                and c.start == card.start
                and c.court == card.court
            ):
                return c
        return find_card(grid, card.date, card.start, card.court)

    def _dismiss_cookie_banner(self) -> None:
        """Close the cookie banner via its close button (never the accept button)."""
        try:
            btn = self.page.locator("button.closecookie")
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click()
        except Exception:
            self.log.exception("failed to dismiss cookie banner")

    def _collect_result_messages(self) -> list[str]:
        """Grab visible text from any alert/modal/message elements after a save attempt."""
        texts: list[str] = []
        try:
            locator = self.page.locator(_RESULT_MESSAGE_SELECTOR)
            for i in range(locator.count()):
                el = locator.nth(i)
                try:
                    if el.is_visible():
                        text = el.inner_text().strip()
                        if text:
                            texts.append(text)
                except Exception:
                    continue
        except Exception:
            self.log.exception("failed to collect result messages")
        return texts


# -- pure, Playwright-free classification from saved HTML (fixtures / tools/ci_smoke.py) --


def classify_save_page_html(
    html: str, card: SessionCard, dialogs: Iterable[str] = (), messages: Iterable[str] = ()
) -> SaveOutcome:
    """Classify a post-Kaydet/post-SMS page from its saved HTML alone -- no Playwright, no
    browser, no I/O. Used by tests (against real, sanitized fixture HTML -- see
    tests/fixtures/save_sms_page.html / save_success_page.html) and by
    tools/ci_smoke.py's offline checks.

    `Site._classify_save_page` is the live, Playwright-locator-backed wrapper that feeds
    the same decision core (`_decide_save_outcome`) with inputs read from a real page
    instead of a static HTML string.
    """
    sms_visible = _sms_field_visible_in_html(html)
    success_texts = _visible_success_texts_in_html(html)
    grid = parse_grid(html) if is_selection_page(html) else None
    updated = Site._same_card(grid, card) if grid is not None else None

    return _decide_save_outcome(
        sms_visible=sms_visible,
        success_texts=success_texts,
        updated_card=updated,
        card=card,
        dialogs=list(dialogs),
        messages=list(messages),
    )
