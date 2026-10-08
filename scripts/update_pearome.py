#!/usr/bin/env python3
"""PEAROME (AROME ensemble, Météo-France) : probabilités de dépassement de seuils sur les Pyrénées-Orientales.

L'API WCS PE-AROME 0,025° n'expose pas les membres un par un mais des produits de probabilité
(rafales >= x km/h, pluie sur 1/6/24 h >= x mm...). On récupère, pour le dernier run, chaque produit
retenu à un pas de 3 h, recadré sur une fenêtre autour du 66, et on publie un JSON compact
(valeurs 0-100 en uint8 base64). Clé : variable d'environnement METEOFRANCE_PAQUET_API_KEY
(jamais en dur, jamais exposée côté site).
"""
import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

BASE = "https://public-api.meteofrance.fr/public/pearome/1.0/wcs/MF-NWP-HIGHRES-PEAROME-0025-FRANCE-WCS/"
UA = "Mozilla/5.0 (compatible; AlertesMeteo-PEAROME/1.0)"
LAT0, LAT1, LON0, LON1 = 41.9, 43.3, 1.4, 3.5
STEP_H = 3
# Environ 30 appels/min : le quota du portail est partagé avec d'autres pipelines.
MIN_INTERVAL = 2.0

# (id court, préfixe de couverture, libellé, famille, seuil)
PRODUCTS = [
    # Orages : probabilité de réflectivité radar simulée élevée (averses orageuses), de grêle et de supercellules.
    ("rfx40", "N_PROBA_RFX_40DBZ__GROUND_OR_WATER_SURFACE", "Orages / fortes averses (réflectivité ≥ 40 dBZ)", "orages", 40),
    ("rfx45", "N_PROBA_RFX_45DBZ__GROUND_OR_WATER_SURFACE", "Orages forts (réflectivité ≥ 45 dBZ)", "orages", 45),
    ("grele8", "N_PROBA_D_GRELE_8__GROUND_OR_WATER_SURFACE", "Grêle (≥ 8 kg/m²)", "grele", 8),
    ("scp1", "N_PROBA_D_SCP_1__GROUND_OR_WATER_SURFACE", "Supercellules (indice SCP > 1)", "supercellules", 1),
    ("raf40", "N_PROBA_RAF_40__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND", "Rafales ≥ 40 km/h", "rafales", 40),
    ("raf50", "N_PROBA_RAF_50__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND", "Rafales ≥ 50 km/h", "rafales", 50),
    ("raf70", "N_PROBA_RAF_70__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND", "Rafales ≥ 70 km/h", "rafales", 70),
    ("p24_20", "N_PROBA_PRECI24_20__GROUND_OR_WATER_SURFACE", "Pluie 24 h ≥ 20 mm", "pluie24", 20),
    ("p24_50", "N_PROBA_PRECI24_50__GROUND_OR_WATER_SURFACE", "Pluie 24 h ≥ 50 mm", "pluie24", 50),
    ("p24_80", "N_PROBA_PRECI24_80__GROUND_OR_WATER_SURFACE", "Pluie 24 h ≥ 80 mm", "pluie24", 80),
    ("p24_120", "N_PROBA_PRECI24_120__GROUND_OR_WATER_SURFACE", "Pluie 24 h ≥ 120 mm", "pluie24", 120),
    ("p24_200", "N_PROBA_PRECI24_200__GROUND_OR_WATER_SURFACE", "Pluie 24 h ≥ 200 mm", "pluie24", 200),
    ("p06_20", "N_PROBA_PRECI06_20__GROUND_OR_WATER_SURFACE", "Pluie 6 h ≥ 20 mm", "pluie6", 20),
    ("p06_60", "N_PROBA_PRECI06_60__GROUND_OR_WATER_SURFACE", "Pluie 6 h ≥ 60 mm", "pluie6", 60),
    ("p06_100", "N_PROBA_PRECI06_100__GROUND_OR_WATER_SURFACE", "Pluie 6 h ≥ 100 mm", "pluie6", 100),
    ("p01_20", "N_PROBA_PRECI01_20__GROUND_OR_WATER_SURFACE", "Pluie 1 h ≥ 20 mm", "pluie1", 20),
]

_last_call = 0.0


class Quota(Exception):
    pass


