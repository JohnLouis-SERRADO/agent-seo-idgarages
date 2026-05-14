"""Synthèse intelligente des erreurs via l'API Claude (Anthropic).

L'agent ne se contente pas de lister les erreurs : il les regroupe, identifie
les patterns (sections entières cassées, redirects en masse, etc.) et produit
des recommandations SEO priorisées.

Modèle : claude-opus-4-7 avec adaptive thinking (le modèle décide combien
réfléchir selon la complexité du rapport).
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse

import anthropic

from charts import build_weekly_charts
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, TARGET_SITE
from database import get_errors_per_day, get_errors_since, get_recent_runs

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Tu es un expert SEO technique senior. Ton rôle est d'analyser \
les erreurs détectées sur un site web pendant la semaine écoulée et de produire \
un rapport HTML synthétique, priorisé et exploitable pour l'équipe technique.

Règles strictes pour ton rapport :

1. **Format** : HTML pur, inline-styled (compatible Gmail). Pas de <html>/<body>, \
juste les balises de contenu (div, h2, h3, table, ul, p, strong, span).

2. **Structure obligatoire** :
   - <h2>📊 Résumé exécutif</h2> : 3-5 phrases max. Volume d'erreurs, tendance, \
gravité globale (faible/modérée/élevée/critique).
   - <h2>🚨 Top 3 priorités SEO</h2> : les 3 actions à mener en premier, classées \
par impact SEO. Pour chaque priorité : titre, description (1-2 phrases), \
URLs concernées (max 5 exemples), action recommandée.
   - <h2>🗂️ Erreurs par section du site</h2> : regroupe les URLs par section \
(ex: /devis/, /garages/, /blog/) avec compteurs par type d'erreur.
   - <h2>📋 Détail des erreurs</h2> : tableau HTML des erreurs les plus \
significatives (max 30 lignes), colonnes : URL, Type, Code, Date.
   - <h2>💡 Recommandations</h2> : conseils techniques généraux (ex: configurer \
des redirections 301, vérifier le sitemap, etc.).

3. **Ton** : factuel, professionnel, orienté action. Pas de blabla.

4. **Intelligence** : identifie les patterns. Si 50 URLs /blog/* retournent 404, \
dis-le explicitement et propose une cause probable (section migrée ? CMS cassé ?). \
Ne te contente pas de lister.

5. **Couleurs** : utilise des badges colorés inline pour les codes HTTP :
   - 404 : background:#ffd6d6; color:#a00
   - 5xx : background:#ffb3b3; color:#700
   - 3xx (chaînes) : background:#fff3b3; color:#806000
   - timeout/connection : background:#e0e0e0; color:#444
"""


def _build_user_prompt(errors: list[dict], runs: list[dict]) -> str:
    """Construit le prompt utilisateur avec les données brutes pré-agrégées."""
    if not errors:
        return (
            "Aucune erreur n'a été détectée cette semaine. "
            "Produis un rapport HTML bref confirmant que tout va bien, "
            f"en mentionnant que {sum(r['pages_crawled'] for r in runs)} pages "
            f"ont été crawlées sur {len(runs)} cycles."
        )

    # Agrégations utiles pour Claude (évite qu'il les recalcule lui-même)
    by_type = Counter(e["error_type"] for e in errors)
    by_status = Counter(e["status_code"] for e in errors if e["status_code"])

    # Regroupement par "section" : on prend le premier segment du path
    def section_of(url: str) -> str:
        path = urlparse(url).path.strip("/")
        if not path:
            return "/ (racine)"
        return "/" + path.split("/")[0] + "/"

    by_section = Counter(section_of(e["url"]) for e in errors)

    total_crawled = sum(r["pages_crawled"] for r in runs)
    total_runs = len(runs)

    # On échantillonne les erreurs pour ne pas dépasser le contexte
    sample = errors[:200]
    error_lines = []
    for e in sample:
        line = f"- [{e['error_type']}] {e['url']}"
        if e.get("status_code"):
            line += f" (HTTP {e['status_code']})"
        if e.get("details"):
            line += f" — {e['details'][:120]}"
        error_lines.append(line)

    return f"""Données de la semaine pour {TARGET_SITE} :

**Statistiques globales**
- Cycles de crawl : {total_runs}
- Pages crawlées au total : {total_crawled}
- Erreurs détectées : {len(errors)}

**Répartition par type d'erreur**
{chr(10).join(f"- {t}: {n}" for t, n in by_type.most_common())}

**Répartition par code HTTP**
{chr(10).join(f"- HTTP {s}: {n}" for s, n in by_status.most_common())}

**Répartition par section du site**
{chr(10).join(f"- {sec}: {n}" for sec, n in by_section.most_common(15))}

**Échantillon d'erreurs (max 200)**
{chr(10).join(error_lines)}

Génère maintenant le rapport HTML selon les règles du system prompt."""


