#!/usr/bin/env python3
"""Diagnostic des produits de rafales PE-AROME (lancé à la main) : quels produits de vent existent, comment Météo-France les
décrit (unité, période), et les probabilités brutes autour du pic du jour (sans le maximum sur 3 h du pipeline).

Écrit seulement dans le journal du workflow ; ne publie rien. Clé : METEOFRANCE_PAQUET_API_KEY.
"""
import os
import re
import sys
from datetime import datetime, timedelta

import numpy as np
import requests

sys.path.insert(0, os.path.dirname(__file__))
from update_pearome import LAT0, LAT1, LON0, LON1, call, decode  # noqa: E402

LIEUX = {"Perpignan": (42.70, 2.89), "Rivesaltes": (42.77, 2.87), "Cap Béar": (42.51, 3.13), "Leucate": (42.91, 3.03)}


def main() -> int:
    key = os.environ.get("METEOFRANCE_PAQUET_API_KEY", "")
    if not key:
        print("METEOFRANCE_PAQUET_API_KEY manquante")
        return 1
    s = requests.Session()
    cap = call(s, "GetCapabilities?service=WCS&version=2.0.1&language=eng", key).text
    ids = re.findall(r"<wcs:CoverageId>([^<]+)</wcs:CoverageId>", cap)
    prefixes = sorted({i.split("___")[0] for i in ids})
    print(f"== {len(prefixes)} produits au catalogue ; ceux du vent :")
    for p in prefixes:
        if re.search(r"RAF|FF|VENT|WIND|GUST", p):
            runs = sorted(i.split("___")[1] for i in ids if i.startswith(p + "___"))
            print(f"  {p}  (dernier run {runs[-1]})")
    print("== Tous les produits :", ", ".join(prefixes))

    for seuil in (40, 50, 70):
        prefix = f"N_PROBA_RAF_{seuil}__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND"
        runs = sorted(i.split("___")[1] for i in ids if i.startswith(prefix + "___"))
        if not runs:
            print(f"== {prefix} absent")
            continue
        cov = f"{prefix}___{runs[-1]}"
        desc = call(s, f"DescribeCoverage?service=WCS&version=2.0.1&coverageid={cov}", key).text
        if seuil == 50:
            print(f"== DescribeCoverage {cov} (extraits)")
            for motif in (r"<gml:description>[^<]*", r"uom[^>]*", r"<swe:label>[^<]*", r"<swe:description>[^<]*", r"<gmlcov:metadata.{0,600}", r"<gml:boundedBy.{0,400}", r"interval[^>]{0,200}", r"<gml:beginPosition>[^<]*", r"<gml:endPosition>[^<]*", r"<gml:timePosition>[^<]*"):
                for m in re.findall(motif, desc, flags=re.S)[:4]:
                    print("   ", re.sub(r"\s+", " ", m)[:400])
            print("   (début brut)", re.sub(r"\s+", " ", desc[:1500]))
        run_dt = datetime.fromisoformat(runs[-1].replace(".", ":").replace("Z", "+00:00"))
        print(f"== Rafales ≥ {seuil} km/h, run {run_dt:%Y-%m-%d %H:%M} UTC : valeur brute heure par heure")
        for lead in range(1, 49):
            t = run_dt + timedelta(hours=lead)
            stamp = t.strftime("%Y-%m-%dT%H:%M:%SZ")
            r = call(s, f"GetCoverage?service=WCS&version=2.0.1&coverageid={cov}&format=application%2Fwmo-grib&subset=time({stamp})&subset=lat({LAT0},{LAT1})&subset=long({LON0},{LON1})", key)
            if r.status_code != 200 or r.content[:4] != b"GRIB":
                continue
            g = np.nan_to_num(decode(r.content), nan=0.0)
            if g.max() <= 0 and lead % 6:
                continue
            nj, ni = g.shape
            pts = []
            for nom, (la, lo) in LIEUX.items():
                y = int(round((LAT1 - la) / (LAT1 - LAT0) * (nj - 1)))
                x = int(round((lo - LON0) / (LON1 - LON0) * (ni - 1)))
                pts.append(f"{nom} {g[y, x]:.0f}")
            valeurs = sorted({round(float(v), 1) for v in g.ravel() if v > 0})[:12]
            print(f"  +{lead:02d} h {t:%d/%m %H} h UTC : max {g.max():.1f} %, {' · '.join(pts)} ; premières valeurs non nulles {valeurs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
