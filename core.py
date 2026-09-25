"""Génération SQL via LLM, validation AST et exécution en lecture seule.

La validation (`valider_sql`) ne repose plus sur une simple blacklist de
mots-clés : la requête est analysée avec sqlparse et vérifiée contre le
schéma réel de la base (tables et colonnes connues, une seule instruction,
SELECT uniquement). Les littéraux de type chaîne ne déclenchent plus de
faux positifs (ex. WHERE libelle = 'delete me').
"""
import os
import re
from datetime import date, timedelta

import pandas as pd
import sqlparse
from sqlparse.sql import (
    Function,
    Identifier,
    IdentifierList,
    Parenthesis,
    Where,
)
from sqlparse.tokens import DDL, DML, Keyword, Name, Number, Punctuation, String, Wildcard

from db import get_engine
from prompt_builder import construire_prompt_systeme
from schema import (
    decrire_schema_textuel,
    detecter_jointures,
    inspecter_schema,
)

# ---------------------------------------------------------------------------
# Schéma (cache process)
# ---------------------------------------------------------------------------

_schema_cache = None


def get_schema() -> dict:
    """Schéma réel de la base, introspecté une seule fois par process."""
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = inspecter_schema(get_engine())
    return _schema_cache


def _colonnes_connues(schema: dict) -> dict:
    return {
        table.lower(): {c["nom"].lower() for c in info["colonnes"]}
        for table, info in schema.items()
    }


def _nettoyer_nom(nom: str | None) -> str:
    if not nom:
        return ""
    return nom.strip().strip('"').strip("'").strip("`").lower()


# ---------------------------------------------------------------------------
# Validation : mots-clés dangereux (analyse AST, pas de regex sur le texte)
# ---------------------------------------------------------------------------

# DML/DDL sont déjà couverts par les types de tokens ; ces mots-clés
# supplémentaires n'ont rien à faire dans un SELECT en lecture seule.
MOTS_CLES_INTERDITS = {
    "COPY",
    "CALL",
    "MERGE",
    "EXEC",
    "EXECUTE",
    "INTO",  # bloque SELECT ... INTO (création de table)
    "REPLACE",
    "VACUUM",
    "GRANT",
    "REVOKE",
}

# Parties de date utilisées dans EXTRACT(...) / DATE_TRUNC(...) : ce sont des
# mots-clés, pas des colonnes. Sans cette liste, EXTRACT(MONTH FROM date)
# lèverait un faux positif "colonne inconnue : MONTH".
PARTIES_DE_DATE = {
    "microseconds",
    "milliseconds",
    "second",
    "minute",
    "hour",
    "day",
    "dow",
    "isodow",
    "doy",
    "week",
    "month",
    "quarter",
    "year",
    "isoyear",
    "decade",
    "century",
    "millennium",
    "epoch",
    "timezone",
}


def _verifier_mots_cles(tlist, erreurs: list) -> None:
    """Refuse toute instruction d'écriture / DDL, où qu'elle soit nichée."""
    for tok in tlist.tokens:
        if tok.is_whitespace:
            continue
        if tok.ttype in Punctuation and tok.value == ";":
            # sqlparse peut regrouper "SELECT 1; -- ..." en une seule
            # instruction : le ";" trahit quand même la tentative.
            erreurs.append("Plusieurs instructions détectées")
        elif tok.ttype in DML:
            if tok.normalized != "SELECT":
                erreurs.append(f"Instruction interdite : {tok.normalized}")
        elif tok.ttype in DDL:
            erreurs.append(f"Instruction interdite : {tok.normalized}")
        elif tok.ttype in Keyword and tok.normalized in MOTS_CLES_INTERDITS:
            erreurs.append(f"Mot-clé interdit : {tok.normalized}")
        if tok.is_group:
            _verifier_mots_cles(tok, erreurs)


# ---------------------------------------------------------------------------
# Validation : tables et colonnes contre le schéma réel
# ---------------------------------------------------------------------------


# Fonctions à effet de bord ou d'exfiltration, même dans un SELECT.
FONCTIONS_INTERDITES = {
    "pg_sleep",
    "dblink",
    "dblink_exec",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
}


def _contient_select(tok) -> bool:
    return any(
        s.ttype in Keyword and s.normalized == "SELECT"
        for s in tok.tokens
        if not s.is_whitespace
    )


