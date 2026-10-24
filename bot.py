#!/usr/bin/env python3
"""bot.py -- root entry point for the online.spor.istanbul (İBB Spor İstanbul) reservation bot.

Usage:
    python bot.py [--config PATH] [-v] <command> [options]

Commands:
    login                          open a browser, log in by hand, save the session
    save-login                     save TC/pasaport no and password to OS keychain
    forget-login                   remove saved credentials from OS keychain
    probe [--branch B] [--facility F]  one-shot read of the grid; prints a table
    watch [--minutes N]            poll and log status transitions to state/observations.jsonl
    scan [--branch B]              scan all facilities of a branch
    reservations                   list current reservations
    cancel --date DD.MM.YYYY --start HH:MM [--yes]  cancel a reservation
    run [--dry-run] [--once] [--headless]  the sniper loop: wait for a preferred slot, book it
    catalog [--depth branches|facilities|grids] [--branch B ...]  scan and build catalog
    targets [--json]               print enabled targets from settings.json
    version                        print the app version
    diagnostics [--dest DIR]       build a redacted diagnostics ZIP (default: Desktop)
    gui                            start the web UI (pywebview + src/webapp.py, via app.pyw)
    reminders [--off]              show/disable reminder notifications (state/reminders/)

Exit codes:
    0  success / normal exit
    1  error
    2  logged out (run `python bot.py login`)
    3  config/settings error
    4  another instance is already running (state/app.lock held by app.pyw or bot.py)
    130  interrupted (Ctrl+C)

Every subcommand that drives a real browser (login, probe, watch, run, scan,
reservations, cancel, catalog), plus save-login and forget-login (which change the
active account / credentials), takes the same single-instance lock app.pyw uses
(state/app.lock) before touching Playwright, so this CLI and the running app can
never drive the same Chromium profile at once.

Runs the same regardless of the current working directory: --config defaults to
config.toml next to this file.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.toml"

TR_WEEKDAYS_SHORT = ("Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz")


def _load_config(path):
    """Load config.toml, or exit 3 with a clear message."""
    from src.config import ConfigError, load_config

    try:
        return load_config(path)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        raise SystemExit(3)


def _load_settings(cfg):
    """Load settings.json with legacy migration, or exit 3 with a clear message."""
    from src.settings import load_settings, SettingsError

    try:
        return load_settings(legacy_cfg=cfg)
    except SettingsError as e:
        print(f"settings error: {e}", file=sys.stderr)
        raise SystemExit(3)


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------

def cmd_login(args: argparse.Namespace) -> int:
    from src.browser import run_login, setup_logging

    setup_logging(args.verbose)
    cfg = _load_config(args.config)
    return 0 if run_login(cfg) else 1


def cmd_save_login(args: argparse.Namespace) -> int:
    """Prompt for TC/pasaport no + şifre and save them to the OS keychain."""
    import getpass

    from src.browser import setup_logging
    from src.credentials import mask_tc
    from src.credentials import save as save_credentials

    setup_logging(args.verbose)

    tc = input("TC Kimlik No (veya pasaport no): ").strip()
    password = getpass.getpass("Şifre: ")
    password2 = getpass.getpass("Şifre (tekrar): ")

    if password != password2:
        print("Şifreler eşleşmiyor; kaydedilmedi.", file=sys.stderr)
        return 1
    if not tc or not password:
        print("TC/pasaport no ve şifre boş olamaz; kaydedilmedi.", file=sys.stderr)
        return 1

    try:
        save_credentials(tc, password)
    except ValueError as e:
        print(f"Geçersiz TC/pasaport no: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Kimlik bilgileri kaydedilemedi (işletim sistemi anahtarlığına erişilemedi): {e}", file=sys.stderr)
        return 1

    if sys.platform == "win32":
        store = "Windows Kimlik Bilgisi Yöneticisi"
    elif sys.platform == "darwin":
        store = "macOS Anahtar Zinciri"
    else:
        store = "sistem anahtarlığı (Secret Service)"
    print(f"Kimlik bilgileri {store}'na kaydedildi (TC: {mask_tc(tc)}).")
    print("Bot artık oturum kapandığında kendiliğinden yeniden giriş yapabilir.")
    print("Kaldırmak için: python bot.py forget-login")

    if args.check:
        return _check_auto_login(args)
    return 0


def _check_auto_login(args: argparse.Namespace) -> int:
    """Open the bot browser and try Site.auto_login() once, printing the result."""
    from src.browser import Artifacts, RateLimiter, open_browser
    from src.site import Site

    cfg = _load_config(args.config)
    print("\nOtomatik giriş test ediliyor (tarayıcı açılacak)...")
    with open_browser(headless=False) as bs:
        limiter = RateLimiter(cfg.timing.max_requests_per_min)
        site = Site(bs.page, cfg, Artifacts(enabled=True), limiter, window=bs.window)
        ok = site.auto_login()
    print("Otomatik giriş testi: " + ("BAŞARILI ✅" if ok else "BAŞARISIZ ❌ (state/artifacts altında ekran görüntüsüne bakın)"))
    return 0 if ok else 1


def cmd_forget_login(args: argparse.Namespace) -> int:
    """Remove any credentials saved to the OS keychain by save-login."""
    from src.browser import setup_logging
    from src.credentials import clear as clear_credentials

    setup_logging(args.verbose)
    clear_credentials()
    print("Kayıtlı kimlik bilgileri silindi (varsa).")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from src.browser import Artifacts, RateLimiter, open_browser, setup_logging
    from src.planner import Planner, windows
    from src.site import Site

    setup_logging(args.verbose)
    cfg = _load_config(args.config)
    settings = _load_settings(cfg)
    tz = ZoneInfo(cfg.site.timezone)

    with open_browser(headless=False) as bs:
        limiter = RateLimiter(cfg.timing.max_requests_per_min)
        artifacts = Artifacts(enabled=True, window=bs.window)
        site = Site(bs.page, cfg, artifacts, limiter, window=bs.window)
        site.login_if_needed()

        # Switch to first enabled target (or any one for display purposes)
        targets_to_use = tuple(t for t in settings.targets if t.enabled)
        if not targets_to_use:
            print("Etkinleştirilen hedef yok. settings.json'da hedef tanımlayın.", file=sys.stderr)
            return 1

        first = targets_to_use[0]
        grid = site.switch_to(first.branch, first.facility)

        # Print the grid as a table
        print(f"{'Tarih':<12}{'Gün':<5}{'Saat':<13}{'Durum':<10}{'Seviye':<24}{'Kontenjan':<10}")
        print("-" * 74)
        for card in grid.cards:
            gun = TR_WEEKDAYS_SHORT[card.date.weekday()]
            saat = f"{card.start}-{card.end}"
            status = card.status.value if hasattr(card.status, "value") else str(card.status)
            print(
                f"{str(card.date):<12}{gun:<5}{saat:<13}{status:<10}"
                f"{(card.level or ''):<24}{str(card.quota):<10}"
            )

        # Show which targets would match and next window
        from src.targets import find_target_multi

        booked_dates = frozenset(
            r.date for r in site.open_reservations() if r.status == "Satış Yapıldı"
        )
        targets_in_group = tuple(t for t in targets_to_use if t.branch == first.branch and t.facility == first.facility)
        hit = find_target_multi(grid, targets_in_group, booked_dates)
        if hit:
            target, card = hit
            print(f"\nEşleşen hedef: {target.name} -> {card.describe()}")
        else:
            print("\nEşleşen hedef yok (uygun/açık kart bulunamadı).")

        now = datetime.now(tz)
        try:
            from src.catalog import Catalog

            upcoming = windows(targets_to_use, Catalog.load(), now, cfg.timing, tz)
            if upcoming:
                window = upcoming[0]
                print(
                    f"Sıradaki açılış: {window.target_name} -> seans {window.session_start.isoformat()}, "
                    f"açılış {window.open_at.isoformat()}"
                )
            else:
                print("Sıradaki açılış bulunamadı.")
        except ValueError as e:
            print(f"Sıradaki açılış hesaplanamadı: {e}")

        artifacts.save(bs.page, "probe", force=True)
        print("\nHTML + ekran görüntüsü kaydedildi: state/artifacts/<timestamp>_probe.{html,png}")

    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    import json
    import logging

    from src.browser import Artifacts, LoggedOut, RateLimiter, open_browser, setup_logging
    from src.runner import OBSERVATIONS_PATH, record_observations
    from src.site import Site
    from src.planner import Planner

    setup_logging(args.verbose)
    log = logging.getLogger("ibbspor.bot")
    cfg = _load_config(args.config)
    settings = _load_settings(cfg)
    tz = ZoneInfo(cfg.site.timezone)

    from datetime import timedelta
    deadline = datetime.now(tz) + timedelta(minutes=args.minutes) if args.minutes else None

    with open_browser(headless=False) as bs:
        limiter = RateLimiter(cfg.timing.max_requests_per_min)
        artifacts = Artifacts(enabled=cfg.safety.screenshot_every_step, window=bs.window)
        site = Site(bs.page, cfg, artifacts, limiter, window=bs.window)
        site.login_if_needed()

        # Build planner from settings
        try:
            from src.catalog import Catalog

            planner = Planner([t for t in settings.targets if t.enabled], Catalog.load(), cfg.timing, tz)
        except ValueError as e:
            print(f"Planner hatası: {e}", file=sys.stderr)
            return 1

        prev_grid = None
        current_group = None

        while True:
            try:
                now = datetime.now(tz)
                if deadline and now >= deadline:
                    break

                # Get next group to poll
                plan = planner.next(now)
                if plan.group != current_group:
                    current_group = plan.group
                    grid = site.switch_to(current_group[0], current_group[1])
                else:
                    grid = site.refresh()

                # Record observations
                events = record_observations(prev_grid, grid, branch=current_group[0], facility=current_group[1])
                for e in events:
                    print(f"{e['ts']}  {e['date']} {e['start']}-{e['end']} {e['level']}: {e.get('old')} -> {e.get('new')} (kontenjan {e['quota']})")
                prev_grid = grid

                time.sleep(plan.sleep_s)

            except LoggedOut:
                ts = datetime.now(tz).isoformat()
                log.warning("Oturum kapanmış (watch), %s", ts)
                artifacts.save(bs.page, "logged_out", force=True)
                OBSERVATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
                with OBSERVATIONS_PATH.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": ts, "event": "logged_out"}, ensure_ascii=False) + "\n")
                while True:
                    site.login_if_needed()
                    try:
                        grid = site.ensure_ready()
                        break
                    except LoggedOut:
                        continue
                prev_grid = None
                current_group = None

    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from src.browser import setup_logging
    from src.runner import run_cli

    setup_logging(args.verbose)
    cfg = _load_config(args.config)
    dry_run = args.dry_run or cfg.safety.dry_run
    return run_cli(cfg, dry_run=dry_run, once=args.once)


def cmd_scan(args: argparse.Namespace) -> int:
    from collections import Counter

    from src.browser import Artifacts, RateLimiter, open_browser, setup_logging
    from src.site import Site

    setup_logging(args.verbose)
    cfg = _load_config(args.config)
    settings = _load_settings(cfg)

    # Determine the branch: use --branch if provided, else first enabled target's branch
    branch = args.branch
    if not branch:
        enabled_targets = tuple(t for t in settings.targets if t.enabled)
        if not enabled_targets:
            print("Etkinleştirilen hedef yok. settings.json'da hedef tanımlayın.", file=sys.stderr)
            return 1
        branch = enabled_targets[0].branch

    with open_browser(headless=False) as bs:
        artifacts = Artifacts(enabled=cfg.safety.screenshot_every_step, window=bs.window)
        limiter = RateLimiter(cfg.timing.max_requests_per_min)
        site = Site(bs.page, cfg, artifacts, limiter, window=bs.window)
        site.login_if_needed()

        results = site.scan_facilities(branch)

    print(f"\n{branch} — {len(results)} tesis")
    for name, grid in results:
        counts = Counter(c.status.value for c in grid.cards)
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "seans yok"
        print(f"- {name}: {summary}")
        for c in grid.cards:
            if c.status.value == "open":
                print(f"    AÇIK: {c.describe()}")
    return 0


def cmd_reservations(args: argparse.Namespace) -> int:
    from src.browser import Artifacts, RateLimiter, open_browser, setup_logging
    from src.site import Site

    setup_logging(args.verbose)
    cfg = _load_config(args.config)

    with open_browser(headless=False) as bs:
        artifacts = Artifacts(enabled=cfg.safety.screenshot_every_step, window=bs.window)
        limiter = RateLimiter(cfg.timing.max_requests_per_min)
        site = Site(bs.page, cfg, artifacts, limiter, window=bs.window)
        site.login_if_needed()
        items = site.open_reservations()

    if not items:
        print("Rezervasyon bulunamadı.")
    for r in items:
        print(r.describe())
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    from src.browser import Artifacts, RateLimiter, open_browser, setup_logging
    from src.reservations import find_reservation
    from src.site import Site

    setup_logging(args.verbose)

    try:
        day = datetime.strptime(args.date, "%d.%m.%Y").date()
        datetime.strptime(args.start, "%H:%M")
    except ValueError:
        print("Tarih GG.AA.YYYY, saat SS:DD biçiminde olmalı (ör. 25.09.2026 --start 16:00)", file=sys.stderr)
        return 3

    cfg = _load_config(args.config)
    with open_browser(headless=False) as bs:
        artifacts = Artifacts(enabled=cfg.safety.screenshot_every_step, window=bs.window)
        limiter = RateLimiter(cfg.timing.max_requests_per_min)
        site = Site(bs.page, cfg, artifacts, limiter, window=bs.window)
        site.login_if_needed()
        res = find_reservation(site.open_reservations(), day, args.start)

        if res is None:
            print(f"{args.date} {args.start} için rezervasyon bulunamadı. `bot.py reservations` ile listeleyin.")
            return 1
        print("İptal edilecek:", res.describe())
        if res.cancel_id is None:
            print("Bu rezervasyon iptal edilemez (seansa 3 saatten az kalmış ya da geçmiş olabilir).")
            return 1
        if not args.yes and input("Emin misiniz? (evet/hayır): ").strip().lower() not in ("evet", "e", "yes", "y"):
            print("Vazgeçildi.")
            return 0
        ok, message = site.cancel_reservation(res, sms_timeout_s=cfg.sms.timeout_s)

    print(("İPTAL EDİLDİ ✅ " if ok else "İptal doğrulanamadı ❌ ") + message)
    return 0 if ok else 1


def cmd_catalog(args: argparse.Namespace) -> int:
    """Scan and build catalog with progress reporting."""
    from src.browser import Artifacts, RateLimiter, open_browser, setup_logging
    from src.site import Site
    from src.catalog import Catalog

    setup_logging(args.verbose)
    cfg = _load_config(args.config)
    settings = _load_settings(cfg)

    depth = args.depth or "grids"
    branches = args.branch if args.branch else None

    with open_browser(headless=False) as bs:
        artifacts = Artifacts(enabled=cfg.safety.screenshot_every_step, window=bs.window)
        limiter = RateLimiter(cfg.timing.max_requests_per_min)
        site = Site(bs.page, cfg, artifacts, limiter, window=bs.window)
        site.login_if_needed()

        catalog = Catalog.load()
        now = datetime.now(ZoneInfo(cfg.site.timezone))

        branch_count = 0
        facility_count = 0
        hall_count = 0
        slot_count = 0

        for item in site.iter_scan(branches, depth):
            if item.kind == "branches":
                print(f"Şubeler: {len(item.names)}")
                branch_count = len(item.names)
            elif item.kind == "facilities":
                print(f"  {item.branch}: {len(item.names)} tesis")
                facility_count += len(item.names)
                catalog.observe_facilities(item.branch, item.names, now)
            elif item.kind == "grid":
                hall_count += len({c.court for c in item.grid.cards}) if item.grid else 0
                slot_count += len(item.grid.cards) if item.grid else 0
                if item.grid:
                    catalog.observe(item.branch, item.facility, item.grid, now)
                print(f"    {item.facility}: {item.done}/{item.total}")

        catalog.save()
        print(f"\nKatalog güncellendi: {branch_count} şube, {facility_count} tesis, ~{hall_count} salon, ~{slot_count} saat")
        return 0


def cmd_targets(args: argparse.Namespace) -> int:
    """Print targets from settings.json."""
    from src.settings import SETTINGS_PATH

    cfg = _load_config(args.config)
    settings = _load_settings(cfg)

    if args.json:
        from src.settings import _target_to_dict
        data = {
            "version": settings.version,
            "membership_text": settings.membership_text,
            "dry_run": settings.dry_run,
            "stop_after_success": settings.stop_after_success,
            "targets": [_target_to_dict(t) for t in settings.targets],
        }
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(f"Hedefler ({SETTINGS_PATH}):\n")
        for i, target in enumerate(settings.targets, 1):
            status = "✓" if target.enabled else "✗"
            print(f"{i}. [{status}] {target.name}")
            print(f"   {target.describe()}")

    print(f"\nAyarlar dosyası: {SETTINGS_PATH}")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    """Print the app version (see src/version.py: git-archive export-subst, else
    `git describe`, else a "development" placeholder)."""
    from src.version import get_version

    print(get_version())
    return 0


def cmd_diagnostics(args: argparse.Namespace) -> int:
    """Build a redacted diagnostics ZIP (see src/diagnostics.py) for sending to the
    bot's author. Default destination: the Desktop."""
    from src.diagnostics import create_diagnostics_zip

    dest = args.dest if args.dest is not None else (Path.home() / "Desktop")

    try:
        out_path = create_diagnostics_zip(dest)
    except Exception as e:
        print(f"Tanı paketi oluşturulamadı: {e}", file=sys.stderr)
        return 1

    print(f"Tanı paketi oluşturuldu: {out_path}")
    print("Bu dosyayı güvendiğiniz birine (botun yazarına) gönderebilirsiniz.")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    """Start the web UI: launches app.pyw (pywebview window backed by src/webapp.py)
    as a subprocess. Not a Tk GUI -- the old Tk interface (src/gui.py) was replaced by
    the pywebview + src/webapp.py + src/web/ web UI."""
    import subprocess

    try:
        from src.browser import setup_logging
        setup_logging(args.verbose)
    except Exception:
        pass

    # Find app.pyw next to bot.py
    app_path = PROJECT_ROOT / "app.pyw"
    if not app_path.exists():
        print(f"GUI uygulaması bulunamadı: {app_path}", file=sys.stderr)
        return 1

    try:
        # On Windows and macOS, pythonw or python should work
        subprocess.Popen([sys.executable, str(app_path)])
        return 0
    except Exception as e:
        print(f"GUI başlatılamadı: {e}", file=sys.stderr)
        return 1


