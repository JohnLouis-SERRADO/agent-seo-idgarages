"""Charge la configuration depuis le fichier .env et expose des constantes typées."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Charge automatiquement le fichier .env à la racine du projet.
load_dotenv()

# Racine du projet (utilisé pour construire les chemins absolus).
ROOT_DIR = Path(__file__).parent.resolve()
DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# --- API Anthropic ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-opus-4-7"  # modèle le plus capable, idéal pour l'analyse SEO

# --- Cible du crawl ---
TARGET_SITE = os.getenv("TARGET_SITE", "https://www.idgarages.com").rstrip("/")
MAX_PAGES_PER_DAY = int(os.getenv("MAX_PAGES_PER_DAY", "500"))
CRAWL_DELAY_SECONDS = float(os.getenv("CRAWL_DELAY_SECONDS", "1.0"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
USER_AGENT = "IDGaragesSEOBot/1.0 (+monitoring interne)"

# --- SMTP ---
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USER)
EMAIL_TO = os.getenv("EMAIL_TO", "")

# --- Planning ---
DAILY_CRAWL_TIME = os.getenv("DAILY_CRAWL_TIME", "03:00")
WEEKLY_REPORT_DAY = os.getenv("WEEKLY_REPORT_DAY", "friday").lower()
WEEKLY_REPORT_TIME = os.getenv("WEEKLY_REPORT_TIME", "11:00")

# --- Fichiers ---
DATABASE_PATH = DATA_DIR / "seo_monitor.db"
LOG_PATH = DATA_DIR / "agent.log"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


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
