"""TCP <-> Unix-socket relay for SSH agent forwarding, run on the host.

`dax.py`'s `_start_ssh_agent_bridge` used to shell out to `socat` for this.
`socat` is guaranteed inside the dax container image (Dockerfile.tmpl
installs it, and `dax-entrypoint.sh` still uses it for the container-side
half of this same bridge) but never guaranteed on the host — stock macOS
doesn't ship it, and nothing here ever checked before relying on it. Python
is already guaranteed on the host (it's what's running `dax.py`), so this
relay is a few dozen lines of asyncio instead of a new host prerequisite.

Opens a *new* Unix connection for every incoming TCP connection, matching
`socat ... ,fork ...`'s behavior on the container side: every SSH agent
request re-resolves whatever the host agent socket currently is, rather than
holding one connection open across a sleep/wake cycle or an agent restart.
"""
import asyncio
import sys


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()


async def _handle(agent_sock, tcp_reader, tcp_writer):
    try:
        unix_reader, unix_writer = await asyncio.open_unix_connection(agent_sock)
    except OSError:
        tcp_writer.close()
        return
    await asyncio.gather(
        _pipe(tcp_reader, unix_writer),
        _pipe(unix_reader, tcp_writer),
    )


async def run_bridge(agent_sock, host, port):
    async def handler(reader, writer):
        await _handle(agent_sock, reader, writer)

    server = await asyncio.start_server(handler, host, port)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='SSH agent TCP<->Unix bridge')
    parser.add_argument('--agent-sock', required=True, help='Host SSH agent Unix socket path')
    parser.add_argument('--port', type=int, required=True, help='TCP port to listen on')
    args = parser.parse_args()

    try:
        asyncio.run(run_bridge(args.agent_sock, '127.0.0.1', args.port))
    except KeyboardInterrupt:
        sys.exit(0)