def call(session: requests.Session, query: str, key: str) -> requests.Response:
    """Un appel WCS espacé de MIN_INTERVAL, avec reprise sur 429 (quota partagé)."""
    global _last_call
    for attempt in range(60):
        wait = _last_call + MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        response = session.get(BASE + query, headers={"apikey": key, "User-Agent": UA}, timeout=(15, 120))
        if response.status_code == 429:
            # Fenêtre d'une minute : on attend la fin de la minute annoncée ("after ... 08:49:00").
            seconds = 20
            match = re.search(r"after \S+ (\d{2}):(\d{2}):(\d{2})", response.text)
            if match:
                now = datetime.now(timezone.utc)
                target = now.replace(hour=int(match[1]), minute=int(match[2]), second=int(match[3]), microsecond=0)
                seconds = max(5, min(70, (target - now).total_seconds() + 3))
            # Quota partagé avec le pipeline AROME (même clé) : après dix refus de suite, on laisse passer
            # une minute entière entre deux essais, jusqu'à ~50 min au total.
            if attempt >= 10:
                seconds = max(seconds, 60)
            print(f"Quota Météo-France atteint, attente {seconds:.0f} s ({attempt + 1}/60).", flush=True)
            time.sleep(seconds)
            continue
        return response
    raise Quota("Quota Météo-France épuisé après 60 tentatives")


def latest_runs(session: requests.Session, key: str) -> dict[str, str]:
    """Dernier run (ex. 2026-10-04T03.00.00Z) de chaque produit retenu."""
    response = call(session, "GetCapabilities?service=WCS&version=2.0.1&language=eng", key)
    response.raise_for_status()
    # Le XML du catalogue n'est pas toujours bien formé (&, etc.) : extraction par expression régulière.
    ids = re.findall(r"<wcs:CoverageId>([^<]+)</wcs:CoverageId>", response.text)
    runs: dict[str, str] = {}
    for _short, prefix, *_ in PRODUCTS:
        stamps = sorted(i.split("___")[1] for i in ids if i.startswith(prefix + "___"))
        if stamps:
            runs[prefix] = stamps[-1]
    return runs


def describe_times(session: requests.Session, key: str, coverage: str) -> list[datetime]:
    """Instants disponibles d'une couverture (pas horaire entre beginPosition et endPosition)."""
    response = call(session, f"DescribeCoverage?service=WCS&version=2.0.1&coverageid={coverage}", key)
    response.raise_for_status()
    text = response.text
    begin = re.search(r"<gml:beginPosition[^>]*>([^<]+)<", text)
    end = re.search(r"<gml:endPosition[^>]*>([^<]+)<", text)
    if not begin or not end:
        raise ValueError("Période de la couverture introuvable")
    t0 = datetime.fromisoformat(begin[1].replace("Z", "+00:00"))
    t1 = datetime.fromisoformat(end[1].replace("Z", "+00:00"))
    out, t = [], t0
    while t <= t1:
        out.append(t)
        t += timedelta(hours=1)
    return out


def decode(content: bytes) -> np.ndarray:
    from eccodes import codes_get, codes_get_values, codes_new_from_message, codes_release

    handle = codes_new_from_message(content)
    try:
        ni, nj = codes_get(handle, "Ni"), codes_get(handle, "Nj")
        values = np.asarray(codes_get_values(handle), dtype=float).reshape(nj, ni)
        if codes_get(handle, "jScansPositively"):
            values = values[::-1]
        return values
    finally:
        codes_release(handle)


