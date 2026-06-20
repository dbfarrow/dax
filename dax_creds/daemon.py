import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path


class FileTokenStore:
    def __init__(self, path):
        self._path = Path(path)

    def get_refresh_token(self, name):
        with open(self._path) as f:
            tokens = json.load(f)
        if name not in tokens:
            raise KeyError(f'No token for {name} in {self._path}')
        return tokens[name]


class PassthroughExchanger:
    def exchange(self, provider, refresh_token):
        return {'token': refresh_token, 'expires_at': '2099-01-01T00:00:00Z'}


def handle_request(request, credentials, token_store, token_exchanger, cache):
    name = request.get('credential')
    if name not in credentials:
        return {'error': 'not_configured', 'message': f'No credential named {name}'}
    if name in cache:
        entry = cache[name]
        expires_at = datetime.fromisoformat(entry['expires_at'])
        if expires_at > datetime.now(timezone.utc):
            return entry
    provider = credentials[name]['provider']
    refresh_token = token_store.get_refresh_token(name)
    result = token_exchanger.exchange(provider, refresh_token)
    cache[name] = result
    return result


async def run_daemon(socket_path, credentials, token_store, token_exchanger):
    cache = {}

    async def handle(reader, writer):
        data = await reader.read(4096)
        try:
            request = json.loads(data)
        except json.JSONDecodeError:
            response = {'error': 'invalid_request', 'message': 'Invalid JSON'}
        else:
            response = handle_request(request, credentials, token_store, token_exchanger, cache)
        writer.write(json.dumps(response).encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    server = await asyncio.start_unix_server(handle, path=str(path))
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='dax-creds daemon')
    parser.add_argument('--socket', required=True, help='Unix socket path')
    parser.add_argument('--tokens', required=True, help='JSON file of tokens')
    parser.add_argument('--credentials', required=True, help='JSON of credential definitions')
    args = parser.parse_args()

    credentials = json.loads(args.credentials)
    token_store = FileTokenStore(args.tokens)
    exchanger = PassthroughExchanger()

    asyncio.run(run_daemon(args.socket, credentials, token_store, exchanger))
