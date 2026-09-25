"""Connexion à la base et définition du schéma partagé.

Le schéma est défini UNE fois ici (SQLAlchemy Core) : `seed.py` crée les
tables à partir de ces métadonnées, et `schema.py` lit le schéma réel depuis
la base. Aucun schéma en dur ailleurs dans le code.
"""
import os

from dotenv import load_dotenv
from sqlalchemy import (
    Column,
    Date,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    create_engine,
)

load_dotenv()

metadata = MetaData()

fournisseurs = Table(
    "fournisseurs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("nom", String(80), unique=True, nullable=False),
    Column("categorie", String(40), nullable=False),
    Column("ville", String(60)),
)

transactions = Table(
    "transactions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("date", Date, nullable=False),
    Column("libelle", String(120), nullable=False),
    Column("fournisseur", String(80), ForeignKey("fournisseurs.nom")),
    Column("categorie", String(40), nullable=False),
    Column("montant", Numeric(10, 2), nullable=False),
    Column("moyen_paiement", String(20), nullable=False),
)


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL non définie. Copiez .env.example vers .env et renseignez-la."
        )
    return url


_engine = None


def get_engine():
    """Moteur SQLAlchemy (créé paresseusement, une seule fois)."""
    global _engine
    if _engine is None:
        _engine = create_engine(get_database_url())
    return _engine