def already_published(url: str, run_iso: str) -> bool:
    """Run déjà publié en entier (une publication partielle, coupée par le quota, est complétée au passage suivant)."""
    try:
        r = requests.get(url, timeout=(10, 30), headers={"User-Agent": UA})
        if r.status_code != 200:
            return False
        index = r.json()
        return index.get("run_time") == run_iso and index.get("complete", True) is True
    except (requests.RequestException, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="build/pearome")
    parser.add_argument("--current-index-url", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--cache-dir", default=".cache/pearome")
    args = parser.parse_args()
    key = os.environ.get("METEOFRANCE_PAQUET_API_KEY", "")
    if not key:
        print("METEOFRANCE_PAQUET_API_KEY manquante", file=sys.stderr)
        return 1
    session = requests.Session()
    runs = latest_runs(session, key)
    if not runs:
        print("Aucun produit PEAROME dans le catalogue", file=sys.stderr)
        return 1
    missing = [p[0] for p in PRODUCTS if p[1] not in runs]
    if missing:
        print(f"Produits absents du catalogue : {missing}", file=sys.stderr)
    run_stamp = max(set(runs.values()), key=lambda s: sum(1 for v in runs.values() if v == s))
    run_iso = run_stamp.replace(".", ":")
    if not args.force and args.current_index_url and already_published(args.current_index_url, run_iso):
        print(f"Run {run_iso} déjà publié.")
        return 0
    run_dt = datetime.fromisoformat(run_iso.replace("Z", "+00:00"))
    out = Path(args.output_dir)
    (out / "products").mkdir(parents=True, exist_ok=True)

    # Produits déjà récupérés pour CE run lors d'un passage précédent (coupé par le quota) : réutilisés tels quels.
    cache = Path(args.cache_dir) / run_stamp
    cache.mkdir(parents=True, exist_ok=True)
    for old in Path(args.cache_dir).iterdir():  # runs précédents : inutiles
        if old.is_dir() and old.name != run_stamp:
            for f in old.iterdir():
                f.unlink()
            old.rmdir()

    products_meta = []
    shape: tuple[int, int] | None = None
    expected = [p for p in PRODUCTS if runs.get(p[1]) == run_stamp]
    partial = False
    for short, prefix, label, family, threshold in expected:
        cached = cache / f"{short}.json"
        if cached.is_file():
            try:
                saved = json.loads(cached.read_text(encoding="utf-8"))
                (out / "products" / f"{short}.json").write_text(json.dumps(saved["product"], separators=(",", ":")), encoding="utf-8")
                products_meta.append(saved["meta"])
                shape = shape or tuple(saved["shape"])
                print(f"{short}: repris du cache", flush=True)
                continue
            except (ValueError, KeyError, OSError):
                cached.unlink(missing_ok=True)
        try:
            result = fetch_product(session, key, run_stamp, run_dt, short, prefix, label, family, threshold, shape)
        except Quota as exc:
            # Quota épuisé : on publie ce qui a été obtenu, le passage suivant complétera grâce au cache.
            print(f"::warning::{exc} - publication partielle ({len(products_meta)}/{len(expected)} produits), suite au prochain passage.", flush=True)
            partial = True
            break
        if result is None:
            continue
        product, meta, shape = result
        (out / "products" / f"{short}.json").write_text(json.dumps(product, separators=(",", ":")), encoding="utf-8")
        cached.write_text(json.dumps({"product": product, "meta": meta, "shape": list(shape)}, separators=(",", ":")), encoding="utf-8")
        products_meta.append(meta)
    if not products_meta or shape is None:
        if partial:
            raise Quota("Quota Météo-France épuisé avant le premier produit")
        print("Aucun produit récupéré", file=sys.stderr)
        return 1
    return write_index(out, run_iso, shape, products_meta, complete=not partial)


def fetch_product(session, key, run_stamp, run_dt, short, prefix, label, family, threshold, shape):
    """Toutes les échéances d'un produit ; None si aucune n'est disponible."""
    coverage = f"{prefix}___{run_stamp}"
    times = describe_times(session, key, coverage)
    steps = [t for t in times if t > run_dt and int((t - run_dt).total_seconds() // 3600) % STEP_H == 0]
    entries = []
    for t in steps:
        stamp = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        response = call(
            session,
            f"GetCoverage?service=WCS&version=2.0.1&coverageid={coverage}&format=application%2Fwmo-grib"
            f"&subset=time({stamp})&subset=lat({LAT0},{LAT1})&subset=long({LON0},{LON1})",
            key,
        )
        if response.status_code != 200 or response.content[:4] != b"GRIB":
            # 404 : échéance sans donnée pour ce produit (les cumuls n'existent qu'à partir de leur durée).
            if response.status_code != 404:
                print(f"{short} {stamp}: HTTP {response.status_code}, ignoré", flush=True)
            continue
        grid = decode(response.content)
        shape = shape or grid.shape
        if grid.shape != shape:
            continue
        grid = np.clip(np.nan_to_num(grid, nan=0.0), 0, 100)
        entries.append(
            {
                "time": stamp,
                "lead": int((t - run_dt).total_seconds() // 3600),
                "max": round(float(grid.max()), 1),
                "mean": round(float(grid.mean()), 1),
                "data": base64.b64encode(np.rint(grid).astype(np.uint8).tobytes()).decode("ascii"),
            }
        )
    print(f"{short}: {len(entries)} échéances", flush=True)
    if not entries:
        return None
    product = {"id": short, "label": label, "family": family, "threshold": threshold, "steps": entries}
    meta = {
        "id": short,
        "label": label,
        "family": family,
        "threshold": threshold,
        "file": f"products/{short}.json",
        "max": max(e["max"] for e in entries),
        "steps": len(entries),
    }
    return product, meta, shape


def write_index(out: Path, run_iso: str, shape, products_meta, complete: bool) -> int:
    index = {
        "schema_version": 1,
        "status": "ok",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": {"name": "PEAROME (AROME ensemble)", "provider": "Météo-France", "resolution_km": 2.5},
        "run_time": run_iso,
        "unit": "% de probabilité de dépasser le seuil",
        "bounds": [[LAT0, LON0], [LAT1, LON1]],
        "height": shape[0],
        "width": shape[1],
        "step_hours": STEP_H,
        # False : publication coupée par le quota, complétée au passage suivant (voir already_published).
        "complete": complete,
        "products": products_meta,
    }
    (out / "index.json").write_text(json.dumps(index, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    print(f"PEAROME {run_iso} : {len(products_meta)} produits{'' if complete else ' (partiel)'}, grille {shape[1]}x{shape[0]}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Quota as exc:
        # Quota partagé avec les autres workflows : on garde les données publiées et on réessaiera au prochain cycle.
        print(f"::warning::{exc} - données précédentes conservées, nouvel essai au prochain cycle.")
        sys.exit(0)
