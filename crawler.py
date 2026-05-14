"""Crawler HTTP pour idgarages.com.

Comportement :
- Respecte robots.txt (fetché une fois par cycle)
- Limite à 1 requête/seconde par défaut (CRAWL_DELAY_SECONDS)
- Reste sur le même domaine
- Détecte : 4xx, 5xx, chaînes de redirection, liens cassés, timeouts
- Persiste tout dans SQLite, et alimente la file pour les jours suivants
- Robuste : un crash sur une page ne stoppe pas le cycle
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from config import (
    CRAWL_DELAY_SECONDS,
    MAX_PAGES_PER_DAY,
    REQUEST_TIMEOUT,
    TARGET_SITE,
    USER_AGENT,
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


@dataclass
class CrawlStats:
    pages_crawled: int = 0
    errors_found: int = 0
    new_urls_discovered: int = 0


# --- Helpers ------------------------------------------------------------------


def _same_domain(url: str, base: str = TARGET_SITE) -> bool:
    """True si url appartient au même domaine que base."""
    try:
        return urlparse(url).netloc == urlparse(base).netloc
    except Exception:
        return False


def _normalize_url(url: str) -> str:
    """Supprime fragments (#section) et trailing slashes pour éviter les doublons."""
    parsed = urlparse(url)
    cleaned = parsed._replace(fragment="")
    normalized = cleaned.geturl()
    # Retire trailing slash sauf si c'est juste la racine
    if normalized.endswith("/") and normalized.count("/") > 3:
        normalized = normalized.rstrip("/")
    return normalized


def _load_robots(base: str = TARGET_SITE) -> RobotFileParser:
    """Charge le robots.txt du site. Retourne un parser même en cas d'échec."""
    rp = RobotFileParser()
    rp.set_url(urljoin(base, "/robots.txt"))
    try:
        rp.read()
    except Exception as exc:
        log.warning("Impossible de lire robots.txt (%s) — on continue prudemment", exc)
    return rp


def _make_session() -> requests.Session:
    """Crée une session HTTP avec User-Agent identifiable."""
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


# --- Crawl unitaire d'une page ------------------------------------------------


def crawl_page(
    session: requests.Session,
    url: str,
    robots: RobotFileParser,
    stats: CrawlStats,
) -> list[str]:
    """Crawle une page et retourne la liste des nouveaux liens à enfiler.

    Toute exception est attrapée et journalisée : un crash ici ne stoppe jamais
    le cycle complet.
    """
    if not robots.can_fetch(USER_AGENT, url):
        log.info("robots.txt interdit %s — skip", url)
        update_url_status(url, status_code=-1, content_type="blocked_by_robots")
        return []

    try:
        response = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
    except requests.Timeout:
        log.warning("Timeout sur %s", url)
        log_error(url, "timeout", details=f"après {REQUEST_TIMEOUT}s")
        update_url_status(url, status_code=0)
        stats.errors_found += 1
        return []
    except requests.ConnectionError as exc:
        log.warning("Erreur de connexion sur %s : %s", url, exc)
        log_error(url, "connection_error", details=str(exc)[:500])
        update_url_status(url, status_code=0)
        stats.errors_found += 1
        return []
    except Exception as exc:  # pragma: no cover - filet de sécurité
        log.exception("Erreur inattendue sur %s", url)
        log_error(url, "unexpected_error", details=str(exc)[:500])
        stats.errors_found += 1
        return []

    status = response.status_code
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
    redirect_target = response.url if response.url != url else None

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
    elif redirect_target and len(response.history) >= 3:
        # Chaîne de redirections >= 3 = mauvais pour le SEO
        chain = " → ".join(r.url for r in response.history) + f" → {response.url}"
        log_error(url, "long_redirect_chain", status_code=status, details=chain[:1000])
        stats.errors_found += 1

    # Extraction des nouveaux liens (uniquement sur les pages HTML 2xx)
    new_links: list[str] = []
    if 200 <= status < 300 and "html" in content_type:
        try:
            soup = BeautifulSoup(response.text, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if not href or href.startswith(("mailto:", "tel:", "javascript:")):
                    continue
                absolute = _normalize_url(urljoin(url, href))
                if _same_domain(absolute):
                    new_links.append(absolute)
        except Exception as exc:
            log.warning("Erreur de parsing HTML sur %s : %s", url, exc)

    stats.pages_crawled += 1
    return new_links


# --- Cycle complet ------------------------------------------------------------


def run_daily_crawl() -> CrawlStats:
    """Lance un cycle de crawl quotidien (max MAX_PAGES_PER_DAY pages)."""
    log.info("=== Début du cycle de crawl quotidien ===")
    stats = CrawlStats()
    run_id = start_crawl_run()

    # Seed : si la base est vide, on injecte l'URL racine
    add_url_if_unknown(TARGET_SITE + "/")

    robots = _load_robots()
    session = _make_session()

    try:
        # On récupère la liste des URLs à crawler aujourd'hui.
        # La requête trie par "jamais crawlées d'abord", donc on couvre
        # progressivement tout le site sans recroiser les mêmes pages.
        to_crawl = get_urls_to_crawl(limit=MAX_PAGES_PER_DAY)

        if not to_crawl:
            log.info("Aucune URL à crawler (file vide)")
            end_crawl_run(run_id, 0, 0, status="success")
            return stats

        log.info("Cycle : %d pages à visiter", len(to_crawl))

        for i, url in enumerate(to_crawl, start=1):
            log.debug("[%d/%d] %s", i, len(to_crawl), url)

            new_links = crawl_page(session, url, robots, stats)

            # Ajout des nouvelles URLs découvertes (pour les prochains cycles)
            for link in new_links:
                if add_url_if_unknown(link):
                    stats.new_urls_discovered += 1

            # Politesse : on attend entre chaque requête
            time.sleep(CRAWL_DELAY_SECONDS)

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
    except Exception as exc:
        log.exception("Crash pendant le cycle de crawl")
        end_crawl_run(run_id, stats.pages_crawled, stats.errors_found, status="failed")
        raise

    return stats
