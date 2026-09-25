"""Marge — démo interactive.

Posez votre question de gestion en français, obtenez la réponse chiffrée.
Boutique fictive « Lueur & Cire », données 100 % synthétiques, lecture seule.
"""
import os
from pathlib import Path

# --- Adaptateur Streamlit Community Cloud -----------------------------------
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
        racine / "marge_demo.db",
    ):
        if candidat and Path(candidat).exists():
            return Path(candidat)
    return racine / "marge_demo.db"


if MARGE_DEMO:
    # Doit être défini AVANT l'import de core (DATABASE_URL lu à l'usage,
    # setdefault n'écrase pas une valeur explicite).
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{_chemin_base_demo()}")

import streamlit as st

from core import (
    executer,
    extraire_bornes_dates,
    generer_sql,
    interpretations_temporelles,
    valider_sql,
)

EXEMPLES = [
    "Quel produit m'a rapporté le plus ce mois-ci ?",
    "Quel est mon chiffre d'affaires par mois sur les 12 derniers mois ?",
    "Quels sont mes 5 meilleurs clients ?",
    "Combien de commandes ai-je eues cette année ?",
    "Quelle catégorie de produits se vend le mieux ?",
    "Quel est mon chiffre d'affaires total cette année ?",
]

st.set_page_config(
    page_title="Marge — Vos chiffres, en français",
    page_icon="📊",
    layout="wide",
)

CSS = """
<style>
/* --- Chrome Streamlit : on épure --- */
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
header[data-testid="stHeader"] {visibility: hidden;}

.block-container {max-width: 920px; padding-top: 2.2rem; padding-bottom: 2rem;}

/* --- Barre de marque --- */
.brand {display: flex; align-items: center; justify-content: space-between;
        margin-bottom: 2.4rem;}
.wordmark {font-size: 1.5rem; font-weight: 800; letter-spacing: 0.14em; color: #F5F7FA;}
.wordmark .dot {color: #E9B44C;}
.badges {display: flex; gap: 0.5rem;}
.badge {font-size: 0.7rem; font-weight: 700; letter-spacing: 0.1em;
        padding: 0.38rem 0.75rem; border-radius: 999px;
        border: 1px solid #2A3342; color: #9AA4B2; text-transform: uppercase;}
.badge.gold {border-color: #E9B44C; color: #E9B44C;}

/* --- Hero --- */
.eyebrow {color: #E9B44C; font-weight: 700; letter-spacing: 0.2em;
          font-size: 0.72rem; text-transform: uppercase; margin-bottom: 0.7rem;}
.hero-title {font-size: 2.7rem; line-height: 1.12; font-weight: 800;
             color: #F5F7FA; margin: 0 0 0.8rem 0;}
.hero-sub {color: #9AA4B2; font-size: 1.07rem; line-height: 1.65;
           margin-bottom: 1.9rem; max-width: 640px;}
.hero-sub strong {color: #D5DBE4; font-weight: 600;}

/* --- Libellés de section --- */
.section-label {color: #E9B44C; font-size: 0.75rem; font-weight: 700;
                letter-spacing: 0.18em; text-transform: uppercase;
                margin: 1.6rem 0 0.8rem 0;}

/* --- Champ de question --- */
.stTextInput > div > div > input {font-size: 1.05rem; border-radius: 12px;
    padding: 0.9rem 1.1rem;}

/* --- Boutons --- */
.stButton > button {border-radius: 11px; font-weight: 500;
    padding: 0.55rem 0.9rem; line-height: 1.35; white-space: normal; height: auto;
    min-height: 3.1rem;}
.stButton > button[kind="primary"] {font-weight: 700; font-size: 1.02rem;
    min-height: 3.4rem; margin-top: 0.4rem;}

/* --- Carte KPI --- */
.kpi {background: linear-gradient(135deg, #161D29 0%, #1B2434 100%);
      border: 1px solid #2A3342; border-left: 4px solid #E9B44C;
      border-radius: 14px; padding: 1.5rem 1.7rem; margin: 1.1rem 0;}
.kpi-label {color: #9AA4B2; font-size: 0.8rem; font-weight: 600;
            text-transform: uppercase; letter-spacing: 0.12em; margin-bottom: 0.45rem;}
.kpi-value {font-size: 2.5rem; font-weight: 800; color: #F5F7FA; line-height: 1;}

/* --- Étapes --- */
.step-card {background: #121826; border: 1px solid #232C3D; border-radius: 14px;
            padding: 1.25rem 1.3rem; height: 100%;}
.step-num {display: inline-flex; align-items: center; justify-content: center;
           width: 1.9rem; height: 1.9rem; border-radius: 999px;
           background: rgba(233, 180, 76, 0.14); color: #E9B44C;
           font-weight: 800; font-size: 0.95rem; margin-bottom: 0.7rem;}
.step-title {font-weight: 700; color: #F5F7FA; margin-bottom: 0.35rem;}
.step-text {color: #9AA4B2; font-size: 0.92rem; line-height: 1.55;}
.step-text em {color: #C7CEDA;}

/* --- Pied de page --- */
.footer {margin-top: 3rem; padding-top: 1.4rem; border-top: 1px solid #1E2634;
         color: #6B7688; font-size: 0.82rem; text-align: center; line-height: 1.7;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


def _fmt(valeur) -> str:
    """Formate un nombre à la française : 1 234,56."""
    try:
        f = float(valeur)
    except (TypeError, ValueError):
        return str(valeur)
    if f.is_integer():
        return f"{int(f):,}".replace(",", " ")
    return f"{f:,.2f}".replace(",", " ").replace(".", ",").replace(" ", " ")


# --- En-tête de marque ---
st.markdown(
    """
    <div class="brand">
      <div class="wordmark">MARGE<span class="dot">.</span></div>
      <div class="badges">
        <span class="badge gold">Démo</span>
        <span class="badge">🔒 Lecture seule</span>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# --- Hero ---
