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
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse

from config import (
    EMAIL_FROM,
    EMAIL_TO,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
    TARGET_SITE,
    WEEKLY_REPORT_DAY,
    WEEKLY_REPORT_TIME,
)

log = logging.getLogger(__name__)

# Nom d'affichage du site surveillé, dérivé de TARGET_SITE : l'objet du mail
# doit suivre la config, pas rester figé sur idgarages.com.
SITE_LABEL = urlparse(TARGET_SITE).netloc or TARGET_SITE


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
    Planification configurée : {WEEKLY_REPORT_DAY} à {WEEKLY_REPORT_TIME}.
  </div>
</body>
</html>"""


def _build_message(
    subject: str,
    full_html: str,
    charts: dict[str, bytes] | None,
) -> MIMEMultipart:
    """Construit le MIMEMultipart selon qu'il y a des images inline ou pas.

    Structure quand `charts` est fourni (RFC 2387, supporté par tous les clients) :

        multipart/related
        ├── multipart/alternative
        │   └── text/html  ← référence les images via <img src="cid:XXX">
        ├── image/png  ← Content-ID: <chart_trend>
        ├── image/png  ← Content-ID: <chart_sections>
        └── image/png  ← Content-ID: <chart_types>

    Sans `charts`, on reste sur le simple multipart/alternative HTML-only.
    """
    if charts:
        msg = MIMEMultipart("related")
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO

        # Le body HTML va dans un sous-conteneur alternative (best practice)
        alt = MIMEMultipart("alternative")
        msg.attach(alt)
        alt.attach(MIMEText(full_html, "html", "utf-8"))

        # Chaque image avec son Content-ID (référencé par <img src="cid:...">)
        for cid, png_bytes in charts.items():
            img = MIMEImage(png_bytes, _subtype="png")
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
            msg.attach(img)
        return msg

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(full_html, "html", "utf-8"))
    return msg


def send_weekly_report(
    html_body: str,
    charts: dict[str, bytes] | None = None,
) -> bool:
    """Envoie le rapport par email avec graphiques inline optionnels.

    `charts` : dict {content_id: png_bytes}. Le HTML doit déjà contenir des
    balises `<img src="cid:content_id">` pour chaque entrée.
    Retourne True si succès, False sinon.
    """
    if not SMTP_USER or not SMTP_PASSWORD:
        log.error("Identifiants SMTP manquants — email non envoyé")
        return False

    if not EMAIL_TO:
        log.error("EMAIL_TO manquant — email non envoyé")
        return False

    today = datetime.now().strftime("%d/%m/%Y")
    subject = f"📊 Rapport SEO hebdomadaire {SITE_LABEL} — {today}"

    full_html = _wrap_html(html_body)
    msg = _build_message(subject, full_html, charts)

    try:
        log.info(
            "Connexion à %s:%d en TLS… (%d graphique%s inline)",
            SMTP_HOST, SMTP_PORT,
            len(charts) if charts else 0,
            "s" if charts and len(charts) > 1 else "",
        )
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
