"""Registre des adaptateurs de scraping, par festival_id.

Un adaptateur expose :
  - scrape_rows(festival) -> list[dict]      (lignes normalisées UTC, sans bio)
  - enrich_new_bios(festival, df, mask)      (remplit la bio des nouveaux sets)

L'endpoint /api/admin/refresh-lineup ignore les festivals sans adaptateur (et
ceux dont la date de fin est passée), donc ajouter un festival = ajouter une
entrée ici, sans toucher au reste.
"""

from . import awakenings

# festival_id -> module adaptateur. Extrema (1) est terminé : pas d'adaptateur.
SCRAPERS = {
    2: awakenings,
}
