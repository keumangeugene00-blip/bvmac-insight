"""
Observatoire du marché BVMAC – Étape 3 : analyse de la liquidité et des rendements obligataires

Lit les CSV produits par 02_extraire_boc.py (data/csv/) et produit dans resultats/ :
  - liquidite_actions.csv        : indicateurs de liquidité par action
  - liquidite_obligations.csv    : indicateurs de liquidité par ligne obligataire
  - rendements_obligations.csv   : rendement actuariel et duration de chaque ligne à la dernière séance
  - fig1_frequence_echanges.png  : part des séances avec au moins une transaction
  - fig2_performance_actions.png : variation des cours depuis la première séance de l'année
  - fig3_courbe_taux.png         : rendement actuariel en fonction de la duration, par émetteur
  - resume.txt                   : les chiffres clés

Utilisation :
    pip install pandas matplotlib scipy
    python 03_analyser.py
"""

import re
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.optimize import brentq

CSV = Path("data/csv")
OUT = Path("resultats")
OUT.mkdir(exist_ok=True)

# Palette (validée daltonisme) et encres
BLEU, ORANGE, VERT_EAU, GRIS = "#2a78d6", "#eb6834", "#1baf7a", "#8a8984"
ENCRE, ENCRE_2, GRILLE = "#0b0b0b", "#52514e", "#e4e3df"
plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": GRILLE,
    "axes.labelcolor": ENCRE_2, "xtick.color": ENCRE_2, "ytick.color": ENCRE_2,
    "axes.spines.top": False, "axes.spines.right": False, "figure.dpi": 150,
})


def lire(nom):
    return pd.read_csv(CSV / f"{nom}.csv", sep=";", decimal=",", parse_dates=["date"])


def fmt(x, dec=0):
    return f"{x:,.{dec}f}".replace(",", " ").replace(".", ",")


synthese = lire("synthese")
actions = lire("actions").drop_duplicates(["date", "isin"])
cotation = lire("obligations_cotation").drop_duplicates(["date", "mnemo"])
caract = lire("obligations_caracteristiques").drop_duplicates(["date", "mnemo"])
for col in ("echeance_suivante", "echeance_precedente", "date_premiere_cotation"):
    caract[col] = pd.to_datetime(caract[col])
n_seances = synthese["date"].nunique()
debut, fin = synthese["date"].min(), synthese["date"].max()

# =============================================================================== 1. Liquidité
# --- actions
liq_a = actions.groupby("nom").agg(
    seances_cotees=("date", "count"),
    seances_avec_echange=("volume_transige", lambda x: int((x > 0).sum())),
    transactions=("nb_transactions", "sum"),
    titres_echanges=("volume_transige", "sum"),
    valeur_echangee_fcfa=("valeur_transigee", "sum"),
    cours_debut=("cours_cloture", "first"),
    cours_fin=("cours_cloture", "last"),
)
liq_a["pct_seances_avec_echange"] = 100 * liq_a.seances_avec_echange / liq_a.seances_cotees
liq_a["part_valeur_marche_pct"] = 100 * liq_a.valeur_echangee_fcfa / liq_a.valeur_echangee_fcfa.sum()
liq_a["variation_cours_pct"] = 100 * (liq_a.cours_fin / liq_a.cours_debut - 1)
# Ordres en attente des deux côtés mais aucune transaction : écart entre prix acheteur et vendeur
bloque = actions[(actions.volume_demande > 0) & (actions.volume_offert > 0) & (actions.volume_transige == 0)]
liq_a["seances_ordres_non_executes"] = bloque.groupby("nom").size().reindex(liq_a.index).fillna(0).astype(int)
liq_a = liq_a.sort_values("pct_seances_avec_echange", ascending=False)
liq_a.round(2).to_csv(OUT / "liquidite_actions.csv", sep=";", decimal=",", encoding="utf-8-sig")

# --- obligations
liq_o = cotation.groupby(["mnemo", "emetteur", "categorie"]).agg(
    seances_cotees=("date", "count"),
    seances_avec_echange=("volume_transige", lambda x: int((x > 0).sum())),
    transactions=("nb_transactions", "sum"),
    valeur_echangee_fcfa=("valeur_transigee", "sum"),
    cours_distincts=("cours_pct", "nunique"),
).reset_index().sort_values("valeur_echangee_fcfa", ascending=False)
liq_o.to_csv(OUT / "liquidite_obligations.csv", sep=";", decimal=",", encoding="utf-8-sig", index=False)

