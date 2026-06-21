import logging
import random
from firebase_admin import messaging
from bigquery import (
    get_username_by_id,
    get_user_current_stage,
    get_now_playing_dj,
    get_hype_variant_cycle_state,
)


# Variantes du corps de la notification 'hype', par palier de données dispo.
# Chaque variante porte une CLÉ stable (sd*/s*/p*) qui sert au dédup : on
# journalise la clé envoyée et on ne réutilise pas une formule avant d'avoir fait
# le tour complet de son palier (cf. _pick_hype_variant).
# Placeholders : {stage}, {dj}. L'auteur·rice figure dans le TITRE
# ("🔥 HYPE ! 🔥 déclenchée par {user}"), donc les corps n'ont plus besoin de
# citer le nom. Formulations neutres (pas d'accord de genre). On évite 🔥 dans
# les corps (réservé au titre) au profit de 💥.
_HYPE_BODIES_STAGE_DJ = [
    ("sd1", "Dinguerie de l'année sur {stage} ! {dj} nous met bieeengggg 💥"),
    ("sd2", "Les verres volent sur {stage} 🍻 {dj} fait des ravages"),
    ("sd3", "Ça cartonne sur {stage} 💥 {dj} fait pas semblant"),
    ("sd4", "{dj} envoie du sale sur {stage} 🤯 c'est la folie totale"),
    ("sd5", "{dj} retourne {stage} 💥 banger sur banger sur certified banger"),
    ("sd6", "C'est illégal d'être aussi bien 🚨 {dj} régale sur {stage}"),
    ("sd7", "Alerte tuerie sur {stage} 🌋 {dj} met le feu, ça pousse de partout"),
    ("sd8", "{dj} sur {stage}, validé à 200% 💥 venez vite"),
    ("sd9", "{stage} sur une autre planète 🌍 {dj} nous emmène loin"),
    ("sd10", "{stage} est hors de contrôle 🌪️ {dj} envoie du lourd"),
    ("sd11", "{dj} fracasse {stage} 💥 grosse claque générale"),
    ("sd12", "Ça tabasse sur {stage} 💣 {dj} en mode machine"),
    ("sd13", "Home run pour {dj} ⚾ {stage} en orbite 🛰️"),
    ("sd14", "{stage} : this is your captain {dj} speaking 🧑‍✈️✈️"),
]
_HYPE_BODIES_STAGE = [
    ("s1", "Dinguerie de l'année sur {stage} ! 💥"),
    ("s2", "Les verres volent sur {stage} 🍻 c'est la folie"),
    ("s3", "{stage} part en cacahuète 🌪️ grosse ambiance"),
    ("s4", "Alerte tuerie sur {stage} 🌋 ça pousse de partout"),
    ("s5", "{stage} est hors de contrôle 💥 rappliquez"),
    ("s6", "{stage} : décollage en cours 🚀"),
]
_HYPE_BODIES_PLAIN = [
    ("p1", "Ça cartonne ici 💥 ambiance de folie"),
    ("p2", "C'est en train de partir sur une autre planète 🌍 grosse énergie"),
    ("p3", "C'est la folie ici 🌪️ ça fracasse"),
    ("p4", "Gros moment en cours 🤯 ça envoie du sale"),
]


def _pick_hype_variant(festival_id, tier):
    """Choisit une variante (clé, texte brut) dans un palier, sans réutiliser une
    formule avant d'avoir fait le tour complet du palier (dédup par cycle).

    Position dans le cycle courant = `count % P` (P = taille du palier) ; les `k`
    clés les plus récentes sont celles déjà utilisées dans ce cycle → on les
    exclut et on tire au hasard parmi le reste. En début de cycle (k == 0), tout
    le palier est candidat, sauf la toute dernière clé envoyée (pas de répétition
    dos à dos d'un cycle à l'autre). Best-effort : sans festival ou en cas d'échec
    BigQuery, simple tirage aléatoire sur tout le palier."""
    pool_keys = [k for k, _ in tier]
    P = len(tier)
    count, recent = get_hype_variant_cycle_state(festival_id, pool_keys) if festival_id else (0, [])

    k = count % P
    used = set(recent[:k])
    candidates = [(key, t) for key, t in tier if key not in used]
    if k == 0 and recent:      # nouveau cycle → éviter de répéter la dernière clé
        candidates = [(key, t) for key, t in tier if key != recent[0]] or tier
    return random.choice(candidates)


def _build_hype_body(festival_id, stage, dj):
    """Retourne (variant_key, body) selon les données dispo, avec dédup par cycle."""
    if stage and dj:
        key, tmpl = _pick_hype_variant(festival_id, _HYPE_BODIES_STAGE_DJ)
        return key, tmpl.format(stage=stage, dj=dj)
    if stage:
        key, tmpl = _pick_hype_variant(festival_id, _HYPE_BODIES_STAGE)
        return key, tmpl.format(stage=stage)
    key, tmpl = _pick_hype_variant(festival_id, _HYPE_BODIES_PLAIN)
    return key, tmpl


