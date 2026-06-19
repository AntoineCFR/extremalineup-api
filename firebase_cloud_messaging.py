import logging
from firebase_admin import messaging
from bigquery import (
    get_username_by_id,
    get_user_current_stage,
    get_now_playing_dj,
)


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
    Utilise le canal 'sos_channel' (Android) configuré avec une vibration longue."""
    try:
        sender_username = get_username_by_id(sender_user_id)
        message = messaging.Message(
            notification=messaging.Notification(
                title="🚨 SOS déclenché !",
                body=f"{sender_username} a besoin d'aide immédiatement !",
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
    except Exception as e:
        logging.error(f"Erreur send_sos_notification: {str(e)}")
        raise


def send_perdu_notification(sender_user_id):
    """Envoie une notification 'perdu' à tous les utilisateurs."""
    try:
        sender_username = get_username_by_id(sender_user_id)
        message = messaging.Message(
            notification=messaging.Notification(
                title="😵 Quelqu'un s'est perdu !",
                body=f"{sender_username} s'est perdu — pouvez-vous l'aider ?",
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
    except Exception as e:
        logging.error(f"Erreur send_perdu_notification: {str(e)}")
        raise


def send_hype_notification(sender_user_id, festival_id=None):
    """Envoie une notification 'hype' à tous les utilisateurs.
    Enrichit le message avec la SCÈNE de l'auteur (et le DJ qui y joue) si
    disponibles, pour que les autres sachent d'où vient la hype."""
    try:
        sender_username = get_username_by_id(sender_user_id)

        # Scène courante de l'auteur + DJ en train de jouer (best-effort).
        stage = get_user_current_stage(festival_id, sender_user_id) if festival_id else None
        dj = get_now_playing_dj(festival_id, stage) if (festival_id and stage) else None

        if stage and dj:
            body = f"{sender_username} kiffe sur {stage} — {dj} en train de jouer ! 🎵"
        elif stage:
            body = f"{sender_username} kiffe sur {stage} ! 🎵"
        else:
            body = f"{sender_username} : c'est incroyable ici ! 🎵"

        data = {"event_type": "hype", "user_id": str(sender_user_id)}
        if stage:
            data["stage"] = stage
        if dj:
            data["dj"] = dj

        message = messaging.Message(
            notification=messaging.Notification(
                title="🔥 HYPE ! 🔥",
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
    except Exception as e:
        logging.error(f"Erreur send_hype_notification: {str(e)}")
        raise