def _nom_fonction(tok) -> str:
    for t in tok.tokens:
        if t.is_whitespace:
            continue
        if t.ttype in Name:
            return t.value.lower()
        if isinstance(t, Identifier):
            # sqlparse encapsule parfois le nom : Function(Identifier('pg_sleep'), ...)
            return (t.get_real_name() or "").lower()
        break
    return ""


def _extraire_colonnes(tokens, erreurs: list | None = None) -> list[tuple[str | None, str]]:
    """Références de colonnes [(qualifiant, nom)] dans une liste de tokens.

    Ignore : littéraux (String, Number), wildcards, noms de fonctions,
    alias (après AS), parties de date (MONTH, YEAR...).
    Les parenthèses contenant un SELECT (sous-requêtes) sont ignorées ici :
    elles sont validées séparément par _traiter_niveau.
    """
    refs: list[tuple[str | None, str]] = []

    def _walk(toks) -> None:
        toks = [t for t in toks if not t.is_whitespace]
        i = 0
        while i < len(toks):
            tok = toks[i]
            suivant = toks[i + 1] if i + 1 < len(toks) else None
            if tok.ttype in (String, Number) or tok.ttype in Wildcard:
                i += 1
                continue
            if isinstance(tok, Function):
                nom_f = _nom_fonction(tok)
                if nom_f in FONCTIONS_INTERDITES and erreurs is not None:
                    erreurs.append(f"Fonction interdite : {nom_f}")
                enfants = [t for t in tok.tokens if not t.is_whitespace]
                _walk(enfants[1:])  # nom de fonction ignoré, arguments analysés
                i += 1
                continue
            if isinstance(tok, Identifier):
                alias = (tok.get_alias() or "").lower()
                enfants = [
                    t
                    for t in tok.tokens
                    if not t.is_whitespace
                    and not (t.ttype in Keyword and t.normalized == "AS")
                    and not (t.ttype in Name and alias and t.value.lower() == alias)
                    # sqlparse imbrique parfois l'alias : `li.title AS produit`
                    # -> Identifier(..., Identifier('produit')). Sans ce filtre,
                    #  la récursion extrait des références parasites ('li' nu).
                    and not (
                        isinstance(t, Identifier)
                        and alias
                        and (t.get_real_name() or "").lower() == alias
                    )
                ]
                simple = all(
                    t.ttype in Name
                    or t.ttype in Wildcard
                    or (t.ttype in Punctuation and t.value == ".")
                    for t in enfants
                )
                if simple:
                    reel = tok.get_real_name()
                    if reel and reel != "*":
                        refs.append((tok.get_parent_name(), reel))
                else:
                    # Récursion sur la liste complète : le lookahead
                    # (ex. SUM suivi de sa parenthèse) est préservé.
                    _walk(enfants)
                i += 1
                continue
            if isinstance(tok, Parenthesis):
                if not _contient_select(tok):
                    _walk(tok.tokens)
                i += 1
                continue
            if tok.is_group:  # Case, Operation, Comparison...
                _walk(tok.tokens)
                i += 1
                continue
            if tok.ttype in Name:
                if isinstance(suivant, Parenthesis):
                    if tok.value.lower() in FONCTIONS_INTERDITES and erreurs is not None:
                        erreurs.append(f"Fonction interdite : {tok.value.lower()}")
                    _walk(suivant.tokens)  # fonction non groupée par sqlparse
                    i += 2
                    continue
                if tok.value.lower() not in PARTIES_DE_DATE:
                    refs.append((None, tok.value))
                i += 1
                continue
            i += 1

    _walk(tokens)
    return refs


def _colonnes_de(alias: str, table: str | None, ctx: dict) -> list[str]:
    if table is None:
        return sorted(ctx["derivees"].get(alias, set()))
    return sorted(ctx["connues"].get(table, set()))


def _sorties_identifier(ident: Identifier, tables: dict, ctx: dict) -> list[str]:
    alias = ident.get_alias()
    if alias:
        return [alias.lower()]
    reel = ident.get_real_name()
    if reel == "*":
        qualif = ident.get_parent_name()
        if qualif:
            q = _nettoyer_nom(qualif)
            return _colonnes_de(q, tables.get(q), ctx)
        cols = []
        for a, t in tables.items():
            cols.extend(_colonnes_de(a, t, ctx))
        return cols
    return [reel.lower()] if reel else []


