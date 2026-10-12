"""
Observatoire du marché BVMAC – Étape 1 : téléchargement des Bulletins Officiels de la Cote (BOC)

Télécharge les BOC quotidiens publiés par la BVMAC (https://www.bvm-ac.org) sur une période donnée.
Adresse type : https://www.bvm-ac.org/wp-content/uploads/AAAA/MM/BOC-AAAAMMJJ.pdf

Utilisation (Anaconda Prompt) :
    pip install requests
    python 01_telecharger_boc.py                     # du 1er janvier 2026 à aujourd'hui
    python 01_telecharger_boc.py 2025-01-01 2026-10-10

Les PDF sont enregistrés dans data/boc/. Les fichiers déjà présents ne sont pas retéléchargés.
"""

import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

BASE = "https://www.bvm-ac.org/wp-content/uploads/{annee}/{mois:02d}/BOC-{jour}.pdf"
DOSSIER = Path("data/boc")
PAUSE = 1.0  # secondes entre deux requêtes, pour ne pas surcharger le site
ENTETES = {"User-Agent": "Mozilla/5.0 (projet etudiant - observatoire BVMAC)"}


def jours_ouvres(debut: date, fin: date):
    d = debut
    while d <= fin:
        if d.weekday() < 5:  # lundi à vendredi
            yield d
        d += timedelta(days=1)


def urls_candidates(d: date):
    """Le PDF est en général rangé dans le dossier du mois de la séance,
    parfois dans celui du mois suivant (bulletin mis en ligne plus tard)."""
    jour = d.strftime("%Y%m%d")
    suivant = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
    for ref in (d, suivant):
        yield BASE.format(annee=ref.year, mois=ref.month, jour=jour)


def telecharger(d: date, session: requests.Session) -> str:
    cible = DOSSIER / f"BOC-{d:%Y%m%d}.pdf"
    if cible.exists() and cible.stat().st_size > 10_000:
        return "déjà présent"
    for url in urls_candidates(d):
        try:
            r = session.get(url, headers=ENTETES, timeout=30)
        except requests.RequestException as e:
            return f"erreur réseau ({e.__class__.__name__})"
        time.sleep(PAUSE)
        if r.status_code == 200 and r.content[:4] == b"%PDF":
            cible.write_bytes(r.content)
            return "téléchargé"
    return "absent (jour férié ou non publié)"


def main():
    debut = datetime.strptime(sys.argv[1], "%Y-%m-%d").date() if len(sys.argv) > 1 else date(2026, 1, 1)
    fin = datetime.strptime(sys.argv[2], "%Y-%m-%d").date() if len(sys.argv) > 2 else date.today()
    DOSSIER.mkdir(parents=True, exist_ok=True)

    bilan = {}
    with requests.Session() as s:
        for d in jours_ouvres(debut, fin):
            etat = telecharger(d, s)
            bilan[etat] = bilan.get(etat, 0) + 1
            print(f"{d:%d/%m/%Y}  {etat}")

    print("\nBilan :", bilan)
    print(f"PDF disponibles dans : {DOSSIER.resolve()}")


if __name__ == "__main__":
    main()