def _format_reminder_row(r) -> str:
    """One line for a `src.reminders.Reminder`, e.g. "Cum 23.10 18:45 · Pazartesi 26.10
    19:00 seansı · hafta içi tenis"."""
    from src.config import WEEKDAYS
    from src.targets import TR_DAY

    remind = f"{TR_WEEKDAYS_SHORT[r.remind_at.weekday()]} {r.remind_at.strftime('%d.%m %H:%M')}"
    session_day = TR_DAY[WEEKDAYS[r.session_start.weekday()]]
    session = f"{session_day} {r.session_start.strftime('%d.%m %H:%M')} seansı"
    return f"{remind} · {session} · {r.target_name}"


def cmd_reminders(args: argparse.Namespace) -> int:
    """`reminders [--off]`: prints whether reminder notifications are enabled, the OS
    scheduler registration-cache status, and the next upcoming reminders. Read-only by
    default -- never calls `src.reminder_sched.sync` (no OS scheduler registration) unless
    `--off` is given, which disables reminders and unregisters the scheduler entry."""
    from src import catalog as catalog_mod
    from src import reminder_sched
    from src import settings as settings_mod
    from src.reminders import read_tennis_history, upcoming

    cfg = _load_config(args.config)
    settings = _load_settings(cfg)

    if args.off:
        from dataclasses import replace

        settings = replace(settings, reminders_enabled=False)
        settings_mod.save_settings(settings, path=settings_mod.SETTINGS_PATH)
        result = reminder_sched.sync([], enabled=False)
        if result.error:
            print(f"Hatırlatmalar kapatıldı (zamanlayıcıdan kaldırılamadı: {result.error}).")
        else:
            print("Hatırlatmalar kapatıldı.")
        return 0

    print("Hatırlatmalar: " + ("açık" if settings.reminders_enabled else "kapalı"))

    cache_path = reminder_sched.REG_DIR / "registered.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        print(f"Zamanlayıcı kaydı: {len(cache.get('slots', []))} tetikleyici kayıtlı.")
    except (OSError, ValueError):
        print("Zamanlayıcı kaydı: henüz yok.")

    tz = ZoneInfo(cfg.site.timezone)
    catalog = catalog_mod.Catalog.load(catalog_mod.CATALOG_PATH)
    # Ekim 2026 duyurusu: predicted 66h/72h tennis moment, same cache remind.pyw uses.
    tennis_last_pick = read_tennis_history()
    if tennis_last_pick is not None:
        tennis_last_pick = tennis_last_pick.replace(tzinfo=tz)
    due = upcoming(settings, catalog, cfg.timing, tz, datetime.now(tz),
                   tennis_last_pick=tennis_last_pick)[:5]
    if not due:
        print("\nYaklaşan hatırlatma yok.")
        return 0

    print("\nSıradaki hatırlatmalar:")
    for r in due:
        print(f"  {_format_reminder_row(r)}")
    return 0