def send_topic_notification(title, body, data=None, channel_id="festival_channel", priority="normal"):
    """Envoi générique d'une notification au topic global `all_users`.
    Utilisé par les pushs programmés (journal). Lève en cas d'échec (l'appelant
    décide quoi en faire). Retourne le message_id."""
    try:
        payload_data = {"event_type": "journal"}
        if data:
            payload_data.update({k: str(v) for k, v in data.items()})
        apns_priority = "10" if priority == "high" else "5"
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=payload_data,
            android=messaging.AndroidConfig(
                priority=priority,
                notification=messaging.AndroidNotification(channel_id=channel_id),
            ),
            apns=messaging.APNSConfig(
                headers={"apns-priority": apns_priority},
                payload=messaging.APNSPayload(aps=messaging.Aps(sound="default", badge=1)),
            ),
            topic="all_users",
        )
        message_id = messaging.send(message)
        logging.info(f"FCM journal envoyé (message_id={message_id}) : {title}")
        return message_id
    except Exception as e:
        logging.error(f"Erreur send_topic_notification: {str(e)}")
        raise


def send_sos_notification(sender_user_id):
    """Envoie une alerte SOS à tous les utilisateurs.
    Utilise le canal 'sos_channel' (Android) configuré avec une vibration longue.
    Retourne (title, body) pour journalisation par l'appelant."""
    try:
        sender_username = get_username_by_id(sender_user_id)
        title = "🚨 SOS déclenché !"
        body = f"{sender_username} a besoin d'aide immédiatement !"
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data={
                "event_type": "sos",
                "user_id": str(sender_user_id),
            },
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="sos_channel",   # canal haute-priorité + vibration longue
                ),
            ),
            apns=messaging.APNSConfig(
                headers={"apns-priority": "10"},    # priorité immédiate iOS
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(sound="default", badge=1),
                ),
            ),
            topic="all_users",
        )
        message_id = messaging.send(message)
        logging.info(f"FCM envoyé (message_id={message_id})")
        logging.info(f"SOS notification envoyée pour l'utilisateur {sender_user_id}.")
        return title, body
    except Exception as e:
        logging.error(f"Erreur send_sos_notification: {str(e)}")
        raise


def send_perdu_notification(sender_user_id):
    """Envoie une notification 'perdu' à tous les utilisateurs.
    Retourne (title, body) pour journalisation par l'appelant."""
    try:
        sender_username = get_username_by_id(sender_user_id)
        title = "😵 Quelqu'un s'est perdu !"
        body = f"{sender_username} s'est perdu — pouvez-vous l'aider ?"
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data={
                "event_type": "perdu",
                "user_id": str(sender_user_id),
                # Demande aux appareils actifs/ouverts de remonter leur position
                # (l'app gère ça à la réception au premier plan ou au tap de la notif).
                "request_location": "true",
            },
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="festival_channel",
                ),
            ),
            apns=messaging.APNSConfig(
                headers={"apns-priority": "10"},
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(sound="default", badge=1),
                ),
            ),
            topic="all_users",
        )
        message_id = messaging.send(message)
        logging.info(f"FCM envoyé (message_id={message_id})")
        logging.info(f"Notification 'perdu' envoyée pour l'utilisateur {sender_user_id}.")
        return title, body
    except Exception as e:
        logging.error(f"Erreur send_perdu_notification: {str(e)}")
        raise


def send_hype_notification(sender_user_id, festival_id=None):
    """Envoie une notification 'hype' à tous les utilisateurs.
    Enrichit le message avec la SCÈNE de l'auteur (et le DJ qui y joue) si
    disponibles, pour que les autres sachent d'où vient la hype.
    Retourne (title, body, variant_key) pour journalisation par l'appelant."""
    try:
        sender_username = get_username_by_id(sender_user_id)

        # Scène courante de l'auteur + DJ en train de jouer (best-effort).
        stage = get_user_current_stage(festival_id, sender_user_id) if festival_id else None
        dj = get_now_playing_dj(festival_id, stage) if (festival_id and stage) else None

        variant_key, body = _build_hype_body(festival_id, stage, dj)
        title = f"🔥 HYPE ! 🔥 déclenchée par {sender_username}"

        data = {"event_type": "hype", "user_id": str(sender_user_id)}
        if stage:
            data["stage"] = stage
        if dj:
            data["dj"] = dj

        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data=data,
            android=messaging.AndroidConfig(
                priority="normal",
                notification=messaging.AndroidNotification(
                    channel_id="festival_channel",
                ),
            ),
            apns=messaging.APNSConfig(
                headers={"apns-priority": "5"},
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(sound="default", badge=1),
                ),
            ),
            topic="all_users",
        )
        message_id = messaging.send(message)
        logging.info(f"FCM envoyé (message_id={message_id})")
        logging.info(f"Notification 'hype' envoyée pour l'utilisateur {sender_user_id}.")
        return title, body, variant_key
    except Exception as e:
        logging.error(f"Erreur send_hype_notification: {str(e)}")
        raise