st.markdown(
    """
    <div class="eyebrow">Démo interactive</div>
    <h1 class="hero-title">Interrogez votre boutique<br>en français.</h1>
    <p class="hero-sub">Marge comprend votre question, génère une requête
    <strong>sécurisée en lecture seule</strong>, et vous donne le chiffre —
    <strong>sans tableur, sans SQL, sans comptable</strong>.</p>
    """,
    unsafe_allow_html=True,
)

if "question" not in st.session_state:
    st.session_state.question = ""
if "lancer" not in st.session_state:
    st.session_state.lancer = False

question = st.text_input(
    "Votre question",
    value=st.session_state.question,
    placeholder="Ex : Quel est mon chiffre d'affaires ce mois-ci ?",
    label_visibility="collapsed",
)
st.session_state.question = question

if st.button("Obtenir la réponse", type="primary", use_container_width=True):
    st.session_state.lancer = True

st.markdown('<p class="section-label">Essayez une question</p>', unsafe_allow_html=True)
colonnes = st.columns(3)
for i, exemple in enumerate(EXEMPLES):
    with colonnes[i % 3]:
        if st.button(exemple, key=f"exemple_{i}", use_container_width=True):
            st.session_state.question = exemple
            st.session_state.lancer = True

# --- Exécution ---
if st.session_state.lancer:
    st.session_state.lancer = False
    q = (st.session_state.question or "").strip()
    if not q:
        st.warning("Posez votre question ci-dessus pour obtenir une réponse.")
        st.stop()

    with st.spinner("Marge interroge votre boutique…"):
        try:
            sql = generer_sql(q)
        except RuntimeError as e:
            st.error(str(e))
            st.warning(
                "La démo n'est pas configurée : la clé d'accès au moteur "
                "de langage est manquante."
            )
            st.stop()
        except Exception as e:
            st.error(f"Erreur de génération : {e}")
            st.stop()

    ok, resultat = valider_sql(sql)
    if not ok:
        st.error(f"Requête refusée par le garde-fou : {resultat}")
        st.stop()

    try:
        df = executer(resultat)
    except Exception as e:
        st.error(f"Erreur d'exécution : {e}")
        st.stop()

    st.markdown('<p class="section-label">Résultat</p>', unsafe_allow_html=True)

    if df.empty:
        st.warning("Aucun résultat pour cette question sur la période analysée.")
    elif df.shape == (1, 1):
        libelle = str(df.columns[0])
        valeur = _fmt(df.iat[0, 0])
        st.markdown(
            f'<div class="kpi"><div class="kpi-label">{libelle}</div>'
            f'<div class="kpi-value">{valeur}</div></div>',
            unsafe_allow_html=True,
        )
    else:
        st.dataframe(df, use_container_width=True)
        if len(df) > 1 and df.shape[1] >= 1:
            try:
                if df.shape[1] >= 2:
                    st.bar_chart(df.set_index(df.columns[0]), color="#E9B44C")
                else:
                    st.bar_chart(df, color="#E9B44C")
            except TypeError:
                # Ancienne version de Streamlit : sans couleur personnalisée.
                if df.shape[1] >= 2:
                    st.bar_chart(df.set_index(df.columns[0]))
                else:
                    st.bar_chart(df)

    bornes = extraire_bornes_dates(sql)
    if bornes:
        st.caption(f"Période analysée : {', '.join(bornes)}")

    notes = interpretations_temporelles(q)
    if notes:
        with st.expander("Précisions sur la période analysée"):
            for note in notes:
                st.write(note)

    with st.expander("Détails techniques"):
        st.code(sql, language="sql")
        st.caption("Requête générée automatiquement, validée en lecture seule.")

# --- Comment ça marche ---
st.markdown('<p class="section-label">Comment ça marche</p>', unsafe_allow_html=True)
c1, c2, c3 = st.columns(3)
with c1:
    st.markdown(
        '<div class="step-card"><div class="step-num">1</div>'
        '<div class="step-title">Vous demandez</div>'
        '<div class="step-text"><em>« Quel produit m\'a rapporté le plus ce mois-ci ? »</em> '
        "— en français, comme vous le diriez à votre comptable.</div></div>",
        unsafe_allow_html=True,
    )
with c2:
    st.markdown(
        '<div class="step-card"><div class="step-num">2</div>'
        '<div class="step-title">Marge traduit</div>'
        '<div class="step-text">Votre question devient une requête SQL, '
        "vérifiée <em>lecture seule</em> : impossible de modifier vos données.</div></div>",
        unsafe_allow_html=True,
    )
with c3:
    st.markdown(
        '<div class="step-card"><div class="step-num">3</div>'
        '<div class="step-title">Vous décidez</div>'
        '<div class="step-text">Le chiffre, le tableau, le graphique : '
        "en <em>quelques secondes</em>, sans ouvrir un tableur.</div></div>",
        unsafe_allow_html=True,
    )

st.markdown(
    """
    <div class="footer">
      Marge — démo · Données 100&nbsp;% fictives : boutique « Lueur &amp; Cire »
      (bougies artisanales)<br>Aucune donnée réelle n'est utilisée ni conservée.
    </div>
    """,
    unsafe_allow_html=True,
)