def _sorties_select(select_toks, tables: dict, ctx: dict) -> list[str]:
    sorties: list[str] = []
    for tok in select_toks:
        if tok.is_whitespace:
            continue
        if tok.ttype in Wildcard:
            for a, t in tables.items():
                sorties.extend(_colonnes_de(a, t, ctx))
        elif isinstance(tok, IdentifierList):
            for ident in tok.get_identifiers():
                sorties.extend(_sorties_identifier(ident, tables, ctx))
        elif isinstance(tok, Identifier):
            sorties.extend(_sorties_identifier(tok, tables, ctx))
        elif tok.ttype in Name:
            sorties.append(tok.value.lower())
    return sorties


def _ajouter_table(ident: Identifier, ctx: dict, tables: dict, conditions: list) -> None:
    """Enregistre une référence de table du FROM (ou une table dérivée)."""
    # Table dérivée : (sous-requête) AS alias
    parens = None
    for t in ident.tokens:
        if isinstance(t, Parenthesis) and any(
            s.ttype in Keyword and s.normalized == "SELECT"
            for s in t.tokens
            if not s.is_whitespace
        ):
            parens = t
            break
    alias = _nettoyer_nom(ident.get_alias())
    if parens is not None:
        if not alias:
            ctx["erreurs"].append("Sous-requête du FROM sans alias")
            return
        _, sorties = _traiter_niveau(parens, ctx)
        tables[alias] = None
        ctx["derivees"][alias] = set(sorties)
        return
    table = _nettoyer_nom(ident.get_real_name())
    if not table:
        conditions.append(ident)  # ex. condition de JOIN mal découpée
        return
    if table not in ctx["connues"] and table not in ctx["derivees"]:
        ctx["erreurs"].append(f"Table inconnue : {ident.get_real_name()}")
        return
    tables[alias or table] = table if table in ctx["connues"] else None


def _traiter_from(ftoks, ctx: dict, tables: dict) -> None:
    conditions: list = []
    for tok in ftoks:
        if tok.is_whitespace:
            continue
        if tok.ttype in Punctuation and tok.value == ",":
            continue
        if isinstance(tok, IdentifierList):
            for ident in tok.get_identifiers():
                _ajouter_table(ident, ctx, tables, conditions)
        elif isinstance(tok, Identifier):
            _ajouter_table(tok, ctx, tables, conditions)
        elif isinstance(tok, Parenthesis):
            # (sous-requête) sans alias explicite -> erreur signalée via _ajouter_table
            _ajouter_table(tok, ctx, tables, conditions)
        else:
            conditions.append(tok)  # JOIN ... ON ... : conditions à vérifier
    for tok in conditions:
        for qualif, nom in _extraire_colonnes([tok], ctx["erreurs"]):
            _valider_ref(qualif, nom, tables, set(), ctx)


def _valider_ref(qualif, nom, tables: dict, alias_select: set, ctx: dict) -> None:
    n = _nettoyer_nom(nom)
    if not n:
        return
    if qualif:
        q = _nettoyer_nom(qualif)
        if q in tables:
            table = tables[q]
            autorisees = (
                ctx["derivees"].get(q, set())
                if table is None
                else ctx["connues"].get(table, set())
            )
            if n not in autorisees:
                ctx["erreurs"].append(f"Colonne inconnue : {qualif}.{nom}")
        elif q in ctx["derivees"]:
            if n not in ctx["derivees"][q]:
                ctx["erreurs"].append(f"Colonne inconnue : {qualif}.{nom}")
        else:
            ctx["erreurs"].append(f"Table ou alias inconnu : {qualif}")
        return
    if n in alias_select:
        return
    autorisees: set[str] = set()
    for a, t in tables.items():
        autorisees |= set(_colonnes_de(a, t, ctx))
    for cols in ctx["derivees"].values():
        autorisees |= set(cols)
    if n not in autorisees:
        ctx["erreurs"].append(f"Colonne inconnue : {nom}")


