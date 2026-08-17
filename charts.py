"""Génération des graphiques PNG pour le rapport hebdomadaire.

Trois graphiques :
  1. Évolution des erreurs sur 7 jours (bar chart)
  2. Top sections du site par nb d'erreurs (horizontal bar)
  3. Répartition par type d'erreur (donut)

Chaque graphique est produit en PNG en mémoire (BytesIO) puis attaché au mail
en tant que pièce jointe inline via `Content-ID:` — le HTML les référence
avec `<img src="cid:...">`. Ce mécanisme est supporté par Gmail, Outlook,
Apple Mail, Thunderbird, etc.

matplotlib utilise le backend "Agg" (pas de display, parfait pour CI/serveur).
"""

from __future__ import annotations

import io
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import matplotlib

matplotlib.use("Agg")  # backend non-interactif (obligatoire en CI/headless)
import matplotlib.pyplot as plt  # noqa: E402

log = logging.getLogger(__name__)


# Palette cohérente et lisible (ColorBrewer / Tableau 10)
COLOR_PRIMARY = "#4e79a7"   # bleu (tendance)
COLOR_DANGER = "#e15759"    # rouge (sections en erreur)
DONUT_COLORS = [
    "#e15759", "#f28e2b", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f",
]

_BASE_STYLE = {
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.edgecolor": "#cccccc",
    "axes.labelcolor": "#333333",
    "axes.titlecolor": "#1a1a1a",
    "axes.titlesize": 14,
    "axes.titleweight": "bold",
    "xtick.color": "#666666",
    "ytick.color": "#666666",
    "font.family": "DejaVu Sans",  # toujours dispo avec matplotlib
    "font.size": 11,
}

# Jours de la semaine en français (d.weekday() : 0 = lundi … 6 = dimanche)
_DAY_NAMES_FR = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]

# Libellés humains pour chaque type d'erreur réellement produit par le crawler
# (voir crawler.crawl_page). Les types `client_error_<code>` sont dynamiques
# (code HTTP exact), on les gère séparément dans _humanize_error_type().
_ERROR_LABELS_FR: dict[str, str] = {
    "not_found_404":         "Page introuvable (404)",
    "server_error_5xx":      "Erreur serveur (5xx)",
    "forbidden_403":         "Accès refusé (403)",
    "long_redirect_chain":   "Redirections en chaîne",
    "timeout":               "Délai de réponse dépassé",
    "navigation_error":      "Erreur de navigation",
}


def _humanize_error_type(error_type: str) -> str:
    """Convertit un identifiant technique d'erreur en libellé français lisible.

    Gère aussi les types dynamiques `client_error_<code>` (ex. 410, 429).
    """
    if error_type in _ERROR_LABELS_FR:
        return _ERROR_LABELS_FR[error_type]
    if error_type.startswith("client_error_"):
        code = error_type.split("_")[-1]
        return f"Erreur client ({code})"
    # Fallback : on retourne tel quel pour ne jamais perdre d'info
    return error_type


def _fig_to_png(fig) -> bytes:
    """Sérialise une figure matplotlib en PNG (bytes) puis ferme la figure."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def chart_errors_per_day(errors_per_day: dict[str, int], days: int = 7) -> bytes:
    """Bar chart vertical : nb d'erreurs sur les `days` derniers jours."""
    plt.rcParams.update(_BASE_STYLE)

    # On reconstruit la séquence complète (y compris jours à 0).
    # En UTC, comme les timestamps stockés par SQLite : sinon, en heure de
    # Paris, le graphique affiche un jour futur vide et perd le plus ancien.
    today = datetime.now(timezone.utc).date()
    dates = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    # Libellés en français (Lun, Mar…) — on n'utilise pas la locale système
    # car elle dépend de l'environnement (CI Ubuntu = en_US par défaut)
    labels = [
        f"{_DAY_NAMES_FR[d.weekday()]}\n{d.strftime('%d/%m')}"
        for d in dates
    ]
    values = [errors_per_day.get(d.isoformat(), 0) for d in dates]

    fig, ax = plt.subplots(figsize=(8, 3.5))
    bars = ax.bar(
        labels, values,
        color=COLOR_PRIMARY,
        edgecolor="white",
        linewidth=1.5,
        width=0.7,
    )
    ax.set_title("Erreurs détectées par jour", loc="left", pad=14)
    ax.set_ylabel("Nombre d'erreurs")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    ax.set_axisbelow(True)

    # Étiquette de valeur au-dessus de chaque barre non nulle
    for bar, val in zip(bars, values):
        if val > 0:
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                val,
                str(val),
                ha="center", va="bottom",
                fontsize=10, color="#333", fontweight="bold",
            )

    # Si tout est à 0, on force quand même un peu de hauteur pour la lisibilité
    if max(values) == 0:
        ax.set_ylim(0, 1)
        ax.text(
            0.5, 0.5, "Aucune erreur détectée sur la semaine",
            transform=ax.transAxes,
            ha="center", va="center",
            fontsize=12, color="#59a14f", fontweight="bold",
        )

    return _fig_to_png(fig)


def _section_of(url: str) -> str:
    """Retourne le 1er segment du path : '/devis/', '/garages/', etc."""
    path = urlparse(url).path.strip("/")
    if not path:
        return "/ (racine)"
    return "/" + path.split("/")[0] + "/"


