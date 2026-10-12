"""
Observatoire du marché BVMAC – Étape 2 : extraction des données des Bulletins Officiels de la Cote

Lit tous les PDF de data/boc/ et produit 4 fichiers CSV dans data/csv/ :
  - synthese.csv                    : une ligne par séance (indice BVMAC-AS, volumes et valeurs échangés)
  - actions.csv                     : une ligne par séance et par action (cours, volumes, statut…)
  - obligations_cotation.csv        : une ligne par séance et par ligne obligataire (cours, coupon couru, volumes…)
  - obligations_caracteristiques.csv: une ligne par séance et par ligne obligataire (taux facial, amortissement,
                                      échéances, encours…)

Utilisation (Anaconda Prompt, dans D:\\bvmac_projet) :
    pip install pdfplumber pandas
    python 02_extraire_boc.py

Principe : pdfplumber donne chaque mot du PDF avec sa position. Les mots d'une même ligne du tableau
ont la même ordonnée ; les milliers ("228 085") sont reconstitués car leurs groupes de chiffres sont
séparés de moins de 3 points, alors que deux colonnes sont toujours séparées de plus de 4 points.
"""

import re
import sys
from pathlib import Path

import pandas as pd
import pdfplumber

DOSSIER_PDF = Path("data/boc")
DOSSIER_CSV = Path("data/csv")

RE_DATE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
RE_ISIN = re.compile(r"([A-Z]{2})\s?(\d{10})")
RE_NOMBRE_STATUT = re.compile(r"^(-?[\d ]+(?:,\d+)?) ([A-Za-z][A-Za-z.]*)$")
ECART_MILLIERS = 3.0     # en points PDF
TOL_LIGNE = 2.5          # tolérance verticale pour regrouper les mots d'une même ligne