def generate_weekly_report() -> tuple[str, dict[str, bytes]]:
    """Génère le rapport HTML hebdomadaire via Claude + graphiques inline.

    Retourne (html_complet, dict_charts) où :
    - html_complet : le HTML prêt à l'envoi (graphiques en haut, analyse Claude
      en dessous). Les graphiques sont référencés via <img src="cid:...">.
    - dict_charts : {content_id: png_bytes} à attacher au MIME.

    En cas d'erreur API Anthropic, le HTML utilise le rapport de secours mais
    les graphiques restent présents.
    """
    log.info("Génération du rapport hebdomadaire via Claude…")

    errors = get_errors_since(days=7)
    runs = get_recent_runs(days=7)
    errors_per_day = get_errors_per_day(days=7)

    # Les graphiques sont indépendants de l'API Claude : on les fait d'abord
    # pour qu'ils soient inclus même si Claude tombe.
    charts_html, charts_dict = build_weekly_charts(errors, errors_per_day)

    # --- Analyse Claude (peut échouer) ---
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY manquante — rapport de secours uniquement")
        return charts_html + "\n" + _fallback_report(errors, runs), charts_dict

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    user_prompt = _build_user_prompt(errors, runs)

    try:
        # Streaming + adaptive thinking + effort high (Opus 4.7).
        with client.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=8000,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        ) as stream:
            final_message = stream.get_final_message()

        html_parts = [
            block.text for block in final_message.content if block.type == "text"
        ]
        claude_html = "\n".join(html_parts).strip()

        log.info(
            "Rapport généré (%d tokens entrée, %d tokens sortie, %d graphiques)",
            final_message.usage.input_tokens,
            final_message.usage.output_tokens,
            len(charts_dict),
        )
        # Graphiques en tête, analyse Claude en dessous
        return charts_html + "\n" + claude_html, charts_dict

    except anthropic.APIError as exc:
        log.exception("Erreur API Anthropic : %s", exc)
        return charts_html + "\n" + _fallback_report(errors, runs), charts_dict
    except Exception:
        log.exception("Erreur inattendue lors de la génération du rapport")
        return charts_html + "\n" + _fallback_report(errors, runs), charts_dict


def _fallback_report(errors: list[dict], runs: list[dict]) -> str:
    """Rapport HTML minimal généré localement si Claude ne répond pas."""
    by_type = Counter(e["error_type"] for e in errors)
    total_crawled = sum(r["pages_crawled"] for r in runs)

    rows = "".join(
        f"<tr><td>{e['url'][:80]}</td><td>{e['error_type']}</td>"
        f"<td>{e.get('status_code', '-')}</td><td>{e['detected_at'][:16]}</td></tr>"
        for e in errors[:50]
    )

    type_summary = "".join(f"<li>{t} : <strong>{n}</strong></li>" for t, n in by_type.most_common())

    return f"""
    <h2>📊 Rapport SEO hebdomadaire (mode de secours)</h2>
    <p><em>L'API Claude n'a pas pu être contactée — rapport brut généré localement.</em></p>
    <p>Période : 7 derniers jours · Pages crawlées : <strong>{total_crawled}</strong>
       · Erreurs : <strong>{len(errors)}</strong></p>
    <h3>Répartition</h3>
    <ul>{type_summary}</ul>
    <h3>50 premières erreurs</h3>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
      <tr><th>URL</th><th>Type</th><th>Code</th><th>Détectée</th></tr>
      {rows}
    </table>
    <p style="color:#666;font-size:12px">Généré le {datetime.now():%Y-%m-%d %H:%M}</p>
    """
