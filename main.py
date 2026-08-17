"""Point d'entrée de l'agent SEO.

Modes d'exécution :
- `python main.py`              → mode démon (boucle infinie avec schedule)
- `python main.py crawl`        → lance un crawl unique et quitte (utile pour test)
- `python main.py report`       → génère et envoie un rapport unique (utile pour test)
- `python main.py init`         → initialise la base SQLite et quitte
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime

import schedule

import config
from analyzer import generate_weekly_report
from crawler import run_daily_crawl
from database import init_db
from mailer import send_weekly_report


def setup_logging() -> None:
    """Configure les logs : console + fichier."""
    logging.basicConfig(
        level=config.LOG_LEVEL,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(config.LOG_PATH, encoding="utf-8"),
        ],
    )


log = logging.getLogger("agent")


# --- Jobs planifiés -----------------------------------------------------------


def daily_crawl_job() -> bool:
    """Job quotidien : crawle jusqu'à MAX_PAGES_PER_DAY pages, met à jour SQLite.

    Retourne True si le cycle a réellement visité des pages. Un cycle qui
    sélectionne des URLs mais n'en crawle aucune (site injoignable, robots.txt
    bloquant) est un ÉCHEC, pas un succès à 0 erreur : la CLI sort alors en
    code != 0 pour que le workflow GitHub Actions passe au rouge.
    """
    log.info(">>> Lancement du job quotidien de crawl")
    try:
        stats = run_daily_crawl()
    except Exception:
        log.exception("Crash du job de crawl — l'agent continue de tourner")
        return False

    if stats.urls_selected and not stats.pages_crawled:
        log.error(
            "Crawl en échec : %d URLs sélectionnées, aucune visitée",
            stats.urls_selected,
        )
        return False

    log.info(
        "Crawl OK : %d pages, %d erreurs, %d nouvelles URLs%s",
        stats.pages_crawled, stats.errors_found, stats.new_urls_discovered,
        " (budget temps épuisé)" if stats.time_budget_exhausted else "",
    )
    return True


def weekly_report_job() -> bool:
    """Job hebdomadaire : génère le rapport (HTML + graphiques) et l'envoie.

    Retourne True si l'email a bien été envoyé, False sinon. Permet à la CLI
    de sortir avec un code != 0 quand l'envoi échoue (= workflow GitHub Actions
    en rouge), au lieu de planter en silence.
    """
    log.info(
        ">>> Lancement du job hebdomadaire (%s %s)",
        config.WEEKLY_REPORT_DAY, datetime.now(),
    )
    try:
        html, charts = generate_weekly_report()
        ok = send_weekly_report(html, charts=charts)
        if ok:
            log.info("Rapport hebdomadaire envoyé avec succès")
        else:
            log.error("Échec de l'envoi du rapport")
        return ok
    except Exception:
        log.exception("Crash du job de rapport — l'agent continue de tourner")
        return False


# --- Mode démon ---------------------------------------------------------------


def run_daemon() -> None:
    """Lance la boucle infinie qui exécute crawl et rapport selon le planning."""
    log.info("=" * 60)
    log.info("Agent SEO idgarages.com — démarrage")
    log.info("Site cible       : %s", config.TARGET_SITE)
    log.info("Crawl quotidien  : tous les jours à %s", config.DAILY_CRAWL_TIME)
    log.info(
        "Rapport email    : %s à %s → %s",
        config.WEEKLY_REPORT_DAY, config.WEEKLY_REPORT_TIME, config.EMAIL_TO,
    )
    log.info("=" * 60)

    # Crawl : tous les jours à l'heure définie
    schedule.every().day.at(config.DAILY_CRAWL_TIME).do(daily_crawl_job)

    # Rapport : uniquement le jour spécifié à l'heure spécifiée
    day_method = getattr(schedule.every(), config.WEEKLY_REPORT_DAY, None)
    if day_method is None:
        log.error("WEEKLY_REPORT_DAY invalide : %s", config.WEEKLY_REPORT_DAY)
        sys.exit(1)
    day_method.at(config.WEEKLY_REPORT_TIME).do(weekly_report_job)

    # Boucle principale : on vérifie toutes les 30 secondes s'il y a un job à exécuter
    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            log.info("Arrêt demandé (Ctrl+C) — bye 👋")
            break
        except Exception:
            log.exception("Erreur dans la boucle principale — on continue")
            time.sleep(60)


# --- CLI ----------------------------------------------------------------------


def main() -> None:
    setup_logging()

    # Vérification de la config
    missing = config.validate()
    if missing:
        log.warning("Variables d'environnement manquantes : %s", ", ".join(missing))
        log.warning("L'agent peut tourner mais certaines fonctions seront désactivées.")

    init_db()

    arg = sys.argv[1] if len(sys.argv) > 1 else "daemon"

    if arg == "init":
        log.info("Base de données initialisée à %s", config.DATABASE_PATH)
    elif arg == "crawl":
        # Code de sortie != 0 si le cycle n'a rien pu visiter → workflow rouge,
        # au lieu d'un faux « tout va bien » propagé jusqu'au rapport.
        if not daily_crawl_job():
            sys.exit(1)
    elif arg == "report":
        # Code de sortie != 0 si l'envoi a échoué → workflow GitHub Actions rouge.
        if not weekly_report_job():
            sys.exit(1)
    elif arg == "daemon":
        run_daemon()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
