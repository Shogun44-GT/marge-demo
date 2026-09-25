"""Construit le prompt système à partir du schéma réel de la base."""
from datetime import date

REGLES = """Règles :
- Réponds uniquement par une requête SQL {dialecte}, sans commentaire ni explication.
- N'utilise que SELECT. Aucun INSERT, UPDATE, DELETE, DROP, ALTER, CREATE ou autre écriture.
- Une seule requête, terminée par un point-virgule.
- N'interroge que les tables listées ci-dessus (les jointures suggérées sont autorisées).
- Si la question porte sur un montant total, utilise SUM(montant).
- Résous les dates relatives (le mois dernier, cette année, récemment...) par rapport à la date de référence ci-dessus.
- Si la question est ambiguë, floue ou hors sujet (pas liée aux données), réponds exactement : IMPOSSIBLE"""

# Précisions pour les bases SQLite (la démo Marge tourne sur SQLite) : les
# fonctions de date PostgreSQL les plus naturelles pour un LLM y sont absentes.
CONSEILS_SQLITE = """- Dialecte SQLite : pour filtrer ou grouper par mois, utilise strftime('%Y-%m', colonne) (ex. strftime('%Y-%m', created_at) = '2026-09') ; pour l'année, strftime('%Y', colonne). N'utilise JAMAIS DATE_TRUNC, EXTRACT, TO_DATE, ::date ni INTERVAL."""


def construire_prompt_systeme(
    description_schema: str,
    suggestions_jointures: list | None = None,
    date_reference: str | None = None,
    dialecte: str = "postgresql",
) -> str:
    """Assemble le prompt système à partir du schéma introspecté.

    `dialecte` : nom du dialecte SQL du moteur connecté (`postgresql`,
    `sqlite`...) — la règle « réponds en SQL ... » et les conseils de
    fonctions de date s'adaptent. Défaut `postgresql` (comportement historique).
    """
    date_reference = date_reference or date.today().isoformat()
    regles = REGLES.format(dialecte=dialecte)
    if dialecte.lower() == "sqlite":
        regles += "\n" + CONSEILS_SQLITE
    parties = [
        "Tu es un assistant qui traduit une question en français en une requête SQL.",
        "",
        "Schéma réel de la base (tables, colonnes, types, exemples) :",
        "",
        description_schema,
    ]
    if suggestions_jointures:
        parties += [
            "",
            "Jointures plausibles :",
            *[f"- {s}" for s in suggestions_jointures],
        ]
    parties += [
        "",
        f"Date de référence (aujourd'hui) : {date_reference}",
        "",
        regles,
    ]
    return "\n".join(parties)
