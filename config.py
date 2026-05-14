"""Charge la configuration depuis le fichier .env et expose des constantes typées."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Charge automatiquement le fichier .env à la racine du projet.
load_dotenv()


def _env(name: str, default: str) -> str:
    """Comme os.getenv mais traite la chaîne vide comme absence.

    Indispensable pour GitHub Actions : si une variable n'est pas définie,
    GitHub substitue "" (au lieu de ne pas définir la variable), ce qui
    casse os.getenv(name, default) — la clé existe mais vaut "".
    """
    value = os.getenv(name)
    return value if value not in (None, "") else default


# Racine du projet (utilisé pour construire les chemins absolus).
ROOT_DIR = Path(__file__).parent.resolve()
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# --- API Anthropic ---
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-opus-4-7"  # modèle le plus capable, idéal pour l'analyse SEO

# --- Cible du crawl ---
TARGET_SITE = _env("TARGET_SITE", "https://www.idgarages.com").rstrip("/")
MAX_PAGES_PER_DAY = int(_env("MAX_PAGES_PER_DAY", "500"))
CRAWL_DELAY_SECONDS = float(_env("CRAWL_DELAY_SECONDS", "1.0"))
REQUEST_TIMEOUT = int(_env("REQUEST_TIMEOUT", "15"))
USER_AGENT = "IDGaragesSEOBot/1.0 (+monitoring interne)"

# Respecter le robots.txt du site cible ?
# - True (défaut) : on s'arrête sur les pages interdites par robots.txt
# - False : on ignore robots.txt (utile pour monitorer SON PROPRE site dont
#   le robots.txt interdit globalement les crawlers non-référencés)
RESPECT_ROBOTS_TXT = _env("RESPECT_ROBOTS_TXT", "true").lower() in ("true", "1", "yes")

# --- SMTP ---
SMTP_HOST = _env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(_env("SMTP_PORT", "587"))
SMTP_USER = _env("SMTP_USER", "")
SMTP_PASSWORD = _env("SMTP_PASSWORD", "")
EMAIL_FROM = _env("EMAIL_FROM", SMTP_USER)
EMAIL_TO = _env("EMAIL_TO", "")

# --- Planning ---
DAILY_CRAWL_TIME = _env("DAILY_CRAWL_TIME", "03:00")
WEEKLY_REPORT_DAY = _env("WEEKLY_REPORT_DAY", "friday").lower()
WEEKLY_REPORT_TIME = _env("WEEKLY_REPORT_TIME", "11:00")

# --- Fichiers ---
DATABASE_PATH = DATA_DIR / "seo_monitor.db"
LOG_PATH = DATA_DIR / "agent.log"
LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()


def validate() -> list[str]:
    """Retourne la liste des variables manquantes (vide si tout est OK)."""
    missing = []
    if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY.startswith("sk-ant-api03-xxx"):
        missing.append("ANTHROPIC_API_KEY")
    if not SMTP_USER:
        missing.append("SMTP_USER")
    if not SMTP_PASSWORD:
        missing.append("SMTP_PASSWORD")
    if not EMAIL_TO:
        missing.append("EMAIL_TO")
    return missing