def _traiter_ctes(toks, ctx: dict) -> int:
    """Traite les définitions WITH ... ; retourne l'index de fin."""
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok.ttype in Keyword and tok.normalized == "SELECT":
            break
        if isinstance(tok, IdentifierList):
            idents = list(tok.get_identifiers())
        elif isinstance(tok, Identifier):
            idents = [tok]
        else:
            i += 1
            continue
        for ident in idents:
            nom = _nettoyer_nom(ident.get_real_name())
            parens = next(
                (
                    t
                    for t in ident.tokens
                    if isinstance(t, Parenthesis)
                ),
                None,
            )
            if nom and parens is not None:
                _, sorties = _traiter_niveau(parens, ctx)
                ctx["derivees"][nom] = set(sorties)
        i += 1
    return i


def _decouper_clauses(toks) -> dict:
    clauses: dict[str, list] = {}
    courant: str | None = None
    noms = {
        "SELECT": "select",
        "FROM": "from",
        "WHERE": "where",
        "GROUP BY": "group",
        "ORDER BY": "order",
        "HAVING": "having",
        "LIMIT": "limit",
        "OFFSET": "offset",
    }
    for tok in toks:
        if tok.ttype in Keyword and tok.normalized in noms:
            courant = noms[tok.normalized]
            clauses.setdefault(courant, [])
            continue
        if courant is None:
            continue
        clauses[courant].append(tok)
    return clauses


def _sous_requetes(toks):
    for tok in toks:
        if isinstance(tok, Parenthesis) and any(
            s.ttype in Keyword and s.normalized == "SELECT"
            for s in tok.tokens
            if not s.is_whitespace
        ):
            yield tok
        elif tok.is_group and not isinstance(tok, Identifier):
            yield from _sous_requetes(tok.tokens)


def _aplatir_where(toks: list) -> list:
    """sqlparse regroupe `WHERE ...` en objet Where : on l'éclate pour que
    le découpeur de clauses voie le mot-clé WHERE."""
    plats: list = []
    for t in toks:
        if isinstance(t, Where):
            plats.extend([x for x in t.tokens if not x.is_whitespace])
        else:
            plats.append(t)
    return plats


def _traiter_niveau(tlist, ctx: dict) -> tuple[dict, list[str]]:
    """Valide un niveau SELECT (requête ou sous-requête).

    Retourne (tables_du_niveau {alias: table|None}, colonnes_de_sortie).
    """
    toks = _aplatir_where([t for t in tlist.tokens if not t.is_whitespace])
    if toks and toks[0].ttype in Keyword and toks[0].normalized == "WITH":
        fin_cte = _traiter_ctes(toks[1:], ctx)
        toks = toks[1 + fin_cte :]
    clauses = _decouper_clauses(toks)

    tables: dict[str, str | None] = {}
    if "from" in clauses:
        _traiter_from(clauses["from"], ctx, tables)

    # Sous-requêtes nichées dans les autres clauses (WHERE IN, EXISTS...).
    for nom, ctoks in clauses.items():
        if nom == "from":
            continue
        for sub in _sous_requetes(ctoks):
            _traiter_niveau(sub, ctx)

    alias_select: set[str] = set()
    sorties: list[str] = []
    if "select" in clauses:
        sorties = _sorties_select(clauses["select"], tables, ctx)
        for tok in clauses["select"]:
            if isinstance(tok, IdentifierList):
                idents = list(tok.get_identifiers())
            elif isinstance(tok, Identifier):
                idents = [tok]
            else:
                continue
            for ident in idents:
                a = ident.get_alias()
                if a:
                    alias_select.add(a.lower())
        for qualif, nom in _extraire_colonnes(clauses["select"], ctx["erreurs"]):
            _valider_ref(qualif, nom, tables, alias_select, ctx)

    for nom in ("where", "group", "having", "order"):
        for qualif, col in _extraire_colonnes(clauses.get(nom, []), ctx["erreurs"]):
            _valider_ref(qualif, col, tables, alias_select, ctx)

    return tables, sorties


def _verifier_schema(stmt, schema: dict, erreurs: list) -> None:
    ctx = {
        "connues": _colonnes_connues(schema),
        "derivees": {},
        "erreurs": erreurs,
    }
    _traiter_niveau(stmt, ctx)


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------