# --------------------------------------------------------------------------
# CLI plumbing
# --------------------------------------------------------------------------

def _add_global_options(parser: argparse.ArgumentParser, *, top_level: bool) -> None:
    """--config and -v/--verbose, added to both the top parser and every subparser so they
    work whether given before or after the subcommand (`bot.py --config X probe` and
    `bot.py probe --config X` both work).

    Only the top-level parser gets a real default; subparser copies default to SUPPRESS so
    an unset subparser flag doesn't clobber a value already parsed by the top-level parser
    (argparse's subparsers otherwise silently overwrite the parent's parsed value with the
    subparser's own default when the flag is given before the subcommand)."""
    default_config = DEFAULT_CONFIG if top_level else argparse.SUPPRESS
    default_verbose = False if top_level else argparse.SUPPRESS
    parser.add_argument(
        "--config",
        default=default_config,
        type=Path,
        help=f"config.toml yolu (varsayılan: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", default=default_verbose, help="ayrıntılı (DEBUG) günlük"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bot.py",
        description="online.spor.istanbul (İBB Spor İstanbul) için rezervasyon botu.",
    )
    _add_global_options(parser, top_level=True)

    sub = parser.add_subparsers(dest="command", required=True)

    # login
    p_login = sub.add_parser("login", help="tarayıcı açıp elle giriş yapmanızı sağlar, oturumu kaydeder")
    _add_global_options(p_login, top_level=False)
    p_login.set_defaults(func=cmd_login)

    # save-login
    p_save_login = sub.add_parser(
        "save-login", help="TC/pasaport no ve şifreyi işletim sistemi anahtarlığına kaydeder (otomatik giriş için)"
    )
    _add_global_options(p_save_login, top_level=False)
    p_save_login.add_argument(
        "--check", action="store_true", help="kaydettikten sonra tarayıcıda otomatik giriş dener"
    )
    p_save_login.set_defaults(func=cmd_save_login)

    # forget-login
    p_forget_login = sub.add_parser("forget-login", help="anahtarlığa kaydedilmiş kimlik bilgilerini siler")
    _add_global_options(p_forget_login, top_level=False)
    p_forget_login.set_defaults(func=cmd_forget_login)

    # probe
    p_probe = sub.add_parser(
        "probe", help="ızgarayı bir kez okuyup tablo halinde yazdırır; hiçbir şey seçmez"
    )
    _add_global_options(p_probe, top_level=False)
    p_probe.add_argument("--branch", help="branş adı (varsayılan: ilk etkinleştirilen hedefin branşı)")
    p_probe.add_argument("--facility", help="tesis adı (varsayılan: ilk etkinleştirilen hedefin tesisi)")
    p_probe.set_defaults(func=cmd_probe)

    # watch
    p_watch = sub.add_parser(
        "watch", help="durum değişikliklerini state/observations.jsonl'a kaydeder; hiçbir şey seçmez"
    )
    _add_global_options(p_watch, top_level=False)
    p_watch.add_argument(
        "--minutes", type=float, default=None, help="bu kadar dakika sonra dur (varsayılan: süresiz)"
    )
    p_watch.set_defaults(func=cmd_watch)

    # run
    p_run = sub.add_parser("run", help="sniper döngüsü: tercih açılınca seçip kaydeder, bir başarıdan sonra durur")
    _add_global_options(p_run, top_level=False)
    p_run.add_argument(
        "--dry-run",
        action="store_true",
        help="seçer ama Kaydet'e basmaz (config'teki safety.dry_run'ı override eder)",
    )
    p_run.add_argument("--once", action="store_true", help="tek kontrol yapıp çık")
    p_run.set_defaults(func=cmd_run)

    # scan
    p_scan = sub.add_parser("scan", help="branştaki TÜM tesislerin seanslarını özetler; hiçbir şey seçmez")
    _add_global_options(p_scan, top_level=False)
    p_scan.add_argument("--branch", help="branş adı (varsayılan: ilk etkinleştirilen hedefin branşı)")
    p_scan.set_defaults(func=cmd_scan)

    # reservations
    p_res = sub.add_parser("reservations", help="mevcut rezervasyonları listeler (Seanslarım > detay)")
    _add_global_options(p_res, top_level=False)
    p_res.set_defaults(func=cmd_reservations)

    # cancel
    p_cancel = sub.add_parser("cancel", help="bir rezervasyonu iptal eder (seanstan en geç 3 saat önce)")
    _add_global_options(p_cancel, top_level=False)
    p_cancel.add_argument("--date", required=True, help="seans tarihi, GG.AA.YYYY (ör. 25.09.2026)")
    p_cancel.add_argument("--start", required=True, help="başlangıç saati, SS:DD (ör. 16:00)")
    p_cancel.add_argument("--yes", action="store_true", help="onay sormadan iptal et")
    p_cancel.set_defaults(func=cmd_cancel)

    # catalog
    p_catalog = sub.add_parser(
        "catalog",
        help="kataloğu tara ve inşa et (şubeler/tesisler/seanslar)",
    )
    _add_global_options(p_catalog, top_level=False)
    p_catalog.add_argument(
        "--depth",
        default="grids",
        choices=["branches", "facilities", "grids"],
        help="tarama derinliği (varsayılan: grids)",
    )
    p_catalog.add_argument(
        "--branch",
        action="append",
        dest="branch",
        help="belirli şubeleri tara (boş bırakılırsa tümü) (tekrarlanabilir)",
    )
    p_catalog.set_defaults(func=cmd_catalog)

    # targets
    p_targets = sub.add_parser(
        "targets",
        help="settings.json'dan hedefleri yazdır",
    )
    _add_global_options(p_targets, top_level=False)
    p_targets.add_argument(
        "--json",
        action="store_true",
        help="JSON biçiminde ayarları yazdır",
    )
    p_targets.set_defaults(func=cmd_targets)

    # version
    p_version = sub.add_parser(
        "version",
        help="uygulama sürümünü yazdır",
    )
    _add_global_options(p_version, top_level=False)
    p_version.set_defaults(func=cmd_version)

    # diagnostics
    p_diagnostics = sub.add_parser(
        "diagnostics",
        help="redakte edilmiş bir tanı (diagnostics) ZIP paketi oluşturur",
    )
    _add_global_options(p_diagnostics, top_level=False)
    p_diagnostics.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="ZIP'in kaydedileceği klasör (varsayılan: Masaüstü)",
    )
    p_diagnostics.set_defaults(func=cmd_diagnostics)

    # gui
    p_gui = sub.add_parser(
        "gui",
        help="uygulamanın web arayüzünü başlat (app.pyw)",
    )
    _add_global_options(p_gui, top_level=False)
    p_gui.set_defaults(func=cmd_gui)

    # reminders
    p_reminders = sub.add_parser(
        "reminders",
        help="hatırlatma bildirimlerinin durumunu ve sıradaki hatırlatmaları gösterir",
    )
    _add_global_options(p_reminders, top_level=False)
    p_reminders.add_argument(
        "--off", action="store_true", help="hatırlatmaları kapatır ve zamanlayıcıdan kaldırır"
    )
    p_reminders.set_defaults(func=cmd_reminders)

    return parser


