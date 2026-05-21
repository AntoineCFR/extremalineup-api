import logging
from firebase_admin import messaging
from bigquery import get_username_by_id


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
        messaging.send(message)
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
        messaging.send(message)
        logging.info(f"Notification 'perdu' envoyée pour l'utilisateur {sender_user_id}.")
    except Exception as e:
        logging.error(f"Erreur send_perdu_notification: {str(e)}")
        raise


def send_hype_notification(sender_user_id):
    """Envoie une notification 'hype' à tous les utilisateurs."""
    try:
        sender_username = get_username_by_id(sender_user_id)
        message = messaging.Message(
            notification=messaging.Notification(
                title="🔥 HYPE ! 🔥",
                body=f"{sender_username} : c'est incroyable ici ! 🎵",
            ),
            data={
                "event_type": "hype",
                "user_id": str(sender_user_id),
            },
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
        messaging.send(message)
        logging.info(f"Notification 'hype' envoyée pour l'utilisateur {sender_user_id}.")
    except Exception as e:
        logging.error(f"Erreur send_hype_notification: {str(e)}")
        raise
