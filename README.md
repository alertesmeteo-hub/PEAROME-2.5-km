# PEAROME — probabilités AROME ensemble (Pyrénées-Orientales)

Pipeline Alertes Météo : récupère les produits de probabilité de PE-AROME 0,025° (Météo-France, API
WCS) pour le dernier run et publie, sur la branche `data`, un JSON compact par produit :

- `index.json` : run, fenêtre géographique, produits disponibles ;
- `products/<id>.json` : probabilité (0 à 100 %) de dépasser le seuil, un tableau par échéance (pas de 3 h).

Produits : rafales ≥ 40/50/70 km/h, pluie 24 h ≥ 20/50/80/120/200 mm, pluie 6 h ≥ 20/60/100 mm,
pluie 1 h ≥ 20 mm.

L'API ne fournit pas les membres un par un : ce sont des probabilités, pas des scénarios.

## Secret requis

`METEOFRANCE_PAQUET_API_KEY` : clé de l'application Météo-France abonnée à l'API PE-AROME
(secret GitHub du dépôt, jamais en dur). Le quota du portail est partagé avec les autres pipelines :
les appels sont espacés et repris sur erreur 429.
