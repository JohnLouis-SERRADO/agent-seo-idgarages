"""Crawler basé sur Playwright (Chrome headless) pour idgarages.com.

Pourquoi Playwright et pas `requests` ?
Le site est derrière un CDN anti-bot agressif qui bloque toute requête
non-navigateur (même avec un User-Agent customisé). Playwright lance un vrai
Chromium, exécute le JavaScript, présente une empreinte réaliste, et a donc
beaucoup plus de chances de passer.

Comportement :
- Respecte robots.txt (lu via requests classique, plus rapide que Playwright)
- 1 page à la fois, 1+ seconde entre chaque, comme un humain
- Détecte les mêmes erreurs que la version `requests` (4xx, 5xx, redirects,
  timeouts) en lisant `response.status` et `response.url`
- Anti-détection : `playwright-stealth` patche navigator.webdriver, plugins,
  langues, etc.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

try:
    # playwright-stealth v1.x (sync API)
    from playwright_stealth import stealth_sync as _stealth_apply  # type: ignore
except ImportError:  # pragma: no cover - fallback si module absent
    _stealth_apply = None  # type: ignore

from config import (
    CRAWL_DELAY_SECONDS,
    MAX_PAGES_PER_DAY,
    REQUEST_TIMEOUT,
    TARGET_SITE,
)
from database import (
    add_url_if_unknown,
    end_crawl_run,
    get_urls_to_crawl,
    log_error,
    start_crawl_run,
    update_url_status,
)

log = logging.getLogger(__name__)


# User-Agent d'un vrai Chrome récent sur Windows. À mettre à jour de temps
# en temps pour rester crédible auprès des détecteurs de bots.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


@dataclass
class CrawlStats:
    pages_crawled: int = 0
    errors_found: int = 0
    new_urls_discovered: int = 0


# --- Helpers ------------------------------------------------------------------


def _same_domain(url: str, base: str = TARGET_SITE) -> bool:
    try:
        return urlparse(url).netloc == urlparse(base).netloc
    except Exception:
        return False


def _normalize_url(url: str) -> str:
    """Supprime fragment et trailing slash pour éviter les doublons."""
    parsed = urlparse(url)
    cleaned = parsed._replace(fragment="")
    normalized = cleaned.geturl()
    if normalized.endswith("/") and normalized.count("/") > 3:
        normalized = normalized.rstrip("/")
    return normalized


def _load_robots(base: str = TARGET_SITE) -> RobotFileParser:
    """Charge robots.txt via urllib (rapide). En cas d'échec, on continue."""
    rp = RobotFileParser()
    rp.set_url(urljoin(base, "/robots.txt"))
    try:
        rp.read()
    except Exception as exc:
        log.warning("Impossible de lire robots.txt (%s) — on continue prudemment", exc)
    return rp


def _make_context(browser: Browser) -> BrowserContext:
    """Crée un BrowserContext avec une empreinte réaliste."""
    return browser.new_context(
        user_agent=BROWSER_USER_AGENT,
        locale="fr-FR",
        timezone_id="Europe/Paris",
        viewport={"width": 1920, "height": 1080},
        extra_http_headers={
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        },
    )


# --- Crawl unitaire -----------------------------------------------------------