encours_moyen = caract.groupby("date")["encours"].sum().mean()
valeur_obl = synthese["obligations_valeur"].sum()
valeur_act = synthese["actions_valeur"].sum()
pct_sans_action = 100 * (synthese["actions_transactions"] == 0).mean()
pct_sans_oblig = 100 * (synthese["obligations_transactions"] == 0).mean()
lignes_prix_fige = int((liq_o.cours_distincts == 1).sum())

# =============================================================================== 2. Rendements
FREQ = {"AN": 1, "SEM": 2, "TRIM": 4}


def echeancier(row):
    """Flux futurs (dates, montants) pour un titre de nominal restant N.

    Hypothèses : coupons et remboursements aux échéances de périodicité fixe à partir de l'échéance
    suivante, jusqu'à la dernière échéance de l'année de maturité (année finale du libellé).
    Amortissement CONSTANT : nominal restant réparti à parts égales sur les échéances restantes.
    """
    f = FREQ.get(row.periodicite, 1)
    annee_fin = max(int(a) for a in re.findall(r"20\d\d", row.titre))
    dates = []
    d = row.echeance_suivante
    while d.year <= annee_fin:
        dates.append(d)
        d = d + pd.DateOffset(months=12 // f)
    if not dates:
        return [], []
    N, taux = row.nominal_restant, row.taux_facial / 100
    n = len(dates)
    flux, restant = [], N
    for i in range(n):
        coupon = restant * taux / f
        remb = N / n if row.amortissement == "CONSTANT" else (N if i == n - 1 else 0.0)
        flux.append(coupon + remb)
        restant -= remb
    return dates, flux


def rendement(row):
    dates, flux = echeancier(row)
    if not dates:
        return pd.Series({"rendement_actuariel_pct": np.nan, "duration_ans": np.nan, "echeance_finale": pd.NaT})
    reglement = pd.Timestamp(np.busday_offset(row.date.date(), 3, roll="forward"))
    t = np.array([(d - reglement).days / 365.0 for d in dates])
    flux = np.array(flux)
    prix_plein = row.cours_fcfa + row.coupon_couru          # cours pied de coupon + coupon couru
    pv = lambda y: (flux / (1 + y) ** t).sum() - prix_plein
    try:
        y = brentq(pv, -0.5, 1.0)
    except ValueError:
        return pd.Series({"rendement_actuariel_pct": np.nan, "duration_ans": np.nan, "echeance_finale": dates[-1]})
    poids = flux / (1 + y) ** t
    return pd.Series({"rendement_actuariel_pct": 100 * y,
                      "duration_ans": (t * poids).sum() / poids.sum(),
                      "echeance_finale": dates[-1]})


dernier = cotation[cotation.date == fin].merge(
    caract[caract.date == fin][["mnemo", "taux_facial", "amortissement", "periodicite",
                                "echeance_suivante", "encours", "rendement_affiche"]],
    on="mnemo", how="inner")
dernier = pd.concat([dernier, dernier.apply(rendement, axis=1)], axis=1)
dernier["pays_ou_type"] = np.where(dernier.categorie == "Souveraine",
                                   dernier.emetteur.str.replace("ETAT DU ", "").str.title(),
                                   dernier.categorie)
cols = ["mnemo", "titre", "pays_ou_type", "taux_facial", "amortissement", "cours_pct", "nominal_restant",
        "echeance_finale", "duration_ans", "rendement_actuariel_pct", "rendement_affiche", "encours"]
dernier[cols].sort_values(["pays_ou_type", "duration_ans"]).round({"duration_ans": 3, "rendement_actuariel_pct": 3}).to_csv(
    OUT / "rendements_obligations.csv", sep=";", decimal=",", encoding="utf-8-sig", index=False)

# =============================================================================== 3. Graphiques
# Figure 1 : fréquence des échanges
f1 = liq_a["pct_seances_avec_echange"].sort_values()
pct_obl_ligne = 100 * liq_o.seances_avec_echange.sum() / liq_o.seances_cotees.sum()
noms = ["Obligations (ligne moyenne)"] + list(f1.index)
vals = [pct_obl_ligne] + list(f1.values)
fig, ax = plt.subplots(figsize=(8, 4.2))
barres = ax.barh(noms, vals, color=[GRIS] + [BLEU] * len(f1), height=0.6)
for b, v in zip(barres, vals):
    lib = f"{v:.1f} %" if v < 1 else f"{v:.0f} %"
    ax.text(v + 1, b.get_y() + b.get_height() / 2, lib.replace(".", ","),
            va="center", color=ENCRE, fontsize=9)
ax.set_xlim(0, 100)
ax.set_xlabel("Part des séances avec au moins une transaction (%)")
ax.xaxis.grid(True, color=GRILLE); ax.set_axisbelow(True)
ax.set_title(f"Fréquence des échanges à la BVMAC – {n_seances} séances "
             f"({debut:%d/%m} – {fin:%d/%m/%Y})", loc="left", color=ENCRE, fontsize=11)
fig.tight_layout(); fig.savefig(OUT / "fig1_frequence_echanges.png"); plt.close(fig)

# Figure 2 : variation des cours
f2 = liq_a["variation_cours_pct"].sort_values()
fig, ax = plt.subplots(figsize=(8, 3.8))
barres = ax.barh(f2.index, f2.values, color=[ORANGE if v < 0 else BLEU for v in f2.values], height=0.6)
for b, v in zip(barres, f2.values):
    ax.text(v + (0.6 if v >= 0 else -0.6), b.get_y() + b.get_height() / 2,
            f"{v:+.1f} %".replace(".", ","), va="center", ha="left" if v >= 0 else "right",
            color=ENCRE, fontsize=9)
ax.axvline(0, color=ENCRE_2, lw=0.8)
lim = max(abs(f2.min()), abs(f2.max())) + 8
ax.set_xlim(-lim, lim)
ax.set_xlabel("Variation du cours de clôture (%)")
ax.xaxis.grid(True, color=GRILLE); ax.set_axisbelow(True)
ax.set_title(f"Variation des cours des actions, {debut:%d/%m} – {fin:%d/%m/%Y}\n"
             "(BGFI Holding : depuis son introduction en mai ; hors dividendes)",
             loc="left", color=ENCRE, fontsize=11)
fig.tight_layout(); fig.savefig(OUT / "fig2_performance_actions.png"); plt.close(fig)

# Figure 3 : rendements actuariels — lignes au pair (marqueur plein) et sous le pair (marqueur creux)
dernier = dernier.merge(liq_o[["mnemo", "transactions", "valeur_echangee_fcfa"]], on="mnemo", how="left")
dernier["sous_le_pair"] = dernier.cours_pct < 99.5
fig, ax = plt.subplots(figsize=(10, 5.4))
au_pair = dernier[~dernier.sous_le_pair].dropna(subset=["duration_ans"])
pente, ordonnee = np.polyfit(au_pair.duration_ans, au_pair.rendement_actuariel_pct, 1)
xs = np.linspace(dernier.duration_ans.min(), dernier.duration_ans.max(), 50)
ax.plot(xs, pente * xs + ordonnee, color=GRIS, lw=1.2, ls="--", zorder=1, label="Tendance des lignes cotées au pair")
groupes = [("Cameroun", BLEU, "o", "État du Cameroun"), ("Gabon", ORANGE, "s", "État du Gabon"),
           ("Régionale", VERT_EAU, "^", "BDEAC (régionale)")]
for nom, coul, mk, lib in groupes + [(None, GRIS, "D", "Autres (Tchad, émetteurs privés)")]:
    g = dernier[dernier.pays_ou_type == nom] if nom else \
        dernier[~dernier.pays_ou_type.isin([x[0] for x in groupes])]
    p, sp = g[~g.sous_le_pair], g[g.sous_le_pair]
    ax.scatter(p.duration_ans, p.rendement_actuariel_pct, color=coul, marker=mk, s=46,
               edgecolor="white", linewidth=1.5, zorder=3, label=lib)
    ax.scatter(sp.duration_ans, sp.rendement_actuariel_pct, facecolor="white", edgecolor=coul,
               marker=mk, s=46, linewidth=1.8, zorder=3)
ax.scatter([], [], facecolor="white", edgecolor=ENCRE_2, marker="o", s=46, linewidth=1.8,
           label="marqueur creux : cotée sous le pair")
# Lignes nettement au-dessus de la tendance : explication dans une colonne à droite, reliée au point
xmax = dernier.duration_ans.max()
hauts = dernier[dernier.rendement_actuariel_pct > pente * dernier.duration_ans + ordonnee + 1.5] \
    .sort_values("rendement_actuariel_pct", ascending=False)
y_txt = hauts.rendement_actuariel_pct.max() + 0.2
for _, r in hauts.iterrows():
    detail = "aucune transaction en 2026" if r.transactions == 0 else \
        f"{int(r.transactions)} transaction(s) en 2026, {fmt(r.valeur_echangee_fcfa / 1e6, 1)} M FCFA"
    ax.annotate(f"{r.mnemo} – cours {fmt(r.cours_pct, 2)} %\n{detail}",
                xy=(r.duration_ans, r.rendement_actuariel_pct), xytext=(xmax + 0.25, y_txt),
                fontsize=7.5, color=ENCRE_2, va="center",
                arrowprops=dict(arrowstyle="-", color=GRIS, lw=0.7, shrinkA=0, shrinkB=5))
    y_txt -= 0.75
ax.set_xlim(right=xmax + 1.75)
ax.set_xticks([x for x in np.arange(0.5, xmax + 0.5, 0.5)])
ax.set_xlabel("Duration de Macaulay (années)")
ax.set_ylabel("Rendement actuariel (%)")
ax.grid(True, color=GRILLE); ax.set_axisbelow(True)
ax.legend(frameon=False, fontsize=8, loc="lower right")
ax.set_title(f"Rendements actuariels des obligations BVMAC au {fin:%d/%m/%Y}\n"
             "Les rendements élevés viennent de cours sous le pair fixés par peu ou pas de transactions",
             loc="left", color=ENCRE, fontsize=10.5)
fig.tight_layout(); fig.savefig(OUT / "fig3_courbe_taux.png"); plt.close(fig)

# =============================================================================== 4. Résumé
sov = dernier[dernier.categorie == "Souveraine"]
moy_pays = sov.groupby("pays_ou_type")["rendement_actuariel_pct"].mean()
top = liq_a.sort_values("valeur_echangee_fcfa", ascending=False)
lignes = [
    f"OBSERVATOIRE DU MARCHÉ BVMAC – {n_seances} séances du {debut:%d/%m/%Y} au {fin:%d/%m/%Y}",
    "",
    "LIQUIDITÉ",
    f"- Séances sans aucune transaction sur les actions : {pct_sans_action:.0f} %",
    f"- Séances sans aucune transaction sur les obligations : {pct_sans_oblig:.0f} %",
    f"- Valeur échangée sur les actions : {fmt(valeur_act)} FCFA ; sur les obligations : {fmt(valeur_obl)} FCFA",
    f"- Encours obligataire moyen : {fmt(encours_moyen / 1e9)} milliards FCFA ; "
    f"taux de rotation sur la période : {100 * valeur_obl / encours_moyen:.2f} %",
    f"- Lignes obligataires dont le cours n'a jamais changé : {lignes_prix_fige} sur {len(liq_o)}",
    f"- Action la plus liquide : {liq_a.index[0]} (échangée lors de {liq_a.iloc[0].pct_seances_avec_echange:.0f} % des séances)",
    f"- Action la moins liquide : {liq_a.index[-1]} ({liq_a.iloc[-1].pct_seances_avec_echange:.0f} % des séances)",
    f"- Concentration : {top.index[0]} et {top.index[1]} = "
    f"{top.part_valeur_marche_pct.iloc[:2].sum():.0f} % de la valeur échangée sur les actions",
    "",
    f"RENDEMENTS AU {fin:%d/%m/%Y} (rendement actuariel moyen des lignes souveraines)",
] + [f"- {p} : {v:.2f} %" for p, v in moy_pays.items()] + [
    f"- Rendement le plus élevé : {dernier.loc[dernier.rendement_actuariel_pct.idxmax(), 'mnemo']} "
    f"({dernier.rendement_actuariel_pct.max():.2f} %)",
]
texte = "\n".join(lignes).replace(".", ",").replace("02_extraire_boc,py", "02_extraire_boc.py")
(OUT / "resume.txt").write_text(texte, encoding="utf-8")
print(texte)
print(f"\nFichiers enregistrés dans {OUT.resolve()}")
