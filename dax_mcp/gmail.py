import base64
import json
import urllib.error
import urllib.parse
import urllib.request

_BASE = 'https://gmail.googleapis.com/gmail/v1/users/me'


def _get(token, path, params=None):
    url = _BASE + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'Gmail API {e.code}: {body}')


def search_threads(token, query, max_results=10):
    data = _get(token, '/threads', {'q': query, 'maxResults': max_results})
    return [{'id': t['id'], 'snippet': t.get('snippet', '')}
            for t in data.get('threads', [])]


def _extract_plain_text(payload):
    mime = payload.get('mimeType', '')
    if mime == 'text/plain':
        raw = payload.get('body', {}).get('data', '')
        if raw:
            return base64.urlsafe_b64decode(raw + '==').decode('utf-8', errors='replace')
    if mime.startswith('multipart/'):
        for part in payload.get('parts', []):
            text = _extract_plain_text(part)
            if text:
                return text
    return ''


def _collect_attachments(payload, message_id, out):
    mime = payload.get('mimeType', '')
    body = payload.get('body', {})
    filename = payload.get('filename', '')
    attachment_id = body.get('attachmentId', '')

    if filename and attachment_id:
        out.append({
            'filename': filename,
            'mimeType': mime,
            'size': body.get('size', 0),
            'attachmentId': attachment_id,
            'messageId': message_id,
        })

    for part in payload.get('parts', []):
        _collect_attachments(part, message_id, out)


def _parse_message(msg):
    headers = {h['name'].lower(): h['value']
               for h in msg.get('payload', {}).get('headers', [])}
    attachments = []
    _collect_attachments(msg.get('payload', {}), msg['id'], attachments)
    return {
        'id': msg['id'],
        'date': headers.get('date', ''),
        'from': headers.get('from', ''),
        'to': headers.get('to', ''),
        'subject': headers.get('subject', ''),
        'body': _extract_plain_text(msg.get('payload', {})).strip(),
        'attachments': attachments,
    }


def get_thread(token, thread_id):
    data = _get(token, f'/threads/{thread_id}', {'format': 'full'})
    return {
        'id': thread_id,
        'messages': [_parse_message(m) for m in data.get('messages', [])],
    }


def get_attachment(token, message_id, attachment_id):
    data = _get(token, f'/messages/{message_id}/attachments/{attachment_id}')
    # Gmail returns standard base64url; convert to standard base64 for callers
    raw = data.get('data', '')
    # urlsafe_b64decode handles both - just normalize padding
    return base64.urlsafe_b64decode(raw + '==')
