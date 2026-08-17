"""Crawler basé sur Playwright (Chrome headless) pour idgarages.com.

Pourquoi Playwright et pas `requests` ?
Le site est derrière un CDN anti-bot agressif qui bloque toute requête
non-navigateur (même avec un User-Agent customisé). Playwright lance un vrai
Chromium, exécute le JavaScript, présente une empreinte réaliste, et a donc
beaucoup plus de chances de passer.

Comportement :
- robots.txt est récupéré **avec le même navigateur** que les pages. Le lire
  avec urllib se ferait bloquer par le CDN exactement comme le reste, et un
  403 sur robots.txt est interprété par RobotFileParser comme « tout est
  interdit » — ce qui transformait silencieusement le cycle en no-op.
- Un robots.txt illisible n'interdit rien : on log un warning et on continue.
  Seule une règle réellement lue et parsée peut bloquer une URL.
- 1 page à la fois, 1+ seconde entre chaque, comme un humain
- Détecte : 4xx, 5xx, chaînes de redirection trop longues, timeouts
- Anti-détection : `playwright-stealth` (API v1 ou v2), et on **prévient**
  quand elle n'a pas pu être appliquée au lieu d'échouer en silence.
- Budget temps : le cycle s'arrête proprement avant le timeout du runner et
  sauvegarde ce qu'il a trouvé ; la rotation reprend au cycle suivant.
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
    Response,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from config import (
    CRAWL_DELAY_SECONDS,
    MAX_CRAWL_MINUTES,
    MAX_PAGES_PER_DAY,
    MAX_REDIRECT_HOPS,
    REQUEST_TIMEOUT,
    RESPECT_ROBOTS_TXT,
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
    urls_selected: int = 0
    pages_crawled: int = 0
    errors_found: int = 0
    new_urls_discovered: int = 0
    skipped_by_robots: int = 0
    time_budget_exhausted: bool = False


# --- Anti-détection ------------------------------------------------------------


def _apply_stealth(page: Page) -> None:
    """Applique playwright-stealth, en supportant les API v1 et v2.

    v1 exposait `stealth_sync(page)`, v2 l'a remplacé par
    `Stealth().apply_stealth_sync(page)`. Un `except ImportError` muet faisait
    passer l'agent en clair sans que personne ne le remarque : ici on prévient.
    """
    try:  # v1.x
        from playwright_stealth import stealth_sync  # type: ignore

        stealth_sync(page)
        log.debug("playwright-stealth (API v1) appliqué")
        return
    except ImportError:
        pass
    except Exception as exc:
        log.warning("playwright-stealth v1 a échoué : %s", exc)
        return

    try:  # v2.x
        from playwright_stealth import Stealth  # type: ignore

        Stealth().apply_stealth_sync(page)
        log.debug("playwright-stealth (API v2) appliqué")
    except ImportError:
        log.warning(
            "playwright-stealth introuvable — le crawl tourne SANS anti-détection "
            "et risque d'être bloqué par le CDN. Installe-le : pip install playwright-stealth"
        )
    except Exception as exc:
        log.warning("Impossible d'appliquer playwright-stealth : %s", exc)


# --- robots.txt ----------------------------------------------------------------


@dataclass
class RobotsPolicy:
    """Décision robots.txt, en distinguant « interdit » de « illisible »."""

    parser: RobotFileParser | None = None
    status: str = "disabled"  # parsed | unreachable | disabled

    def allows(self, url: str) -> bool:
        # Pas de règles exploitables → on n'interdit rien. Un robots.txt qu'on
        # n'a pas réussi à lire n'est pas un robots.txt qui refuse.
        if self.parser is None:
            return True
        return self.parser.can_fetch(BROWSER_USER_AGENT, url)


def _load_robots(page: Page, base: str = TARGET_SITE) -> RobotsPolicy:
    """Récupère robots.txt via le navigateur (donc à travers le CDN)."""
    if not RESPECT_ROBOTS_TXT:
        log.warning(
            "RESPECT_ROBOTS_TXT=false → robots.txt ignoré. "
            "OK uniquement pour monitorer SON PROPRE site."
        )
        return RobotsPolicy(status="disabled")

    url = urljoin(base + "/", "robots.txt")
    try:
        response = page.goto(url, timeout=REQUEST_TIMEOUT * 1000, wait_until="domcontentloaded")
    except Exception as exc:
        log.warning("robots.txt injoignable (%s) — aucune restriction appliquée", exc)
        return RobotsPolicy(status="unreachable")

    if response is None or response.status >= 400:
        code = response.status if response else "pas de réponse"
        log.warning(
            "robots.txt inaccessible (HTTP %s) — aucune restriction appliquée. "
            "Si le CDN le bloque, c'est attendu : passe RESPECT_ROBOTS_TXT=false "
            "pour rendre ce choix explicite.",
            code,
        )
        return RobotsPolicy(status="unreachable")

    try:
        body = response.text()
    except Exception as exc:
        log.warning("robots.txt illisible (%s) — aucune restriction appliquée", exc)
        return RobotsPolicy(status="unreachable")

    parser = RobotFileParser()
    parser.parse(body.splitlines())
    log.info("robots.txt chargé (%d lignes)", len(body.splitlines()))
    return RobotsPolicy(parser=parser, status="parsed")


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


def _redirect_hops(response: Response) -> int:
    """Nombre de redirections ayant mené à cette réponse.

    Playwright chaîne les requêtes via `redirected_from` ; on remonte la chaîne.
    La borne à 20 est une sécurité contre une boucle de redirection.
    """
    hops = 0
    request = response.request.redirected_from
    while request is not None and hops < 20:
        hops += 1
        request = request.redirected_from
    return hops


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
    robots: RobotsPolicy,
    stats: CrawlStats,
    found_on: str | None = None,
) -> list[str]:
    """Visite une page avec Playwright, retourne les liens découverts.

    `found_on` est la page qui pointait vers cette URL : il est journalisé avec
    chaque erreur pour qu'on sache quel lien corriger.

    Toute exception est attrapée → le cycle continue.
    """
    if not robots.allows(url):
        log.info("robots.txt interdit %s — skip (mets RESPECT_ROBOTS_TXT=false pour ignorer)", url)
        update_url_status(url, status_code=-1, content_type="blocked_by_robots")
        stats.skipped_by_robots += 1
        return []

    try:
        response = page.goto(
            url,
            timeout=REQUEST_TIMEOUT * 1000,  # Playwright attend des ms
            wait_until="domcontentloaded",
        )
    except PlaywrightTimeout:
        log.warning("Timeout sur %s", url)
        log_error(url, "timeout", found_on=found_on, details=f"après {REQUEST_TIMEOUT}s")
        update_url_status(url, status_code=0)
        stats.errors_found += 1
        return []
    except Exception as exc:
        log.warning("Erreur de navigation sur %s : %s", url, exc)
        log_error(url, "navigation_error", found_on=found_on, details=str(exc)[:500])
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
        log_error(url, "server_error_5xx", status_code=status, found_on=found_on)
        stats.errors_found += 1
    elif status == 404:
        log_error(url, "not_found_404", status_code=status, found_on=found_on)
        stats.errors_found += 1
    elif status == 403:
        log_error(url, "forbidden_403", status_code=status, found_on=found_on)
        stats.errors_found += 1
    elif 400 <= status < 500:
        log_error(url, f"client_error_{status}", status_code=status, found_on=found_on)
        stats.errors_found += 1

    # Chaîne de redirection : indépendant du code final — une page qui répond
    # 200 après 4 sauts reste un problème SEO (budget de crawl, perte de jus).
    hops = _redirect_hops(response)
    if hops >= MAX_REDIRECT_HOPS:
        log_error(
            url,
            "long_redirect_chain",
            status_code=status,
            found_on=found_on,
            details=f"{hops} redirections jusqu'à {final_url[:200]}",
        )
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
    """Cycle de crawl quotidien (max MAX_PAGES_PER_DAY pages, MAX_CRAWL_MINUTES)."""
    log.info("=== Début du cycle de crawl quotidien (Playwright) ===")
    stats = CrawlStats()
    run_id = start_crawl_run()

    add_url_if_unknown(TARGET_SITE + "/")

    try:
        to_crawl = get_urls_to_crawl(limit=MAX_PAGES_PER_DAY)
        stats.urls_selected = len(to_crawl)

        if not to_crawl:
            log.info("Aucune URL à crawler (file vide)")
            end_crawl_run(run_id, 0, 0, status="success")
            return stats

        log.info(
            "Cycle : %d pages à visiter (budget %.0f min)", len(to_crawl), MAX_CRAWL_MINUTES
        )
        deadline = time.monotonic() + MAX_CRAWL_MINUTES * 60

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
            _apply_stealth(page)

            robots = _load_robots(page)

            try:
                for i, row in enumerate(to_crawl, start=1):
                    if time.monotonic() >= deadline:
                        stats.time_budget_exhausted = True
                        log.warning(
                            "Budget temps épuisé (%.0f min) — %d URLs reportées au "
                            "prochain cycle, la rotation reprendra là où on s'arrête.",
                            MAX_CRAWL_MINUTES,
                            len(to_crawl) - i + 1,
                        )
                        break

                    url = row["url"]
                    log.debug("[%d/%d] %s", i, len(to_crawl), url)

                    new_links = crawl_page(page, url, robots, stats, found_on=row.get("found_on"))

                    for link in new_links:
                        if add_url_if_unknown(link, found_on=url):
                            stats.new_urls_discovered += 1

                    time.sleep(CRAWL_DELAY_SECONDS)
            finally:
                browser.close()

        # Un cycle qui n'a rien vu ne doit surtout pas passer pour un succès :
        # sans ça, le rapport du vendredi annonce « 0 erreur, tout va bien »
        # alors qu'en réalité on n'a pas regardé une seule page.
        if stats.pages_crawled == 0:
            log.error(
                "Cycle en échec : %d URLs sélectionnées, 0 page crawlée "
                "(%d bloquées par robots.txt). Vérifie l'accès au site et robots.txt.",
                stats.urls_selected,
                stats.skipped_by_robots,
            )
            end_crawl_run(run_id, 0, stats.errors_found, status="failed")
            return stats

        end_crawl_run(
            run_id,
            stats.pages_crawled,
            stats.errors_found,
            status="partial" if stats.time_budget_exhausted else "success",
        )
        log.info(
            "=== Cycle terminé : %d pages crawlées, %d erreurs, %d nouvelles URLs "
            "(%d ignorées par robots.txt) ===",
            stats.pages_crawled,
            stats.errors_found,
            stats.new_urls_discovered,
            stats.skipped_by_robots,
        )
    except Exception:
        log.exception("Crash pendant le cycle de crawl")
        end_crawl_run(run_id, stats.pages_crawled, stats.errors_found, status="failed")
        raise

    return stats
