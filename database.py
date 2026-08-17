"""Couche de persistance SQLite.

Le schéma comporte 3 tables :
- urls         : toutes les URLs connues + last_crawled_at (pour rotation)
- errors       : journal de toutes les erreurs détectées
- crawl_runs   : historique des cycles de crawl

La rotation des URLs fonctionne ainsi : à chaque cycle quotidien, on sélectionne
en priorité les URLs jamais crawlées, puis celles dont last_crawled_at est le
plus ancien. Une page crawlée hier ne sera donc pas re-crawlée aujourd'hui tant
qu'il reste d'autres pages à voir.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from config import DATABASE_PATH


# SQLite écrit CURRENT_TIMESTAMP en UTC, au format "YYYY-MM-DD HH:MM:SS".
# Les bornes temporelles doivent utiliser EXACTEMENT ce format, sinon la
# comparaison de chaînes part en vrille : isoformat() insère un "T" (0x54)
# là où SQLite met un espace (0x20), et toutes les lignes du jour-limite
# passent alors sous le seuil quelle que soit leur heure.
SQLITE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _cutoff(days: int) -> str:
    """Borne basse d'une fenêtre de N jours, au format et fuseau de SQLite."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return since.strftime(SQLITE_TIME_FORMAT)


SCHEMA = """
CREATE TABLE IF NOT EXISTS urls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT UNIQUE NOT NULL,
    first_seen      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_crawled    TIMESTAMP,
    last_status     INTEGER,
    last_content_type TEXT,
    redirect_target TEXT,
    found_on        TEXT,
    crawl_count     INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_urls_last_crawled ON urls(last_crawled);

CREATE TABLE IF NOT EXISTS errors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL,
    status_code     INTEGER,
    error_type      TEXT NOT NULL,
    found_on        TEXT,
    detected_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    details         TEXT
);

CREATE INDEX IF NOT EXISTS idx_errors_detected_at ON errors(detected_at);
CREATE INDEX IF NOT EXISTS idx_errors_type ON errors(error_type);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at        TIMESTAMP,
    pages_crawled   INTEGER DEFAULT 0,
    errors_found    INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'running'
);
"""


# Colonnes ajoutées après la mise en production. CREATE TABLE IF NOT EXISTS ne
# touche pas une table existante : il faut un ALTER TABLE explicite, sinon la
# base déjà présente sur la branche `data` reste au vieux schéma.
MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "urls": [("found_on", "ALTER TABLE urls ADD COLUMN found_on TEXT")],
}


def init_db(path: Path = DATABASE_PATH) -> None:
    """Crée les tables et applique les migrations manquantes. Idempotent."""
    with get_conn(path) as conn:
        conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, ddl in columns:
                if column not in existing:
                    conn.execute(ddl)


@contextmanager
def get_conn(path: Path = DATABASE_PATH) -> Iterator[sqlite3.Connection]:
    """Context manager qui ouvre/ferme proprement la connexion SQLite."""
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row  # accès par nom de colonne (row["url"])
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- URLs ---------------------------------------------------------------------


def add_url_if_unknown(url: str, found_on: str | None = None) -> bool:
    """Insère une URL si elle n'existe pas déjà. Retourne True si ajout réussi.

    `found_on` est la page où le lien a été découvert : c'est elle qu'il faudra
    corriger si l'URL se révèle cassée. On ne l'écrase pas sur une URL déjà
    connue — la première page qui pointe dessus fait référence.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO urls(url, found_on) VALUES (?, ?)",
            (url, found_on),
        )
        return cur.rowcount > 0


def update_url_status(
    url: str,
    status_code: int,
    content_type: str | None = None,
    redirect_target: str | None = None,
) -> None:
    """Met à jour le statut HTTP et la date du dernier crawl."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE urls
               SET last_crawled = CURRENT_TIMESTAMP,
                   last_status = ?,
                   last_content_type = ?,
                   redirect_target = ?,
                   crawl_count = crawl_count + 1
             WHERE url = ?
            """,
            (status_code, content_type, redirect_target, url),
        )


def get_urls_to_crawl(limit: int) -> list[dict]:
    """Sélectionne les URLs à crawler ce cycle.

    Priorité : jamais vues (last_crawled IS NULL) d'abord, puis les plus
    anciennes. Cela garantit qu'on ne recrawle pas les mêmes pages chaque jour.

    Retourne des dicts {url, found_on} : `found_on` suit l'URL jusqu'au
    journal d'erreurs, pour qu'un 404 indique la page à corriger.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT url, found_on FROM urls
             ORDER BY last_crawled IS NULL DESC,  -- NULL en premier
                      last_crawled ASC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def count_urls() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM urls").fetchone()[0]


# --- Erreurs ------------------------------------------------------------------


def log_error(
    url: str,
    error_type: str,
    status_code: int | None = None,
    found_on: str | None = None,
    details: str | None = None,
) -> None:
    """Enregistre une erreur détectée pendant le crawl."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO errors(url, status_code, error_type, found_on, details)
            VALUES (?, ?, ?, ?, ?)
            """,
            (url, status_code, error_type, found_on, details),
        )


def get_errors_since(days: int = 7) -> list[dict]:
    """Récupère toutes les erreurs des N derniers jours."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT url, status_code, error_type, found_on, detected_at, details
              FROM errors
             WHERE detected_at >= ?
             ORDER BY detected_at DESC
            """,
            (_cutoff(days),),
        ).fetchall()
        return [dict(row) for row in rows]


def get_errors_per_day(days: int = 7) -> dict[str, int]:
    """Retourne {YYYY-MM-DD: nombre_d_erreurs} sur les N derniers jours.

    Utilisé pour le graphique d'évolution dans le rapport hebdomadaire.
    Les jours sans erreur ne sont pas dans le dict — le caller doit
    boucler sur les N jours pour avoir les zéros. Les jours sont en UTC,
    comme les timestamps stockés.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT date(detected_at) AS day, COUNT(*) AS n
              FROM errors
             WHERE detected_at >= ?
             GROUP BY day
             ORDER BY day ASC
            """,
            (_cutoff(days),),
        ).fetchall()
        return {row["day"]: row["n"] for row in rows}


# --- Cycles de crawl ----------------------------------------------------------


def start_crawl_run() -> int:
    """Crée une ligne crawl_runs et retourne son ID."""
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO crawl_runs(status) VALUES ('running')")
        return cur.lastrowid or 0


def end_crawl_run(run_id: int, pages: int, errors: int, status: str = "success") -> None:
    """Clôture un cycle de crawl."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE crawl_runs
               SET ended_at = CURRENT_TIMESTAMP,
                   pages_crawled = ?,
                   errors_found = ?,
                   status = ?
             WHERE id = ?
            """,
            (pages, errors, status, run_id),
        )


def get_recent_runs(days: int = 7) -> list[dict]:
    """Retourne les cycles de crawl des N derniers jours."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, started_at, ended_at, pages_crawled, errors_found, status
              FROM crawl_runs
             WHERE started_at >= ?
             ORDER BY started_at DESC
            """,
            (_cutoff(days),),
        ).fetchall()
        return [dict(row) for row in rows]
