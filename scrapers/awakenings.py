"""Adaptateur de scraping Awakenings Festival 2026 (festival_id = 2).

Porté depuis extremalineup-backend. Expose l'interface attendue par le registre
`scrapers/__init__.py` :
  - scrape_rows(festival)            -> lignes normalisées (UTC), SANS bio
  - enrich_new_bios(festival, df, mask) -> remplit la bio des NOUVEAUX sets

La page expose tous les jours sur une seule URL, rendue côté serveur. On combine
l'heure (HH:MM) avec la date réelle du jour, gère les b2b et les after-parties
(set « du petit matin » rattaché à la nuit).
"""

import re
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

AWAKENINGS_URL = (
    'https://www.awakenings.com/en/events/2026/07/awakenings-festival/378057/'
)
FESTIVAL_ID = 2
TIMEZONE = ZoneInfo('Europe/Amsterdam')  # Hilvarenbeek, NL

# En-tête de jour (texte de h2.blockTitle__title) -> (date réelle, day_int).
DAY_DATES = {
    'FRIDAY':   (date(2026, 7, 10), 1),
    'SATURDAY': (date(2026, 7, 11), 2),
    'SUNDAY':   (date(2026, 7, 12), 3),
}

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/120.0 Safari/537.36'
    )
}

_TIME_RE = re.compile(r'(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})')


def _pad(hhmm: str) -> str:
    h, m = hhmm.split(':')
    return f'{int(h):02d}:{int(m):02d}'


def _to_utc(naive_local: datetime) -> datetime:
    """Heure locale (Amsterdam) -> UTC naïf (ce que stocke BigQuery ; l'API
    réapplique le décalage du fuseau à la lecture)."""
    return (
        naive_local.replace(tzinfo=TIMEZONE)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )


def _split_artists(dj: str) -> list[str]:
    """Reconstitue la liste d'artistes d'un set à partir du libellé combiné."""
    return [p.strip() for p in str(dj).split(' & ') if p.strip()]


def _bio_for_artists(artists: list[str], bios: dict) -> str:
    """Bio d'un set : pour un solo, la bio de l'artiste ; pour un b2b, les bios
    disponibles concaténées et préfixées du nom de l'artiste."""
    available = [(a, bios.get(a)) for a in artists if bios.get(a)]
    if not available:
        return ''
    if len(available) == 1 and len(artists) == 1:
        return available[0][1]
    return "\n\n".join(f"{a} — {b}" for a, b in available)


def get_awakenings_timetable(bios: dict | None = None) -> list[dict]:
    """Scrape le line-up complet et retourne une liste de dicts prêts pour la
    table `timetable` (sans set_id ni festival_id). Si `bios` ({nom: bio}) est
    fourni, chaque set reçoit sa bio (sinon bio='')."""
    resp = requests.get(AWAKENINGS_URL, headers=_HEADERS, timeout=40)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')

    acts: list[dict] = []
    current = None  # (day_name_lower, day_date, day_int)
    stage_index = 0

    elements = soup.select(
        'h2.blockTitle__title, div.layoutItem__lineup--multiple-stage-item'
    )
    for el in elements:
        classes = el.get('class') or []

        if 'blockTitle__title' in classes:
            label = el.get_text(strip=True).upper()
            if label in DAY_DATES:
                day_date, day_int = DAY_DATES[label]
                current = (label.lower(), day_date, day_int)
                stage_index = 0
            continue

        if current is None:
            continue
        day_name, day_date, day_int = current

        stage_el = el.select_one('.layoutItem__lineup--multiple-stage-title')
        if stage_el is None:
            continue
        stage = stage_el.get_text(strip=True).title()
        stage_order = stage_index
        stage_index += 1

        for row in el.select('.layoutItem__lineup--multiple-items-artists'):
            dates_el = row.select_one('.layoutItem__lineup--multiple-items-dates')
            if dates_el is None:
                continue
            match = _TIME_RE.search(' '.join(dates_el.get_text().split()))
            if not match:
                continue
            start_t = time.fromisoformat(_pad(match.group(1)))
            end_t = time.fromisoformat(_pad(match.group(2)))

            artists = [
                a.get_text(strip=True)
                for a in row.select('a.layoutItem__lineup--multiple-items-artists-link')
            ]
            dj = ' & '.join(a for a in artists if a)
            if not dj:
                continue

            bio = _bio_for_artists(artists, bios) if bios else ''

            # Seuil afterparty : avant 9h -> nuit précédente (même night header,
            # date calendaire réelle = lendemain) ; 9h pile ou après -> jour même.
            base = day_date + timedelta(days=1) if start_t.hour < 9 else day_date
            start_local = datetime.combine(base, start_t)
            end_local = datetime.combine(base, end_t)
            if end_local <= start_local:
                end_local += timedelta(days=1)

            acts.append({
                'dj': dj,
                'stage': stage,
                'host': '',
                'day': day_name,
                'day_int': day_int,
                'stage_order': stage_order,
                'bio': bio,
                'start_time': _to_utc(start_local),
                'end_time': _to_utc(end_local),
            })

    return acts


def get_artist_links() -> dict:
    """{nom_artiste: url_page_detail} pour tous les artistes du line-up."""
    resp = requests.get(AWAKENINGS_URL, headers=_HEADERS, timeout=40)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    links: dict[str, str] = {}
    for a in soup.select('a.layoutItem__lineup--multiple-items-artists-link'):
        name = a.get_text(strip=True)
        href = a.get('href')
        if name and href:
            links[name] = href
    return links


def _extract_bio(html: str) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    el = soup.select_one('p.layoutItem__text')
    return el.get_text(strip=True) if el else ''


def get_artist_bios(only: set | None = None, delay: float = 0.4) -> dict:
    """{nom_artiste: bio} en ouvrant la fiche de chaque artiste. Si `only` est
    fourni, on ne récupère QUE ces artistes (sert à n'enrichir que les nouveaux
    sets, sans re-scraper tout le line-up)."""
    links = get_artist_links()
    if only is not None:
        links = {n: u for n, u in links.items() if n in only}
    bios: dict[str, str] = {}
    for name, url in links.items():
        try:
            page = requests.get(url, headers=_HEADERS, timeout=40)
            page.raise_for_status()
            bios[name] = _extract_bio(page.text)
        except Exception:
            bios[name] = ''
        _time.sleep(delay)
    return bios


# --- Interface attendue par le registre ------------------------------------

def scrape_rows(festival) -> list[dict]:
    """Lignes normalisées (UTC) SANS bio — rapide (une seule page)."""
    return get_awakenings_timetable()


def enrich_new_bios(festival, df, mask) -> None:
    """Remplit la colonne `bio` des lignes NOUVELLES (mask=True) en n'allant
    chercher que les bios des artistes concernés. Modifie `df` en place."""
    if not bool(mask.any()):
        return
    new_names: set[str] = set()
    for dj in df.loc[mask, 'dj']:
        new_names.update(_split_artists(dj))
    bios = get_artist_bios(only=new_names)
    if 'bio' not in df.columns:
        df['bio'] = ''
    df.loc[mask, 'bio'] = df.loc[mask, 'dj'].apply(
        lambda dj: _bio_for_artists(_split_artists(dj), bios)
    )
