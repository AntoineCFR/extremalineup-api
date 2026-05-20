import logging
from firebase_admin import messaging
from bigquery import get_username_by_id

def send_sos_notification(sender_user_id):
    """Envoie une notification SOS à tous les utilisateurs (via FCM)."""
    try:
        sender_username = get_username_by_id(sender_user_id)
        message = messaging.Message(
            notification=messaging.Notification(
                title="🚨 SOS déclenché !",
                body=f"{sender_username} a besoin d'aide !"
            ),
            topic="all_users"  # Envoie à tous les utilisateurs abonnés
        )
        messaging.send(message)
    except Exception as e:
        logging.error(f"Erreur _send_sos_notification: {str(e)}")
        raise
