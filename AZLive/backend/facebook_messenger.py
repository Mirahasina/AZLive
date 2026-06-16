import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

from .facebook_oauth import GRAPH_API_VERSION
from .models import PageFacebook

logger = logging.getLogger(__name__)


def send_facebook_private_message(page: PageFacebook, recipient_id: str, text: str) -> dict:
    if not page.access_token:
        return {'sent': False, 'error': 'Token page manquant.'}

    payload = {
        'recipient': json.dumps({'id': str(recipient_id)}),
        'message': json.dumps({'text': text}),
        'messaging_type': 'RESPONSE',
        'access_token': page.access_token,
    }
    data = urllib.parse.urlencode(payload).encode('utf-8')
    url = f'https://graph.facebook.com/{GRAPH_API_VERSION}/{page.page_id}/messages'
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': 'AZLive/1.0',
        },
        method='POST',
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode('utf-8'))
        return {'sent': True, 'channel': 'Facebook', 'message_id': body.get('message_id')}
    except urllib.error.HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode('utf-8'))
            message = error_payload.get('error', {}).get('message', str(error_payload))
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = str(exc)
        logger.warning('Messenger send failed page %s: %s', page.page_id, message)
        return {'sent': False, 'error': message, 'channel': 'Facebook'}
    except urllib.error.URLError as exc:
        logger.warning('Messenger network error page %s: %s', page.page_id, exc.reason)
        return {'sent': False, 'error': str(exc.reason), 'channel': 'Facebook'}