def chart_errors_by_section(errors: list[dict], top_n: int = 8) -> bytes | None:
    """Bar chart horizontal : top N sections du site par nb d'erreurs.

    Retourne None si aucune erreur (rien à afficher).
    """
    if not errors:
        return None

    plt.rcParams.update(_BASE_STYLE)

    counts = Counter(_section_of(e["url"]) for e in errors).most_common(top_n)
    if not counts:
        return None

    # Inversé pour que la plus grosse barre soit en haut
    sections = [s for s, _ in counts][::-1]
    values = [n for _, n in counts][::-1]

    fig_height = max(2.5, len(sections) * 0.5 + 1)
    fig, ax = plt.subplots(figsize=(8, fig_height))
    bars = ax.barh(
        sections, values,
        color=COLOR_DANGER,
        edgecolor="white",
        linewidth=1.5,
    )
    ax.set_title("Top sections du site avec le plus d'erreurs", loc="left", pad=14)
    ax.set_xlabel("Nombre d'erreurs")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="x", linestyle="--", alpha=0.4)
    ax.set_axisbelow(True)

    for bar, val in zip(bars, values):
        ax.text(
            val, bar.get_y() + bar.get_height() / 2,
            f" {val}",
            ha="left", va="center",
            fontsize=10, color="#333", fontweight="bold",
        )

    return _fig_to_png(fig)


def chart_errors_by_type(errors: list[dict]) -> bytes | None:
    """Donut chart : répartition par type d'erreur (404, 5xx, redirects, …).

    Retourne None si aucune erreur.
    """
    if not errors:
        return None

    plt.rcParams.update(_BASE_STYLE)

    counts = Counter(e["error_type"] for e in errors).most_common()
    if not counts:
        return None

    # Étiquettes en français lisible plutôt que les IDs techniques
    labels = [_humanize_error_type(t) for t, _ in counts]
    values = [n for _, n in counts]
    total = sum(values)

    # Légende latérale au lieu de labels autour du donut : évite les
    # chevauchements quand certaines parts sont très petites (< 5%).
    fig, ax = plt.subplots(figsize=(9, 4.5))
    wedges, _texts, autotexts = ax.pie(
        values,
        autopct=lambda pct: f"{pct:.0f}%" if pct >= 4 else "",
        colors=DONUT_COLORS[: len(values)],
        wedgeprops={"width": 0.45, "edgecolor": "white", "linewidth": 2},
        textprops={"fontsize": 10, "color": "#333"},
        pctdistance=0.78,
        startangle=90,
    )
    for at in autotexts:
        at.set_color("white")
        at.set_fontweight("bold")

    # Légende avec libellé + valeur absolue (ex: "Page introuvable (404) — 32")
    legend_labels = [f"{lab}  —  {val}" for lab, val in zip(labels, values)]
    ax.legend(
        wedges,
        legend_labels,
        loc="center left",
        bbox_to_anchor=(1.05, 0.5),
        frameon=False,
        fontsize=10,
    )

    ax.set_title(
        f"Répartition par type ({total} erreurs au total)",
        loc="left", pad=14,
    )
    ax.set(aspect="equal")

    return _fig_to_png(fig)


# --- Assembleur ---------------------------------------------------------------


def build_weekly_charts(
    errors: list[dict],
    errors_per_day: dict[str, int],
) -> tuple[str, dict[str, bytes]]:
    """Génère les 3 graphiques + le snippet HTML qui les référence.

    Retourne (html_snippet, {cid: png_bytes}). Si aucun graphique pertinent
    n'a pu être généré (semaine sans données), retourne ("", {}).
    """
    charts: dict[str, bytes] = {}

    # 1. Tendance — toujours générée (utile même à 0 pour montrer le monitoring)
    try:
        charts["chart_trend"] = chart_errors_per_day(errors_per_day)
    except Exception:
        log.exception("Erreur génération chart_trend")

    # 2. Sections — seulement s'il y a des erreurs
    try:
        png = chart_errors_by_section(errors)
        if png:
            charts["chart_sections"] = png
    except Exception:
        log.exception("Erreur génération chart_sections")

    # 3. Types — seulement s'il y a des erreurs
    try:
        png = chart_errors_by_type(errors)
        if png:
            charts["chart_types"] = png
    except Exception:
        log.exception("Erreur génération chart_types")

    if not charts:
        return ("", {})

    parts = ['<h2>📈 Vue d\'ensemble</h2>']

    if "chart_trend" in charts:
        parts.append(
            '<p style="margin:12px 0">'
            '<img src="cid:chart_trend" alt="Évolution des erreurs" '
            'style="max-width:100%; height:auto; border:1px solid #eee; border-radius:4px">'
            "</p>"
        )
    if "chart_sections" in charts:
        parts.append(
            '<p style="margin:12px 0">'
            '<img src="cid:chart_sections" alt="Erreurs par section" '
            'style="max-width:100%; height:auto; border:1px solid #eee; border-radius:4px">'
            "</p>"
        )
    if "chart_types" in charts:
        parts.append(
            '<p style="margin:12px 0">'
            '<img src="cid:chart_types" alt="Répartition par type d\'erreur" '
            'style="max-width:100%; height:auto; border:1px solid #eee; border-radius:4px">'
            "</p>"
        )

    return ("\n".join(parts), charts)