# Subcommands that drive a real Playwright browser (open the same Chromium profile
# app.pyw uses). These take the single-instance lock so this CLI and a running
# app.pyw can never touch state/profile/cookies.json at the same time.
_BROWSER_COMMANDS = frozenset(
    {"login", "probe", "watch", "run", "scan", "reservations", "cancel", "catalog"}
)


def _needs_single_instance_guard(args: argparse.Namespace) -> bool:
    # Browser commands always take the lock. save-login and forget-login also take it
    # (always, not just save-login --check) because they change the active account /
    # credentials that a running app depends on.
    if args.command in _BROWSER_COMMANDS:
        return True
    if args.command in ("save-login", "forget-login"):
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    # Reconfigure stdout/stderr to UTF-8 for Windows console (Turkish characters)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)

    lock = None
    if _needs_single_instance_guard(args):
        # A broken config is reported (exit 3) even while the app holds the lock.
        _load_config(args.config)
        from src.single_instance import SingleInstance

        lock = SingleInstance()
        if not lock.acquire():
            print("Uygulama açık; önce uygulamayı kapatın.", file=sys.stderr)
            return 4

    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print("\nİptal edildi.", file=sys.stderr)
        return 130
    except Exception as e:
        try:
            from src.browser import LoggedOut
            from src.settings import SettingsError
        except ImportError:
            LoggedOut = ()  # type: ignore[assignment]
            SettingsError = ()  # type: ignore[assignment]

        if LoggedOut and isinstance(e, LoggedOut):
            print(f"Oturum kapanmış: {e}\n`python bot.py login` ile tekrar giriş yapın.", file=sys.stderr)
            return 2
        if SettingsError and isinstance(e, SettingsError):
            print(f"Ayarlar hatası: {e}", file=sys.stderr)
            return 3
        print(f"error: {e}", file=sys.stderr)
        if hasattr(args, "verbose") and args.verbose:
            raise
        return 1
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    sys.exit(main())