def valider_sql(requete: str, schema: dict | None = None) -> tuple[bool, str]:
    """Valide une requête SQL générée par le LLM.

    Retourne (True, requete_nettoyee) si tout est OK, sinon (False, raison).
    Si `schema` est None, le schéma est introspecté depuis la base ; en cas
    d'échec d'introspection, seule la validation AST (mots-clés) s'applique.
    """
    if not requete or not requete.strip():
        return False, "Requête vide"
    texte = requete.strip()
    if texte.upper() == "IMPOSSIBLE":
        return False, "Question hors périmètre"

    # Un point-virgule final est une convention d'écriture, pas une
    # deuxième instruction : on le retire AVANT l'analyse. Un ";" restant
    # à l'intérieur sera détecté comme multi-instructions.
    while texte.endswith(";"):
        texte = texte[:-1].strip()
    if not texte:
        return False, "Requête vide"

    instructions = [
        s for s in sqlparse.parse(texte) if str(s).strip().strip(";").strip()
    ]
    if len(instructions) != 1:
        return False, (
            "Plusieurs instructions détectées"
            if len(instructions) > 1
            else "Requête vide"
        )

    stmt = instructions[0]
    if stmt.get_type() != "SELECT":
        return False, "Seules les requêtes SELECT sont autorisées"

    if schema is None:
        try:
            schema = get_schema()
        except Exception:
            schema = None  # pas de base joignable : validation AST seule

    erreurs: list[str] = []
    _verifier_mots_cles(stmt, erreurs)
    if schema is not None:
        _verifier_schema(stmt, schema, erreurs)

    if erreurs:
        vus: list[str] = []
        for e in erreurs:
            if e not in vus:
                vus.append(e)
        return False, "; ".join(vus)

    nettoyee = sqlparse.format(str(stmt), strip_comments=True).strip().rstrip(";")
    return True, nettoyee


def _nettoyer_bloc_code(brut: str) -> str:
    """Retire les clôtures ``` éventuelles autour du SQL généré."""
    return re.sub(r"^```(?:sql)?|```$", "", brut, flags=re.MULTILINE).strip()


def _generer_sql_groq(prompt: str, question: str) -> str:
    from groq import Groq  # import tardif : les tests n'ont pas besoin de clé

    if not os.getenv("GROQ_API_KEY"):
        raise RuntimeError(
            "GROQ_API_KEY non définie. Créez une clé gratuite (tier gratuit, "
            "sans carte bancaire) sur https://console.groq.com puis "
            "renseignez-la dans .env (ou en secret du Space)."
        )
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    reponse = client.chat.completions.create(
        model=os.getenv("MODEL", "openai/gpt-oss-120b"),
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": question},
        ],
        temperature=0,
    )
    return _nettoyer_bloc_code(reponse.choices[0].message.content.strip())


def _generer_sql_anthropic(prompt: str, question: str) -> str:
    from anthropic import Anthropic  # import tardif : pas besoin de clé sinon

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY non définie.")
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    reponse = client.messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5"),
        max_tokens=1024,
        temperature=0,
        system=prompt,
        messages=[{"role": "user", "content": question}],
    )
    return _nettoyer_bloc_code(reponse.content[0].text.strip())


def generer_sql(question: str) -> str:
    """Génère la requête SQL via le LLM configuré.

    `LLM_PROVIDER` : `groq` (défaut, tier gratuit) ou `anthropic`.
    Le prompt système est construit à partir du schéma RÉEL de la base, avec
    le dialecte SQL du moteur connecté (le validateur reste inchangé).
    """
    provider = os.getenv("LLM_PROVIDER", "groq").strip().lower() or "groq"
    engine = get_engine()
    schema = get_schema()
    prompt = construire_prompt_systeme(
        decrire_schema_textuel(schema, engine),
        detecter_jointures(schema),
        dialecte=engine.dialect.name,
    )
    if provider == "anthropic":
        return _generer_sql_anthropic(prompt, question)
    if provider != "groq":
        raise RuntimeError(
            f"LLM_PROVIDER inconnu : {provider!r} (attendu : 'groq' ou 'anthropic')."
        )
    return _generer_sql_groq(prompt, question)


def executer(requete: str):
    """Exécute une requête validée et retourne un DataFrame pandas."""
    with get_engine().connect() as conn:
        return pd.read_sql(requete, conn)


