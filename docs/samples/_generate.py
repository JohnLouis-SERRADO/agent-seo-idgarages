"""Génère les 3 graphiques d'exemple avec des données réalistes.

Lancer depuis la racine du projet :
    python docs/samples/_generate.py

Les PNG sont écrits à côté de ce fichier (chart_trend.png, etc.).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

# Permet d'importer charts.py depuis la racine du projet
sys.path.insert(0, str(Path(__file__).parents[2]))

from charts import (
    chart_errors_by_section,
    chart_errors_by_type,
    chart_errors_per_day,
)


def realistic_sample_data() -> tuple[list[dict], dict[str, int]]:
    """Données factices mais réalistes pour une semaine typique de monitoring SEO.

    Inspiration idgarages.com : section /devis/ probablement la plus active, donc
    la plus d'erreurs en valeur absolue. Les 404 dominent (URLs supprimées), suivis
    des chaînes de redirection (migrations passées), puis des 5xx ponctuels.
    """
    today = datetime.now().date()

    # Distribution sur 7 jours : pic mercredi (= jour après un déploiement ?)
    daily_counts = {
        (today - timedelta(days=6)).isoformat(): 8,
        (today - timedelta(days=5)).isoformat(): 12,
        (today - timedelta(days=4)).isoformat(): 7,
        (today - timedelta(days=3)).isoformat(): 23,  # pic
        (today - timedelta(days=2)).isoformat(): 18,
        (today - timedelta(days=1)).isoformat(): 11,
        today.isoformat(): 6,
    }

    # Erreurs factices, distribuées dans les sections principales
    sections_distribution = {
        "/devis/":      32,
        "/garages/":    21,
        "/blog/":       12,
        "/pieces/":     8,
        "/entretien/":  5,
        "/aide/":       4,
        "/marques/":    2,
        "/":            1,
    }

    type_distribution = {
        "not_found_404":         48,  # le gros morceau
        "long_redirect_chain":   17,
        "server_error_5xx":      10,
        "timeout":                6,
        "forbidden_403":          4,
    }

    errors: list[dict] = []
    # On distribue les erreurs équitablement entre sections et types pour
    # alimenter les 3 graphiques (les graphiques recomptent depuis errors[]).
    err_id = 0
    for section, n in sections_distribution.items():
        for _ in range(n):
            # Pour la racine "/", on garde l'URL exactement = domaine racine,
            # sinon _section_of() retournerait "/page-X/" au lieu de "/ (racine)"
            url = (
                "https://www.idgarages.com/"
                if section == "/"
                else f"https://www.idgarages.com{section}page-{err_id}"
            )
            errors.append({
                "url": url,
                "error_type": "not_found_404",  # placeholder, écrasé plus bas
                "status_code": 404,
                "detected_at": today.isoformat(),
            })
            err_id += 1

    # Réécrit les error_type pour matcher la distribution
    type_targets = []
    for t, n in type_distribution.items():
        type_targets.extend([t] * n)

    # Tronque/complète pour que len(type_targets) == len(errors)
    while len(type_targets) < len(errors):
        type_targets.append("not_found_404")
    for i, t in enumerate(type_targets[: len(errors)]):
        errors[i]["error_type"] = t
        if t == "not_found_404":
            errors[i]["status_code"] = 404
        elif t == "server_error_5xx":
            errors[i]["status_code"] = 500
        elif t == "forbidden_403":
            errors[i]["status_code"] = 403
        elif t == "long_redirect_chain":
            errors[i]["status_code"] = 301
        else:
            errors[i]["status_code"] = 0

    return errors, daily_counts


def main() -> None:
    errors, daily = realistic_sample_data()
    out_dir = Path(__file__).parent

    samples = {
        "chart_trend.png":    chart_errors_per_day(daily),
        "chart_sections.png": chart_errors_by_section(errors),
        "chart_types.png":    chart_errors_by_type(errors),
    }

    for name, png_bytes in samples.items():
        if png_bytes is None:
            print(f"[skip] {name} (pas de données)")
            continue
        path = out_dir / name
        path.write_bytes(png_bytes)
        print(f"[ok]   {path} ({len(png_bytes):,} octets)")


if __name__ == "__main__":
    main()
