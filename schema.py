"""Introspection dynamique du schéma de la base.

Le prompt envoyé au LLM est construit à partir du schéma RÉEL (tables,
colonnes, types, exemples de valeurs, plages de dates), jamais d'un schéma
en dur. Supporte plusieurs tables et détecte les clés de jointure plausibles
(contraintes FK réelles + heuristique de nommage).
"""
from sqlalchemy import inspect, text

# Noms de colonnes trop génériques pour l'heuristique de nommage :
# `id` existe dans (presque) toutes les tables et ne prouve aucune jointure.
COLONNES_GENERIQUES = {"id", "uuid", "created_at", "updated_at", "deleted_at"}


def inspecter_schema(engine) -> dict:
    """Lit le schéma réel via l'inspecteur SQLAlchemy.

    Retourne {nom_table: {"colonnes": [{"nom", "type", "nullable"}],
                            "pk": [...], "fks": [...], "uniques": [...]}}.
    """
    insp = inspect(engine)
    schema = {}
    for table in sorted(insp.get_table_names()):
        colonnes = [
            {
                "nom": c["name"],
                "type": str(c["type"]),
                "nullable": c.get("nullable", True),
            }
            for c in insp.get_columns(table)
        ]
        schema[table] = {
            "colonnes": colonnes,
            "pk": insp.get_pk_constraint(table).get("constrained_columns", []) or [],
            "fks": insp.get_foreign_keys(table) or [],
            "uniques": [
                u.get("column_names", [])
                for u in (insp.get_unique_constraints(table) or [])
                if u.get("column_names")
            ],
        }
    return schema


def _exec_liste(engine, requete: str) -> list:
    try:
        with engine.connect() as conn:
            return [r[0] for r in conn.execute(text(requete)).fetchall()]
    except Exception:
        return []


def _exemples_colonne(engine, table: str, colonne: str, limite: int = 8) -> list:
    return _exec_liste(
        engine,
        f'SELECT DISTINCT "{colonne}" FROM "{table}" '
        f'WHERE "{colonne}" IS NOT NULL LIMIT {int(limite)}',
    )


def _plage_colonne(engine, table: str, colonne: str):
    try:
        with engine.connect() as conn:
            ligne = conn.execute(
                text(f'SELECT MIN("{colonne}"), MAX("{colonne}") FROM "{table}"')
            ).fetchone()
        if ligne and ligne[0] is not None:
            return ligne[0], ligne[1]
    except Exception:
        pass
    return None


def decrire_schema_textuel(schema: dict, engine=None, max_exemples: int = 8) -> str:
    """Description textuelle du schéma, injectée dans le prompt du LLM."""
    blocs = []
    for table, info in schema.items():
        lignes = [f"Table {table} :"]
        for c in info["colonnes"]:
            lignes.append(f"  - {c['nom']} ({c['type']})")
        if info["pk"]:
            lignes.append(f"  Clé primaire : {', '.join(info['pk'])}")
        for fk in info["fks"]:
            src = ", ".join(fk["constrained_columns"])
            dst = ", ".join(fk["referred_columns"])
            lignes.append(f"  Clé étrangère : {src} → {fk['referred_table']}.{dst}")
        if engine is not None:
            for c in info["colonnes"]:
                type_up = c["type"].upper()
                if any(k in type_up for k in ("CHAR", "TEXT")):
                    vals = _exemples_colonne(engine, table, c["nom"], max_exemples)
                    if vals:
                        lignes.append(
                            f"  Exemples de {c['nom']} : {', '.join(map(str, vals))}"
                        )
                elif "DATE" in type_up:
                    plage = _plage_colonne(engine, table, c["nom"])
                    if plage:
                        lignes.append(
                            f"  Plage de {c['nom']} : {plage[0]} → {plage[1]}"
                        )
        blocs.append("\n".join(lignes))
    return "\n\n".join(blocs)


def _colonne_jointure_preferee(info: dict, preferer_unique: bool = False) -> str | None:
    """Colonne de destination préférée pour une jointure.

    Par défaut : PK puis UNIQUE. Pour l'heuristique de nommage
    (`fournisseur` → `fournisseurs`), on préfère la clé naturelle UNIQUE
    (`nom`) à l'`id` technique.
    """
    uniques = [u[0] for u in info["uniques"] if u]
    if preferer_unique and uniques:
        return uniques[0]
    if info["pk"]:
        return info["pk"][0]
    if uniques:
        return uniques[0]
    return info["colonnes"][0]["nom"] if info["colonnes"] else None


def detecter_jointures(schema: dict) -> list[str]:
    """Suggestions de jointures plausibles, en texte.

    1. Contraintes de clés étrangères réelles (fiable).
    2. Heuristique de nommage : une colonne `fournisseur` dans `transactions`
       et une table `fournisseurs` suggèrent `transactions.fournisseur →
       fournisseurs.<clé>`. Les colonnes génériques (`id`, ...) sont ignorées
       pour éviter les faux positifs.
    """
    suggestions: list[str] = []

    def ajouter(s: str):
        if s not in suggestions:
            suggestions.append(s)

    # 1. Clés étrangères réelles
    colonnes_couvertes_par_fk: set[tuple[str, str]] = set()
    for table, info in schema.items():
        for fk in info["fks"]:
            for src, dst in zip(fk["constrained_columns"], fk["referred_columns"]):
                ajouter(f"{table}.{src} → {fk['referred_table']}.{dst} (clé étrangère)")
                colonnes_couvertes_par_fk.add((table.lower(), src.lower()))

    # 2. Heuristique de nommage (seulement si aucune FK réelle ne couvre
    # déjà la colonne source, pour éviter les doublons contradictoires)
    for table, info in schema.items():
        singulier = table[:-1] if table.endswith("s") and len(table) > 1 else table
        cible = _colonne_jointure_preferee(info, preferer_unique=True)
        if not cible:
            continue
        for autre, info_autre in schema.items():
            if autre == table:
                continue
            for c in info_autre["colonnes"]:
                nom = c["nom"].lower()
                if nom in COLONNES_GENERIQUES:
                    continue
                if (autre.lower(), nom) in colonnes_couvertes_par_fk:
                    continue
                if nom == table.lower() or nom == singulier.lower():
                    ajouter(
                        f"{autre}.{c['nom']} → {table}.{cible} (heuristique de nommage)"
                    )
    return suggestions
