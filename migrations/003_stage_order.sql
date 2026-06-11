-- ============================================================================
-- Migration 003 — Ordre d'affichage des scènes (stage_order)
--
-- Ajoute une colonne `stage_order` (INT64) à `timetable` : l'ordre dans lequel
-- les scènes apparaissent sur le site du festival (0 = première, etc.).
-- Le frontend trie les scènes par cette valeur → on respecte la mise en page
-- voulue du festival (ex. "AREA N (CAMPING AFTER)" en dernier), au lieu d'un
-- tri alphabétique.
--
-- NB : les lignes existantes (Extrema, festival 1) auront stage_order = NULL.
-- Le frontend bascule alors sur un tri alphabétique insensible à la casse pour
-- ces lignes — aucun besoin de re-scraper l'Extrema (ce qui réassignerait les
-- set_id et orphelinerait les favoris).
--
-- À lancer AVANT de re-uploader le line-up Awakenings (main_awakenings.py).
-- ============================================================================

ALTER TABLE `extremalineup.dataset.timetable`
  ADD COLUMN IF NOT EXISTS stage_order INT64;

-- Vérification
-- SELECT festival_id, stage, stage_order FROM `extremalineup.dataset.timetable`
-- ORDER BY festival_id, stage_order LIMIT 20;
