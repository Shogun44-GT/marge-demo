"""Interface Streamlit : question en français -> SQL validé -> résultat.

Deux modes :
- standard : `streamlit run app.py` (DATABASE_URL dans .env) ;
- démo Marge : `MARGE_DEMO=1 streamlit run app.py` — tourne contre la base
  SQLite `demo/marge_demo.db` (boutique fictive « Lueur & Cire »), avec des
  questions d'exemple cliquables. Lecture seule dans tous les cas.
"""
import os
from pathlib import Path

# --- Adaptateur Streamlit Community Cloud ---
# Les secrets y sont exposés via st.secrets (pas en variables d'environnement) :
# on les recopie dans os.environ au démarrage. Sans effet en local.
try:
    import streamlit as _st_cloud

    for _cle, _valeur in dict(_st_cloud.secrets).items():
        if isinstance(_valeur, str) and _cle not in os.environ:
            os.environ[_cle] = _valeur
except Exception:
    pass
# Ce dépôt est la démo : mode démo actif par défaut.
os.environ.setdefault("MARGE_DEMO", "1")

MARGE_DEMO = os.getenv("MARGE_DEMO", "").strip().lower() in (
    "1", "true", "yes", "on",
)


def _chemin_base_demo() -> Path:
    racine = Path(__file__).resolve().parent
    for candidat in (
        os.getenv("MARGE_DEMO_DB"),
        racine / "demo" / "marge_demo.db",
        racine / "marge_demo.db",
    ):
        if candidat and Path(candidat).exists():
            return Path(candidat)
    return racine / "demo" / "marge_demo.db"


if MARGE_DEMO:
    # Doit être défini AVANT l'import de core/db (DATABASE_URL lu à l'usage,
    # setdefault ne écrase pas un .env explicite).
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{_chemin_base_demo()}")

import streamlit as st

from core import (
    executer,
    extraire_bornes_dates,
    generer_sql,
    get_schema,
    interpretations_temporelles,
    valider_sql,
)
from schema import decrire_schema_textuel, detecter_jointures

EXEMPLES_DEMO = [
    "Quel produit m'a rapporté le plus ce mois-ci ?",
    "Quel est mon chiffre d'affaires par mois sur les 12 derniers mois ?",
    "Quels sont mes 5 meilleurs clients ?",
    "Combien de commandes ai-je eues cette année ?",
    "Quelle catégorie de produits se vend le mieux ?",
]

EXEMPLES = [
    "Combien j'ai dépensé chez mes fournisseurs en juin ?",
    "Quelles sont mes 5 plus grosses dépenses ?",
    "Quel est le total par catégorie ?",
    "Combien j'ai payé en espèces cette année ?",
    "Quel fournisseur m'a coûté le plus cher, et dans quelle ville est-il ?",
]


def _fournisseur_llm() -> str:
    provider = os.getenv("LLM_PROVIDER", "groq").strip().lower() or "groq"
    if provider == "anthropic":
        return f"anthropic / {os.getenv('ANTHROPIC_MODEL', 'claude-haiku-4-5')}"
    return f"groq / {os.getenv('MODEL', 'openai/gpt-oss-120b')}"


if MARGE_DEMO:
    st.set_page_config(page_title="Marge — Démo", layout="centered")
    st.title("🕯️ Marge — Démo")
    st.caption(
        "Boutique fictive « Lueur & Cire » (bougies artisanales) — "
        "données 100 % synthétiques. Posez votre question en français."
    )
    st.info("🔒 Mode démo : lecture seule, aucune donnée ne peut être modifiée.")
    exemples = EXEMPLES_DEMO
else:
    st.set_page_config(page_title="Assistant SQL", layout="centered")
    st.title("Assistant SQL")
    st.caption("Posez une question en français, obtenez la réponse depuis la base.")
    exemples = EXEMPLES

with st.sidebar:
    st.subheader("Base détectée")
    try:
        schema = get_schema()
        for table, info in schema.items():
            with st.expander(f"🗃️ {table}"):
                for c in info["colonnes"]:
                    st.write(f"- {c['nom']} ({c['type']})")
        jointures = detecter_jointures(schema)
        if jointures:
            st.subheader("Jointures plausibles")
            for j in jointures:
                st.write(f"🔗 {j}")
    except Exception as e:
        st.error(f"Schéma indisponible : {e}")

    st.subheader("Moteur")
    st.write(f"🤖 {_fournisseur_llm()}")

if "question" not in st.session_state:
    st.session_state.question = exemples[0]

if MARGE_DEMO:
    st.subheader("Essayez une question")
    for i, exemple in enumerate(exemples):
        if st.button(exemple, key=f"exemple_{i}", use_container_width=True):
            st.session_state.question = exemple

question = st.text_input("Votre question", value=st.session_state.question)
st.session_state.question = question

if st.button("Interroger", type="primary"):
    notes = interpretations_temporelles(question)
    if notes:
        with st.expander("🔍 Hypothèses d'interprétation", expanded=True):
            for note in notes:
                st.info(note)

    with st.spinner("Génération de la requête..."):
        try:
            sql = generer_sql(question)
        except RuntimeError as e:
            st.error(str(e))
            if MARGE_DEMO:
                st.warning(
                    "Astuce démo : définissez GROQ_API_KEY (clé gratuite, sans "
                    "carte bancaire : https://console.groq.com) comme variable "
                    "d'environnement ou secret du Space."
                )
            st.stop()
        except Exception as e:
            st.error(f"Erreur de génération : {e}")
            st.stop()

    st.subheader("Requête générée")
    st.code(sql, language="sql")

    bornes = extraire_bornes_dates(sql)
    if bornes:
        st.caption(f"📅 Dates filtrées par la requête : {', '.join(bornes)}")

    ok, resultat = valider_sql(sql)
    if not ok:
        st.error(f"Requête refusée : {resultat}")
    else:
        st.success("Lecture seule validée")
        try:
            df = executer(resultat)
        except Exception as erreur:
            st.error(f"Erreur d'exécution : {erreur}")
        else:
            st.subheader("Résultat")
            st.dataframe(df, use_container_width=True)
            if df.shape[1] == 2 and len(df) > 1:
                st.bar_chart(df.set_index(df.columns[0]))
