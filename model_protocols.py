"""Wire protocols shared by model configuration, Agent Runtime and ingestion."""
from urllib.parse import quote

PROTOCOLS = {'openai-compatible', 'openai-responses', 'anthropic', 'gemini'}
DEFAULT_PROTOCOL = 'openai-compatible'


def protocol_for(data):
    # Old connections predate this field and always used Chat Completions.
    value = data.get('protocol') or DEFAULT_PROTOCOL
    if value not in PROTOCOLS:
        raise ValueError('MODEL_PROTOCOL_UNSUPPORTED')
    return value


def sdk_endpoint(base_url, protocol):
    base = base_url.rstrip('/')
    if protocol == 'anthropic':
        return (base[:-3] if base.endswith('/v1') else base), 'v1'
    if protocol == 'gemini':
        root, _, last = base.rpartition('/')
        return (root, last) if last in {'v1', 'v1beta', 'v1alpha'} else (base, 'v1beta')
    return base, ''


def api_base(base_url, protocol):
    root, version = sdk_endpoint(base_url, protocol)
    return root + '/' + version if version else root


def request_headers(data):
    key = data.get('api_key', '')
    protocol = protocol_for(data)
    if protocol == 'anthropic':
        return {'anthropic-version': '2023-06-01', **({'x-api-key': key} if key else {})}
    if protocol == 'gemini':
        return {'x-goog-api-key': key} if key else {}
    return {'Authorization': 'Bearer ' + key} if key else {}


def generation_url(data):
    protocol = protocol_for(data)
    base = api_base(data['base_url'], protocol)
    if protocol == 'gemini':
        model = str(data['model_id']).removeprefix('models/')
        return base + '/models/' + quote(model, safe='') + ':generateContent'
    return base + {'openai-compatible': '/chat/completions', 'openai-responses': '/responses', 'anthropic': '/messages'}[protocol]