def crawl_page(
    page: Page,
    url: str,
    robots: RobotFileParser,
    stats: CrawlStats,
) -> list[str]:
    """Visite une page avec Playwright, retourne les liens découverts.

    Toute exception est attrapée → le cycle continue.
    """
    if not robots.can_fetch(BROWSER_USER_AGENT, url):
        log.info("robots.txt interdit %s — skip", url)
        update_url_status(url, status_code=-1, content_type="blocked_by_robots")
        return []

    try:
        response = page.goto(
            url,
            timeout=REQUEST_TIMEOUT * 1000,  # Playwright attend des ms
            wait_until="domcontentloaded",
        )
    except PlaywrightTimeout:
        log.warning("Timeout sur %s", url)
        log_error(url, "timeout", details=f"après {REQUEST_TIMEOUT}s")
        update_url_status(url, status_code=0)
        stats.errors_found += 1
        return []
    except Exception as exc:
        log.warning("Erreur de navigation sur %s : %s", url, exc)
        log_error(url, "navigation_error", details=str(exc)[:500])
        update_url_status(url, status_code=0)
        stats.errors_found += 1
        return []

    if response is None:
        log.warning("Pas de réponse HTTP pour %s", url)
        update_url_status(url, status_code=0)
        return []

    status = response.status
    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    final_url = page.url
    redirect_target = final_url if final_url != url else None

    update_url_status(url, status, content_type, redirect_target)

    # Détection des erreurs HTTP
    if status >= 500:
        log_error(url, "server_error_5xx", status_code=status)
        stats.errors_found += 1
    elif status == 404:
        log_error(url, "not_found_404", status_code=status)
        stats.errors_found += 1
    elif status == 403:
        log_error(url, "forbidden_403", status_code=status)
        stats.errors_found += 1
    elif 400 <= status < 500:
        log_error(url, f"client_error_{status}", status_code=status)
        stats.errors_found += 1

    # Extraction des liens (uniquement HTML 2xx)
    new_links: list[str] = []
    if 200 <= status < 300 and "html" in content_type:
        try:
            html = page.content()
            soup = BeautifulSoup(html, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not href or href.startswith(("mailto:", "tel:", "javascript:")):
                    continue
                absolute = _normalize_url(urljoin(url, href))
                if _same_domain(absolute):
                    new_links.append(absolute)
        except Exception as exc:
            log.warning("Erreur d'extraction HTML sur %s : %s", url, exc)

    stats.pages_crawled += 1
    return new_links


# --- Cycle complet ------------------------------------------------------------


def run_daily_crawl() -> CrawlStats:
    """Cycle de crawl quotidien (max MAX_PAGES_PER_DAY pages)."""
    log.info("=== Début du cycle de crawl quotidien (Playwright) ===")
    stats = CrawlStats()
    run_id = start_crawl_run()

    add_url_if_unknown(TARGET_SITE + "/")
    robots = _load_robots()

    try:
        to_crawl = get_urls_to_crawl(limit=MAX_PAGES_PER_DAY)

        if not to_crawl:
            log.info("Aucune URL à crawler (file vide)")
            end_crawl_run(run_id, 0, 0, status="success")
            return stats

        log.info("Cycle : %d pages à visiter", len(to_crawl))

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    # Cache les marqueurs d'automatisation détectés par certains CDN
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = _make_context(browser)
            page = context.new_page()

            # Anti-détection supplémentaire (playwright-stealth)
            if _stealth_apply is not None:
                try:
                    _stealth_apply(page)
                    log.debug("playwright-stealth appliqué")
                except Exception as exc:
                    log.warning("Impossible d'appliquer stealth : %s", exc)

            try:
                for i, url in enumerate(to_crawl, start=1):
                    log.debug("[%d/%d] %s", i, len(to_crawl), url)

                    new_links = crawl_page(page, url, robots, stats)

                    for link in new_links:
                        if add_url_if_unknown(link):
                            stats.new_urls_discovered += 1

                    time.sleep(CRAWL_DELAY_SECONDS)
            finally:
                browser.close()

        end_crawl_run(
            run_id,
            stats.pages_crawled,
            stats.errors_found,
            status="success",
        )
        log.info(
            "=== Cycle terminé : %d pages crawlées, %d erreurs, %d nouvelles URLs ===",
            stats.pages_crawled,
            stats.errors_found,
            stats.new_urls_discovered,
        )
    except Exception:
        log.exception("Crash pendant le cycle de crawl")
        end_crawl_run(run_id, stats.pages_crawled, stats.errors_found, status="failed")
        raise

    return stats
