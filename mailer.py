"""Envoi du rapport hebdomadaire via SMTP (Gmail).

Pour utiliser Gmail :
1. Active la validation en 2 étapes sur ton compte Google
2. Génère un mot de passe d'application : https://myaccount.google.com/apppasswords
3. Renseigne SMTP_USER et SMTP_PASSWORD dans .env
"""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import (
    EMAIL_FROM,
    EMAIL_TO,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
    TARGET_SITE,
)

log = logging.getLogger(__name__)


def _wrap_html(body_html: str) -> str:
    """Enveloppe le rapport dans un document HTML complet avec styles communs."""
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif;
         color: #222; line-height: 1.5; max-width: 800px; margin: 0 auto; padding: 20px; }}
  h2 {{ color: #1a1a1a; border-bottom: 2px solid #eee; padding-bottom: 8px; margin-top: 28px; }}
  h3 {{ color: #333; margin-top: 20px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 13px; }}
  th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
  th {{ background: #f5f5f5; }}
  tr:nth-child(even) {{ background: #fafafa; }}
  code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 3px; font-size: 12px; }}
  .footer {{ color: #888; font-size: 12px; margin-top: 32px; border-top: 1px solid #eee; padding-top: 12px; }}
</style>
</head>
<body>
  <p style="color:#666;font-size:13px">
    Rapport SEO hebdomadaire pour <strong>{TARGET_SITE}</strong><br>
    Période : 7 derniers jours · Généré le {datetime.now():%d/%m/%Y à %Hh%M}
  </p>
  {body_html}
  <div class="footer">
    Agent SEO automatisé · Analyse propulsée par Claude (Anthropic)<br>
    Ce rapport est généré automatiquement chaque vendredi à 11h.
  </div>
</body>
</html>"""


def send_weekly_report(html_body: str) -> bool:
    """Envoie le rapport par email. Retourne True si succès, False sinon."""
    if not SMTP_USER or not SMTP_PASSWORD:
        log.error("Identifiants SMTP manquants — email non envoyé")
        return False

    if not EMAIL_TO:
        log.error("EMAIL_TO manquant — email non envoyé")
        return False

    today = datetime.now().strftime("%d/%m/%Y")
    subject = f"📊 Rapport SEO hebdomadaire idgarages.com — {today}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    # On envoie en HTML uniquement ; les clients mail modernes le gèrent.
    full_html = _wrap_html(html_body)
    msg.attach(MIMEText(full_html, "html", "utf-8"))

    try:
        log.info("Connexion à %s:%d en TLS…", SMTP_HOST, SMTP_PORT)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        log.info("Rapport envoyé avec succès à %s", EMAIL_TO)
        return True
    except smtplib.SMTPAuthenticationError:
        log.exception(
            "Échec d'authentification SMTP. "
            "Vérifie SMTP_USER/SMTP_PASSWORD (utilise un 'mot de passe d'application' Gmail)."
        )
        return False
    except Exception:
        log.exception("Erreur lors de l'envoi de l'email")
        return False