# ----------------------------------------------------------------------------- outils
def nombre(txt):
    """'1 655 500' -> 1655500.0 ; '0,00%' -> 0.0 ; '-' ou 'NC' -> None."""
    if txt is None:
        return None
    t = txt.replace("\u00a0", "").replace(" ", "").replace("%", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def lignes_de_la_page(page):
    """Reconstitue les lignes du tableau à partir des caractères du PDF.

    Les caractères « espace » du PDF sont ignorés (ils chevauchent souvent les chiffres) : seuls les
    écarts mesurés entre caractères visibles comptent.
      écart < 1 pt          -> même mot
      1 pt <= écart < 3 pt  -> espace à l'intérieur d'une cellule (séparateur de milliers, mots d'un libellé)
      écart >= 3 pt         -> changement de colonne
    """
    chars = [c for c in page.chars if c["text"].strip()]
    chars.sort(key=lambda c: c["top"])
    lignes, courante = [], []
    for c in chars:
        if courante and abs(c["top"] - courante[0]["top"]) > TOL_LIGNE:
            lignes.append(courante)
            courante = []
        courante.append(c)
    if courante:
        lignes.append(courante)

    resultat = []
    for ligne in lignes:
        ligne.sort(key=lambda c: c["x0"])
        jetons, texte, x1 = [], "", None
        for c in ligne:
            if x1 is not None:
                ecart = c["x0"] - x1
                if ecart >= ECART_MILLIERS:
                    jetons.append(texte)
                    texte = ""
                elif ecart >= 1.0:
                    texte += " "
            texte += c["text"]
            x1 = c["x1"] if x1 is None else max(x1, c["x1"])
        if texte:
            jetons.append(texte)
        # « 0 NC » : nombre de transactions collé au statut -> deux cellules
        propres = []
        for j in jetons:
            m = RE_NOMBRE_STATUT.match(j)
            propres += [m.group(1), m.group(2)] if m else [j]
        resultat.append(propres)
    return resultat


def isin_de(jetons):
    for j in jetons:
        m = RE_ISIN.search(j)
        if m:
            return m.group(1) + m.group(2)
    return None


# ----------------------------------------------------------------------------- lecture d'un bulletin
def lire_bulletin(chemin):
    pdf = pdfplumber.open(chemin)
    toutes = []           # (numéro de page, jetons)
    texte_p1_p2 = ""
    for i, page in enumerate(pdf.pages):
        for jetons in lignes_de_la_page(page):
            toutes.append((i, jetons))
        if i < 3:
            texte_p1_p2 += (page.extract_text() or "") + "\n"
    pdf.close()

    m = re.search(r"BULLETIN OFFICIEL DE LA COTE N°\s*(\d+)\s*DU\s*(\d{2}/\d{2}/\d{4})", texte_p1_p2)
    if not m:
        raise ValueError("en-tête du bulletin introuvable")
    numero, seance = int(m.group(1)), pd.to_datetime(m.group(2), dayfirst=True)

    synthese = {"date": seance, "numero": numero, "indice_bvmac_as": None}
    for page, jetons in toutes:
        if page == 0 and "BVMAC-AS" in jetons:
            k = jetons.index("BVMAC-AS")
            if k + 1 < len(jetons) and nombre(jetons[k + 1]) is not None:
                synthese["indice_bvmac_as"] = nombre(jetons[k + 1])
                break

    actions, obl_cot, obl_car = [], [], []
    isin_en_attente = None   # ISIN d'une action dont les chiffres sont sur la ligne suivante

    for page, jetons in toutes:
        texte = " ".join(jetons)

        # --- tableau de synthèse
        for instr in ("ACTIONS", "OBLIGATIONS"):
            if jetons and jetons[0] == instr and len(jetons) == 5 and all(nombre(x) is not None for x in jetons[1:]):
                v = [nombre(x) for x in jetons[1:]]
                synthese.update({f"{instr.lower()}_volume": v[0], f"{instr.lower()}_valeur": v[1],
                                 f"{instr.lower()}_transactions": v[2], f"{instr.lower()}_emetteurs": v[3]})

        isin = isin_de(jetons)
        dates = [k for k, j in enumerate(jetons) if RE_DATE.match(j)]

        # --- tableaux de caractéristiques des obligations (repérés par le type d'amortissement)
        amort = next((j for j in jetons if j in ("CONSTANT", "IN FINE")), None)
        if amort:
            k = jetons.index(amort)
            avant, apres = jetons[:k], jetons[k + 1:]
            # avant : ... ISIN | mnemo | taux facial | date 1re cotation | montant levé | nb titres | nominal | cours % | maturité
            try:
                idx_d1 = max(i for i, j in enumerate(avant) if RE_DATE.match(j))
                obl_car.append({
                    "date": seance, "isin": isin, "mnemo": avant[idx_d1 - 2],
                    "taux_facial": nombre(avant[idx_d1 - 1]),
                    "date_premiere_cotation": pd.to_datetime(avant[idx_d1], dayfirst=True),
                    "montant_leve": nombre(avant[idx_d1 + 1]),
                    "nombre_titres": nombre(avant[idx_d1 + 2]),
                    "nominal": nombre(avant[idx_d1 + 3]),
                    "cours_pct": nombre(avant[idx_d1 + 4]),
                    "maturite_ans": nombre(avant[idx_d1 + 5]),
                    "amortissement": amort,
                    "periodicite": apres[0],
                    "echeance_precedente": pd.to_datetime(apres[1], dayfirst=True),
                    "echeance_suivante": pd.to_datetime(apres[2], dayfirst=True),
                    "encours": nombre(apres[3]),
                    "rendement_affiche": nombre(apres[4]),
                    "taux_rotation": nombre(apres[5]) if len(apres) > 5 else None,
                })
            except (IndexError, ValueError):
                print(f"   ! ligne de caractéristiques non lue : {texte[:100]}")
            continue

        # --- ligne d'action dont l'ISIN est seul sur sa ligne (les chiffres sont à la ligne suivante)
        if isin and isin[2:9] == "0000010" and not dates:
            isin_en_attente = isin
            continue

        if not dates:
            continue
        d = dates[0]

        # Statut de cotation (NC, PEq…) : première cellule non numérique après la date
        st = next((k for k in range(d + 1, len(jetons)) if nombre(jetons[k]) is None), None)
        if st is None:
            continue
        apres = jetons[st + 1:]
        pct = [x for x in apres if x.endswith("%")]
        sans_pct = [x for x in apres if not x.endswith("%")]

        # --- marché des actions : ... | nom court | cours précédent | date | dem | off | transigé | valeur | nb | statut | ouv | clôt | seuil haut | seuil bas | var % | réf | haut | bas | var année
        code = isin or isin_en_attente
        if code and code[2:9] == "0000010" and st - d == 6 and d >= 2:
            isin_en_attente = None
            v = jetons[d + 1:st]
            actions.append({
                "date": seance, "isin": code, "nom": jetons[d - 2].replace("(cid:9)", "").strip(),
                "cours_precedent": nombre(jetons[d - 1]),
                "volume_demande": nombre(v[0]), "volume_offert": nombre(v[1]),
                "volume_transige": nombre(v[2]), "valeur_transigee": nombre(v[3]),
                "nb_transactions": nombre(v[4]), "statut": jetons[st],
                "cours_ouverture": nombre(sans_pct[0]) if len(sans_pct) > 0 else None,
                "cours_cloture": nombre(sans_pct[1]) if len(sans_pct) > 1 else None,
                "variation_pct": nombre(pct[0]) if pct else None,
                "plus_haut_annee": nombre(sans_pct[5]) if len(sans_pct) > 5 else None,
                "plus_bas_annee": nombre(sans_pct[6]) if len(sans_pct) > 6 else None,
            })
            continue

        # --- marché des obligations : émetteur | titre | ISIN | mnemo | date | cours % | cours FCFA | nominal | coupon couru | dem | off | transigé | valeur | nb | statut | ouv | clôt | seuils | var | réf
        if isin and isin[2:9] == "0000020" and st - d == 10 and d >= 2:
            v = jetons[d + 1:st]
            k_isin = next(k for k in range(d) if RE_ISIN.search(jetons[k]))
            colle = RE_ISIN.sub("", jetons[k_isin]).strip()      # titre collé à l'ISIN (libellé trop long)
            titre = colle or (jetons[k_isin - 1] if k_isin >= 1 else "")
            emetteur = jetons[0] if (k_isin >= 2 or (colle and k_isin >= 1)) else ""
            obl_cot.append({
                "date": seance, "isin": isin, "mnemo": jetons[d - 1], "emetteur": emetteur,
                "titre": titre,
                "categorie": "Souveraine" if emetteur.startswith("ETAT") else
                             ("Régionale" if emetteur.startswith("BDEAC") else "Privée"),
                "cours_pct": nombre(v[0]), "cours_fcfa": nombre(v[1]),
                "nominal_restant": nombre(v[2]), "coupon_couru": nombre(v[3]),
                "volume_demande": nombre(v[4]), "volume_offert": nombre(v[5]),
                "volume_transige": nombre(v[6]), "valeur_transigee": nombre(v[7]),
                "nb_transactions": nombre(v[8]), "statut": jetons[st],
                "cours_cloture_pct": nombre(sans_pct[1]) if len(sans_pct) > 1 else None,
                "variation_pct": nombre(pct[0]) if pct else None,
            })

    # ISIN illisible dans le tableau des caractéristiques (libellé sur deux lignes) : on le reprend
    # du tableau de cotation, via le mnémonique
    isin_par_mnemo = {o["mnemo"]: o["isin"] for o in obl_cot}
    for o in obl_car:
        o["isin"] = isin_par_mnemo.get(o["mnemo"], o["isin"])

    return synthese, actions, obl_cot, obl_car


# ----------------------------------------------------------------------------- programme principal
def main():
    fichiers = sorted(DOSSIER_PDF.glob("BOC-*.pdf"))
    if len(sys.argv) > 1:
        fichiers = [Path(f) for f in sys.argv[1:]]
    DOSSIER_CSV.mkdir(parents=True, exist_ok=True)

    S, A, OC, OK = [], [], [], []
    erreurs = []
    for f in fichiers:
        try:
            s, a, oc, ok = lire_bulletin(f)
            S.append(s); A += a; OC += oc; OK += ok
            print(f"{f.name}  actions={len(a):2d}  obligations={len(oc):2d}  caractéristiques={len(ok):2d}")
        except Exception as e:
            erreurs.append(f.name)
            print(f"{f.name}  ERREUR : {e}")

    sorties = {
        "synthese.csv": pd.DataFrame(S),
        "actions.csv": pd.DataFrame(A),
        "obligations_cotation.csv": pd.DataFrame(OC),
        "obligations_caracteristiques.csv": pd.DataFrame(OK),
    }
    for nom, df in sorties.items():
        if not df.empty:
            df = df.sort_values([c for c in ("date", "isin") if c in df.columns])
        df.to_csv(DOSSIER_CSV / nom, index=False, encoding="utf-8-sig", sep=";", decimal=",")
        print(f"-> {nom} : {len(df)} lignes")

    print(f"\n{len(fichiers) - len(erreurs)} bulletins lus, {len(erreurs)} en erreur {erreurs if erreurs else ''}")


if __name__ == "__main__":
    main()
