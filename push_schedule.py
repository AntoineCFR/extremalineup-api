"""Pushs programmés du festival (« journal »).

Un unique cron externe (cron-job.org) appelle `GET /api/push/tick` toutes les
~5 min. À chaque tick, ce module regarde l'heure LOCALE du festival, détermine
les créneaux dus et pas encore partis (anti-doublon via la table notifications),
calcule les « gagnant·e·s » et envoie/journalise les notifications.

Tout le planning et les textes vivent ici → modifiables sans toucher au cron.
"""

import logging
import random
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo

import bigquery as bq
import firebase_cloud_messaging as fcm

logger = logging.getLogger(__name__)

# ── Thèmes ───────────────────────────────────────────────────────────────────
TRENDING = "trending"
LOST = "lost"  # vanne sur le·la + « perdu » la veille (jadis « airtag »)
BAR = "bar"
HYDRATION = "hydration"
ENERGY = "energy"
HYPE = "hype"
JURE = "jure"
FOMO = "fomo"

# Fenêtre de rattrapage : un créneau manqué de plus de N min n'est PAS poussé
# (juste journalisé), pour éviter un envoi tardif/spam si le cron a calé.
CATCH_UP_MIN = 30
CLOSING_TIME = "10:00"  # heure locale de la notif de clôture (lendemain du festival)

# ── Décompte avant le festival ────────────────────────────────────────────────
# Notifs « J-N » qui montent en intensité à l'approche. Chacune part UNE fois,
# le jour J-N atteint, à partir de COUNTDOWN_TIME (heure locale). Pas de fenêtre
# de rattrapage : si le jour J-N est en cours et l'heure passée, ça part au tick
# suivant → testable en live dès qu'un palier tombe sur aujourd'hui.
COUNTDOWN_TIME = "09:00"
COUNTDOWN_DAYS = [30, 21, 14, 10, 7, 5, 3, 2, 1]

# ── Planning (heures locales du festival) ─────────────────────────────────────
# variant 'd' = texte du Trending choisi selon l'ordinal du jour.
SCHEDULE_FIRST = [  # 1er jour : pas de « veille » → seulement Trending / FOMO / Juré
    {"time": "08:00", "theme": TRENDING, "variant": "d"},
    {"time": "11:00", "theme": FOMO, "variant": "7B"},
    {"time": "13:00", "theme": JURE, "variant": "6A"},
]
SCHEDULE_FULL_A = [  # jour intermédiaire (ex. samedi)
    {"time": "08:00", "theme": TRENDING, "variant": "d"},
    {"time": "09:00", "theme": LOST, "variant": "1B"},
    {"time": "09:40", "theme": BAR, "variant": "2B"},
    {"time": "10:20", "theme": HYPE, "variant": "5A"},
    {"time": "11:00", "theme": FOMO, "variant": "7A"},
    {"time": "11:40", "theme": HYDRATION, "variant": "3B"},
    {"time": "12:20", "theme": ENERGY, "variant": "4B"},
    {"time": "13:00", "theme": JURE, "variant": "6B"},
]
SCHEDULE_FULL_B = [  # dernier jour (ex. dimanche) : ordre des heures permuté
    {"time": "08:00", "theme": TRENDING, "variant": "d"},
    {"time": "09:00", "theme": HYPE, "variant": "5C"},
    {"time": "09:40", "theme": ENERGY, "variant": "4C"},
    {"time": "10:20", "theme": FOMO, "variant": "7C"},
    {"time": "11:00", "theme": BAR, "variant": "2C"},
    {"time": "11:40", "theme": LOST, "variant": "1C"},
    {"time": "12:20", "theme": HYDRATION, "variant": "3C"},
    {"time": "13:00", "theme": JURE, "variant": "6C"},
]


def _slots_for_ordinal(ordinal, n_days):
    """Planning du jour selon son rang : 0 = premier (léger), dernier = FULL_B,
    intermédiaires = FULL_A."""
    if ordinal <= 0:
        return SCHEDULE_FIRST
    if ordinal >= n_days - 1:
        return SCHEDULE_FULL_B
    return SCHEDULE_FULL_A