# ---------------------------------------------------------------------------
# Transparence des hypothèses temporelles
# ---------------------------------------------------------------------------


def _decaler_mois(ref: date, delta: int) -> tuple[int, int]:
    mois = ref.month - 1 + delta
    return ref.year + mois // 12, mois % 12 + 1


def _dernier_jour_mois(annee: int, mois: int) -> date:
    premier_suivant = date(annee + 1, 1, 1) if mois == 12 else date(annee, mois + 1, 1)
    return premier_suivant - timedelta(days=1)


def interpretations_temporelles(
    question: str, date_reference: date | None = None
) -> list[str]:
    """Interprétations explicites des expressions temporelles ambiguës.

    Déterministe et testable sans LLM. Chaque note indique l'interprétation
    retenue pour que l'utilisateur voie l'hypothèse faite.
    """
    ref = date_reference or date.today()
    q = question.lower()
    notes: list[str] = []

    def ajouter(motif: str, texte: str) -> None:
        if re.search(motif, q):
            notes.append(texte)

    ajouter(
        r"\bcette année\b",
        f"« cette année » → année civile {ref.year} "
        f"({ref.year}-01-01 → {ref.year}-12-31)",
    )
    an, _ = _decaler_mois(ref, -12)
    ajouter(
        r"\bl['’]année dernière\b|\bl['’]an dernier\b",
        f"« l'année dernière » → année civile {an} ({an}-01-01 → {an}-12-31)",
    )
    an, mo = _decaler_mois(ref, -1)
    ajouter(
        r"\b(du|le|au) mois dernier\b",
        f"« le mois dernier » → {mo:02d}/{an} "
        f"({an}-{mo:02d}-01 → {_dernier_jour_mois(an, mo)})",
    )
    ajouter(
        r"\bce mois-?ci\b",
        f"« ce mois-ci » → {ref.month:02d}/{ref.year} "
        f"({ref.year}-{ref.month:02d}-01 → {_dernier_jour_mois(ref.year, ref.month)})",
    )
    ajouter(
        r"\bcette semaine\b",
        "« cette semaine » → semaine civile en cours (lundi → dimanche)",
    )
    ajouter(
        r"\bla semaine dernière\b",
        "« la semaine dernière » → semaine civile précédente (lundi → dimanche)",
    )
    ajouter(
        r"\brécemment\b|\bdernièrement\b",
        f"« récemment » → convention : 30 derniers jours "
        f"(depuis {ref - timedelta(days=30)})",
    )
    an3, mo3 = _decaler_mois(ref, -3)
    ajouter(
        r"\bces derniers mois\b",
        f"« ces derniers mois » → convention : 3 derniers mois "
        f"(depuis {an3}-{mo3:02d}-01)",
    )
    ajouter(r"\bhier\b", f"« hier » → {ref - timedelta(days=1)}")
    ajouter(r"\baujourd['’]hui\b", f"« aujourd'hui » → {ref}")
    ajouter(
        r"\bcet été\b",
        f"« cet été » → juin–août {ref.year} "
        f"({ref.year}-06-01 → {ref.year}-08-31)",
    )
    ajouter(
        r"\bcet hiver\b",
        f"« cet hiver » → décembre {ref.year - 1} – février {ref.year}",
    )
    debut_trim = ((ref.month - 1) // 3) * 3 + 1
    ajouter(
        r"\bce trimestre\b",
        f"« ce trimestre » → T{(debut_trim - 1) // 3 + 1} {ref.year} "
        f"(depuis {ref.year}-{debut_trim:02d}-01)",
    )
    return notes


def extraire_bornes_dates(sql: str) -> list[str]:
    """Dates littérales 'AAAA-MM-JJ' présentes dans la requête générée."""
    return sorted(set(re.findall(r"'(\d{4}-\d{2}-\d{2})'", sql)))


if __name__ == "__main__":
    question = "Combien j'ai dépensé chez mes fournisseurs en juin ?"
    sql = generer_sql(question)
    print(sql)
    ok, resultat = valider_sql(sql)
    print(ok, resultat)
    if ok:
        print(executer(resultat))
    for note in interpretations_temporelles(question):
        print("HYPOTHÈSE :", note)
