"""Real functional test of the SSH agent TCP<->Unix relay.

No subprocess, no `socat` — `dax_creds/ssh_bridge.py` replaced a host-side
`socat` call in `dax.py`'s `_start_ssh_agent_bridge`. `socat` is guaranteed
inside the dax container image (`Dockerfile.tmpl` installs it, and
`dax-entrypoint.sh` still uses it for the container-side half of this same
bridge) but was never guaranteed on the *host* running `dax run` — stock
macOS doesn't ship it. Found live 2026-08: it worked in every sandbox that
happened to have `socat` on PATH and failed with ENOENT on a real Mac. This
test exercises the real relay (real Unix socket, real TCP socket, real
bytes) rather than mocking either side, since that host/container split is
exactly what a mock would paper over.
"""
import asyncio

from dax_creds.ssh_bridge import run_bridge


async def _fake_agent_server(path):
    """Stands in for a real ssh-agent: echoes whatever it receives, so a
    round trip through the bridge proves both directions relay correctly."""
    async def handle(reader, writer):
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data)
            await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(handle, path=str(path))


async def _free_tcp_port():
    server = await asyncio.start_server(lambda r, w: None, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port


async def _run_round_trip(tmp_path):
    agent_sock = tmp_path / 'agent.sock'
    agent_server = await _fake_agent_server(agent_sock)

    port = await _free_tcp_port()
    bridge_task = asyncio.ensure_future(run_bridge(str(agent_sock), '127.0.0.1', port))
    await asyncio.sleep(0.1)  # let the bridge's listener actually bind

    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        writer.write(b'ssh-agent-protocol-bytes')
        await writer.drain()
        echoed = await asyncio.wait_for(reader.read(64), timeout=2.0)
        writer.close()
        return echoed
    finally:
        bridge_task.cancel()
        agent_server.close()
        await agent_server.wait_closed()


def test_bridge_relays_bytes_both_directions(tmp_path):
    echoed = asyncio.run(_run_round_trip(tmp_path))
    assert echoed == b'ssh-agent-protocol-bytes'


async def _run_against_missing_agent(tmp_path, port):
    bridge_task = asyncio.ensure_future(
        run_bridge(str(tmp_path / 'no-such-agent.sock'), '127.0.0.1', port))
    await asyncio.sleep(0.1)
    try:
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        try:
            writer.write(b'anything')
            await writer.drain()
            return await asyncio.wait_for(reader.read(64), timeout=2.0)
        except ConnectionResetError:
            # Whether the closed-before-reading side shows up as a clean EOF
            # or a reset depends on OS-level timing, not on anything this
            # bridge controls — either way it's "closed", which is the actual
            # guarantee under test. The `wait_for` above is what would catch
            # an actual hang.
            return b''
    finally:
        bridge_task.cancel()


def test_bridge_closes_tcp_side_when_agent_socket_is_gone(tmp_path):
    # A container asking for a bridge whose host agent has since vanished
    # (revoked, host rebooted) should see its connection cleanly closed, not
    # hang forever waiting on a socket that will never answer.
    port = asyncio.run(_free_tcp_port())
    data = asyncio.run(_run_against_missing_agent(tmp_path, port))
    assert data == b''