# ── Utilitaires temps / genre / gagnant·e ─────────────────────────────────────

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _weekday_name(d):
    return _WEEKDAYS[d.weekday()]


def _local_day_window_utc(tz, d):
    """Bornes UTC (aware) d'une journée LOCALE [d 00:00, d+1 00:00)."""
    start_local = datetime.combine(d, time(0, 0), tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _g(gender, masc, fem, neutral=None):
    """Forme genrée : 'm' → masc, 'f' → fem, inconnu → neutral ou « masc/fem »."""
    if gender == "m":
        return masc
    if gender == "f":
        return fem
    return neutral if neutral is not None else f"{masc}/{fem}"


def _enrich(uid):
    return {"user_id": uid, "name": bq.get_username_by_id(uid), "gender": bq.get_user_gender(uid)}


def _pick(agg):
    """agg = {user_id: score}. Retourne le·la gagnant·e enrichi·e (égalité → au
    hasard), ou None si vide / scores nuls."""
    if not agg:
        return None
    mx = max(agg.values())
    if mx <= 0:
        return None
    candidates = [u for u, s in agg.items() if s == mx]
    return _enrich(random.choice(candidates))


def _winner_from_counts(counts, weights):
    """counts = [{user_id, event_type, n}]. weights = {event_type: poids}."""
    agg = {}
    for c in counts:
        w = weights.get(c["event_type"])
        if w:
            agg[c["user_id"]] = agg.get(c["user_id"], 0) + w * c["n"]
    return _pick(agg)


def _top_user(rows):
    """rows = [{user_id, n}]."""
    return _pick({r["user_id"]: r["n"] for r in rows})


def _join_djs(djs):
    """['A','B','C'] → 'A, B & C'."""
    if not djs:
        return ""
    if len(djs) == 1:
        return djs[0]
    return ", ".join(djs[:-1]) + " & " + djs[-1]


# ── Textes par thème (title, body) ────────────────────────────────────────────

_ALCOHOL = {"demi_alcool": 0.5, "alcool": 1.0}
_ENERGY = {"quart_energie": 1.0, "demi_energie": 1.0, "pleine_energie": 1.0}


def _trending_title(day_name):
    return {
        "friday": "Freaky Friday ! 🎉",
        "saturday": "Super Saturday ! ⚡",
        "sunday": "Sunday Funday ! 🌞",
    }.get(day_name, "C'est parti ! 🎶")


def _trending_text(ordinal, day_name, djs):
    title = _trending_title(day_name)
    spotlight = _join_djs(djs)
    if not spotlight:
        return title, ("Le classement dort encore 😴 Notez vos sets pour réveiller "
                       "les coups de cœur du groupe ! 🎧")
    if ordinal <= 0:
        body = (f"Premier jour, premières basses 🔊 Aujourd'hui sous les projecteurs : "
                f"{spotlight}. Échauffez les nuques (et les gourdes 💧).")
    elif ordinal == 1:
        body = (f"On remet ça, et plus fort ⚡ Les chouchous du groupe aujourd'hui : "
                f"{spotlight}. Réveil tout en douceur, journée tout en feu. 🔥")
    else:
        body = (f"Dernier round, zéro regret 🌞 À l'affiche aujourd'hui : "
                f"{spotlight}. Donnez tout — la récup', c'est un problème de lundi.")
    return title, body


def _lost_text(variant, w):
    title = "Avis de recherche 📍"
    if not w:
        return title, ("Personne perdu hier ?! 👀 La team progresse… ou personne "
                       "n'ose l'avouer. Déclarez vos égarements sur Events !")
    n, g = w["name"], w["gender"]
    if variant == "1C":
        body = (f"{n}, notre GPS humain le moins fiable 🧭 ({_g(g, 'roi', 'reine')} "
                f"du « perdu » hier). {_g(g, 'Gardez-le', 'Gardez-la', 'Gardez-le/la')} à vue !")
    else:  # 1B
        body = (f"Avis de recherche préventif sur {n} 🔍 : record de « perdu » hier. "
                f"On lui colle un AirTag ou bien ?")
    return title, body


def _bar_text(variant, w, stage):
    title = "Au bar ! 🍺"
    if not w:
        return title, ("Bizarrement, personne n'a abusé hier 🍺… Suspect. Déclarez "
                       "vos consos sur Events (avec modération) !")
    n, g = w["name"], w["gender"]
    loc = stage or "bar central"
    if variant == "2C":
        body = (f"{n} vous attend probablement à {loc}, un verre à la main 🥂 "
                f"({_g(g, 'champion', 'championne')} de l'apéro hier). Les habitudes "
                f"ont la vie dure.")
    else:  # 2B
        body = (f"Point info : {n} a élu domicile au bar de {loc} 🍻 (record de "
                f"verres hier). Passez lui faire un coucou.")
    return title, body


def _hydration_text(variant, w):
    title = "Champion·ne de l'eau 💧"
    if not w:
        return title, ("Alerte sécheresse 🏜️ : zéro verre d'eau déclaré hier. Buvez "
                       "(de l'eau !) et notez-le sur Events.")
    n, g = w["name"], w["gender"]
    if variant == "3C":
        body = (f"{n} carbure à l'eau plate 🚰 (record d'hydratation hier). Le "
                f"festival a {_g(g, 'son', 'sa')} pro de la récup'.")
    else:  # 3B
        body = (f"Élève modèle du jour : {n} 💧 (plus d'eau bue hier). Prenez-en de "
                f"la graine.")
    return title, body


def _energy_text(variant, w):
    title = "Pleine charge ⚡"
    if not w:
        return title, ("Batteries à plat ? 🪫 Aucun boost déclaré hier. Rechargez et "
                       "notez-le sur Events !")
    n = w["name"]
    if variant == "4C":
        body = (f"Niveau d'énergie maximal détecté : {n} ⚡ (plus de boosts hier). "
                f"Suivez le mouvement… ou pas.")
    else:  # 4B
        body = (f"{n} a rechargé les batteries à fond hier 🔋. Branchez-vous dessus "
                f"pour tenir la journée.")
    return title, body


def _hype_text(variant, w):
    title = "Ambiance 🔥"
    if not w:
        return title, ("Ambiance plate hier ? 😴 Aucun « hype » déclaré. Secouez-vous "
                       "et balancez vos « hype » sur Events !")
    n, g = w["name"], w["gender"]
    if variant == "5C":
        body = (f"Détecteur d'ambiance : {n} est en surchauffe 🌋 "
                f"({_g(g, 'champion', 'championne')} du « hype » hier). Calez-vous sur "
                f"son radar.")
    else:  # 5A
        body = (f"{_g(g, 'Ambianceur', 'Ambianceuse')} {_g(g, 'officiel', 'officielle')} "
                f"du jour : {n} 🔥 (plus de « hype » hier). "
                f"{_g(g, 'Suivez-le', 'Suivez-la', 'Suivez-le/la')}, c'est là que ça se passe.")
    return title, body


def _jure_text(variant, w):
    title = "Juré·e des Tendances 🎧"
    if not w:
        return title, ("Les Tendances ont besoin de vous ! 🎧 Notez vos sets pour "
                       "faire vivre le classement du groupe.")
    n, g = w["name"], w["gender"]
    if variant == "6B":
        body = (f"{_g(g, 'Le', 'La')} {_g(g, 'critique en chef', 'critique en cheffe')}, "
                f"c'est {n} 🎧 (plus de sets notés). Le classement n'a aucun secret "
                f"pour {_g(g, 'lui', 'elle')}.")
    elif variant == "6C":
        body = (f"{n} a écouté et jugé plus que quiconque 🎚️. Si un set grimpe dans "
                f"les Tendances, c'est un peu grâce à {_g(g, 'lui', 'elle')}.")
    else:  # 6A
        body = (f"{n}, {_g(g, 'juré', 'jurée')} {_g(g, 'officiel', 'officielle')} des "
                f"Tendances 🎧 : ses notes font la pluie et le beau temps du classement.")
    return title, body


def _fomo_text(variant, w):
    title = "FOMO du jour ⭐"
    if not w:
        return title, ("Encore aucun favori coché ? ⭐ Préparez votre planning sur le "
                       "line-up, les meilleurs sets n'attendent pas !")
    n, g = w["name"], w["gender"]
    if variant == "7B":
        body = (f"Programme le plus chargé du jour : celui de {n} 📋 "
                f"({_g(g, 'champion', 'championne')} des favoris). Le repos, c'est pour "
                f"les faibles.")
    elif variant == "7C":
        body = (f"{n} a coché la moitié du line-up ⭐ (plus de favoris du jour). FOMO "
                f"niveau expert.")
    else:  # 7A
        body = (f"{n} veut TOUT voir aujourd'hui 🤯 (record de favoris du jour). Bon "
                f"courage pour suivre le rythme.")
    return title, body


def _build_text(festival, theme, variant, ordinal, tz, today, day_name, prev_day_name):
    """Calcule (title, body) d'un créneau (résout le·la gagnant·e via BigQuery)."""
    fid = festival["festival_id"]

    if theme == TRENDING:
        return _trending_text(ordinal, day_name, bq.get_trending_djs(fid, day=day_name, limit=3))
    if theme == JURE:
        return _jure_text(variant, _top_user(bq.get_rating_count_by_user(fid, day=day_name)))
    if theme == FOMO:
        return _fomo_text(variant, _top_user(bq.get_favorite_count_by_user(fid, day=day_name)))

    # Thèmes events → fenêtre de la veille (locale).
    start_utc, end_utc = _local_day_window_utc(tz, today - timedelta(days=1))
    counts = bq.get_event_counts(fid, start_utc, end_utc)
    if theme == LOST:
        return _lost_text(variant, _winner_from_counts(counts, {"perdu": 1.0}))
    if theme == HYDRATION:
        return _hydration_text(variant, _winner_from_counts(counts, {"eau": 1.0}))
    if theme == ENERGY:
        return _energy_text(variant, _winner_from_counts(counts, _ENERGY))
    if theme == HYPE:
        return _hype_text(variant, _winner_from_counts(counts, {"hype": 1.0}))
    if theme == BAR:
        w = _winner_from_counts(counts, _ALCOHOL)
        stage = None
        if w:
            stage = (bq.get_user_top_fav_stage(fid, w["user_id"], day=prev_day_name)
                     or bq.get_main_stage(fid))
        return _bar_text(variant, w, stage)
    raise ValueError(f"Thème inconnu : {theme}")


# ── Orchestration (appelée par /api/push/tick) ────────────────────────────────

def run_tick(festival):
    """Envoie/journalise les notifications dues pour ce festival, à l'instant présent."""
    fid = festival["festival_id"]
    tz = ZoneInfo(festival["timezone"])
    now_local = datetime.now(tz)
    start = date.fromisoformat(festival["start_date"])
    end = date.fromisoformat(festival["end_date"])
    today = now_local.date()
    n_days = (end - start).days + 1

    fired = []
    if start <= today <= end:
        ordinal = (today - start).days
        for slot in _slots_for_ordinal(ordinal, n_days):
            res = _maybe_fire_slot(festival, tz, now_local, today, ordinal, slot)
            if res:
                fired.append(res)
    elif today == end + timedelta(days=1):
        fired.extend(_maybe_fire_closing(festival, tz, now_local, today, n_days))
    elif today < start:
        fired.extend(_maybe_fire_countdown(festival, tz, now_local, today, start))
    else:
        return {"status": "idle", "reason": "hors période festival"}

    return {"status": "ok", "festival_id": fid, "fired": fired}


def _countdown_text(festival, days_until):
    """(title, body) du palier J-N. Certains paliers piochent les coups de cœur
    du moment (classement bayésien, tout le festival) — utile et testable avant J."""
    fid = festival["festival_id"]
    name = festival.get("name") or "le festival"
    city = festival.get("city")
    here = f" à {city}" if city else ""
    title = f"J-{days_until} ⏳"

    if days_until == 30:
        body = (f"Un mois. UN. 🎶 {name} pointe le bout de son nez — l'heure de "
                f"speed-dater le line-up dans l'appli.")
    elif days_until == 21:
        body = ("3 semaines, le compte à rebours est lancé ⏱️ T'as noté tes sets, "
                "ou tu improvises comme un sauvage ?")
    elif days_until == 14:
        body = (f"La playlist chauffe 🎒 Deux semaines pour réviser tes classiques. "
                f"(La météo de {name} débarque bientôt dans l'appli.)")
    elif days_until == 10:
        body = (f"Plus que 10 dodos 🔟 Affûte tes favoris, {name} arrive à grands pas.")
    elif days_until == 7:
        djs = bq.get_trending_djs(fid, day=None, limit=3)
        spot = f" Coups de cœur du groupe : {_join_djs(djs)}." if djs else ""
        body = (f"UNE SEMAINE 🔥 Dernière ligne droite, coche tes incontournables "
                f"sur le line-up.{spot}")
    elif days_until == 5:
        body = (f"La météo de {name} se précise dans l'appli 🌡️ — reste à prier le "
                f"dieu de la techno pour le soleil.")
    elif days_until == 3:
        body = ("Plus que 3 dodos ! 🎉 Batterie externe ✅ scènes repérées ✅ "
                "excuse pour poser ton vendredi ✅.")
    elif days_until == 2:
        body = ("48 h ⏰ Covoit, cash, bouchons d'oreilles… et zéro regret. "
                "On y est presque.")
    else:  # 1
        djs = bq.get_trending_djs(fid, day=None, limit=3)
        spot = f" Le top du groupe : {_join_djs(djs)}." if djs else ""
        body = (f"C'EST DEMAIN 🤩 Favoris ✅ planning ✅ équipe ✅ foie 🫣. "
                f"Rendez-vous{here} !{spot}")
    return title, body


def _maybe_fire_countdown(festival, tz, now_local, today, start):
    """Avant le festival : envoie le palier J-N du jour (une seule fois)."""
    fid = festival["festival_id"]
    days_until = (start - today).days
    if days_until not in COUNTDOWN_DAYS:
        return []

    hh, mm = (int(x) for x in COUNTDOWN_TIME.split(":"))
    scheduled = datetime.combine(today, time(hh, mm), tzinfo=tz)
    if now_local < scheduled:
        return []

    slot_key = f"{today.isoformat()}|countdown|{days_until}"
    if bq.notification_exists(fid, slot_key):
        return []

    title, body = _countdown_text(festival, days_until)
    try:
        fcm.send_topic_notification(title, body, channel_id="festival_channel")
    except Exception as e:
        logger.error(f"Push countdown J-{days_until} échouée, retry au prochain tick: {e}")
        return []
    bq.insert_notification(fid, slot_key, None, COUNTDOWN_TIME, "countdown",
                           title, body, True)
    return [{"slot_key": slot_key, "pushed": True, "title": title}]


def _maybe_fire_slot(festival, tz, now_local, today, ordinal, slot):
    fid = festival["festival_id"]
    hh, mm = (int(x) for x in slot["time"].split(":"))
    scheduled = datetime.combine(today, time(hh, mm), tzinfo=tz)
    if now_local < scheduled:
        return None  # pas encore l'heure

    slot_key = f"{today.isoformat()}T{slot['time']}|{slot['theme']}"
    if bq.notification_exists(fid, slot_key):
        return None  # déjà parti

    day_name = _weekday_name(today)
    prev_day_name = _weekday_name(today - timedelta(days=1))
    try:
        title, body = _build_text(festival, slot["theme"], slot["variant"], ordinal,
                                   tz, today, day_name, prev_day_name)
    except Exception as e:
        logger.error(f"Erreur construction texte ({slot}): {e}")
        return None

    late = (now_local - scheduled) > timedelta(minutes=CATCH_UP_MIN)
    pushed = False
    if late:
        # Créneau manqué (cron calé) : on journalise sans pousser (pas de spam tardif).
        bq.insert_notification(fid, slot_key, day_name, slot["time"], slot["theme"],
                               title, body, False)
        return {"slot_key": slot_key, "pushed": False, "late": True, "title": title}

    try:
        fcm.send_topic_notification(title, body, channel_id="festival_channel")
        pushed = True
    except Exception as e:
        # Échec d'envoi : NE PAS journaliser → on retentera au prochain tick.
        logger.error(f"Push échouée ({slot_key}), retry au prochain tick: {e}")
        return None

    bq.insert_notification(fid, slot_key, day_name, slot["time"], slot["theme"],
                           title, body, pushed)
    return {"slot_key": slot_key, "pushed": pushed, "late": False, "title": title}


def _maybe_fire_closing(festival, tz, now_local, today, n_days):
    """Lendemain du festival : 1 notif groupée poussée + le palmarès (journal seul)."""
    fid = festival["festival_id"]
    hh, mm = (int(x) for x in CLOSING_TIME.split(":"))
    scheduled = datetime.combine(today, time(hh, mm), tzinfo=tz)
    if now_local < scheduled:
        return []

    fired = []
    name = festival.get("name") or "Le festival"

    # 1) Notif de clôture (poussée)
    closing_key = f"{today.isoformat()}|closing"
    if not bq.notification_exists(fid, closing_key):
        title = f"{name}, c'est plié ! 🎉"
        body = (f"Merci pour ces {n_days} jours de folie. Le palmarès final du "
                f"week-end vous attend dans l'appli → Tendances & Journal.")
        try:
            fcm.send_topic_notification(title, body, channel_id="festival_channel")
            bq.insert_notification(fid, closing_key, None, CLOSING_TIME, "closing",
                                   title, body, True)
            fired.append({"slot_key": closing_key, "pushed": True})
        except Exception as e:
            logger.error(f"Push clôture échouée: {e}")

    # 2) Palmarès (journal seulement, jamais poussé)
    for entry in _palmares_entries(festival, tz, start_window=True):
        key = f"{today.isoformat()}|palmares|{entry['key']}"
        if bq.notification_exists(fid, key):
            continue
        bq.insert_notification(fid, key, None, None, "palmares",
                               entry["title"], entry["body"], False)
        fired.append({"slot_key": key, "pushed": False})

    return fired


def _palmares_entries(festival, tz, start_window=True):
    """Construit les lignes de palmarès (calculs sur TOUT le festival)."""
    fid = festival["festival_id"]
    start = date.fromisoformat(festival["start_date"])
    end = date.fromisoformat(festival["end_date"])
    start_utc = _local_day_window_utc(tz, start)[0]
    end_utc = _local_day_window_utc(tz, end)[1]
    counts = bq.get_event_counts(fid, start_utc, end_utc)

    entries = []

    djs = bq.get_trending_djs(fid, day=None, limit=1)
    if djs:
        entries.append({"key": "dj", "title": "🏆 DJ du week-end",
                        "body": f"{djs[0]} — meilleure note du groupe."})

    jure = _top_user(bq.get_rating_count_by_user(fid))
    if jure:
        entries.append({"key": "jure", "title": "🎧 Juré·e n°1",
                        "body": f"{jure['name']} — le plus de sets notés."})

    fomo = _top_user(bq.get_favorite_count_by_user(fid))
    if fomo:
        entries.append({"key": "fomo", "title": "⭐ Plus FOMO",
                        "body": f"{fomo['name']} — le plus de favoris."})

    drinker = _winner_from_counts(counts, _ALCOHOL)
    if drinker:
        entries.append({"key": "bar", "title": "🍺 Plus gros·se buveur·euse",
                        "body": f"{drinker['name']} — santé !"})

    lost = _winner_from_counts(counts, {"perdu": 1.0})
    if lost:
        g = lost["gender"]
        entries.append({"key": "perdu", "title": "🧭 " + _g(g, "Roi", "Reine") + " du « perdu »",
                        "body": f"{lost['name']} — on t'a retrouvé·e, c'est l'essentiel."})

    water = _winner_from_counts(counts, {"eau": 1.0})
    if water:
        entries.append({"key": "eau", "title": "💧 Champion·ne hydratation",
                        "body": f"{water['name']} — la sagesse incarnée."})

    hype = _winner_from_counts(counts, {"hype": 1.0})
    if hype:
        entries.append({"key": "hype", "title": "🔥 Ambianceur·euse du week-end",
                        "body": f"{hype['name']} — merci pour l'énergie !"})

    entries.append({"key": "fin", "title": "💜 À l'année prochaine",
                    "body": "Merci pour ce week-end, les p'tits potes. On remet ça l'an prochain !"})
    return entries
