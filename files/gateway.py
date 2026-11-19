"""Gateway of the administrator account (adminAccount): forwards connections on the admin port to
Stalwart's management listener on 127.0.0.1 in the same pod.

Stalwart accepts the account's app passwords only from 127.0.0.1 and ::1. Behind this gateway
every client appears as 127.0.0.1, so the credentials work only through it: never through the
ingress (which reaches the management port directly) or the mail ports. Stalwart refuses a
loopback address in X-Forwarded-For or Forwarded, so a client cannot claim one on the management
port. Who may reach the gateway is up to the Service and the NetworkPolicy.

Plain TCP, so TLS, HTTP/1.1 and HTTP/2 pass through unchanged. Standard library only.
Configuration: $GATEWAY_PORT (listen, all addresses), $TARGET_PORT (Stalwart, 127.0.0.1).
"""
import asyncio
import os
import signal

LISTEN_PORT = int(os.environ.get("GATEWAY_PORT", "8081"))
TARGET_PORT = int(os.environ.get("TARGET_PORT", "8080"))
CONNECT_TIMEOUT = 10
BUFFER = 64 * 1024
# Seconds open connections get to finish on SIGTERM.
SHUTDOWN_GRACE = 5


async def pipe(reader, writer):
    """Copies one direction until EOF, then half-closes it, so the other direction can finish.
    Returns False when the connection failed, so the caller closes both directions."""
    try:
        while data := await reader.read(BUFFER):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
        return True
    except (ConnectionError, OSError):
        return False


async def forward(client_reader, client_writer):
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", TARGET_PORT), CONNECT_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        client_writer.close()
        return
    writers = (upstream_writer, client_writer)
    try:
        pipes = {asyncio.ensure_future(pipe(client_reader, upstream_writer)),
                 asyncio.ensure_future(pipe(upstream_reader, client_writer))}
        while pipes:
            done, pipes = await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
            if not all(task.result() for task in done):
                # One side failed: closing both ends the other direction too.
                for writer in writers:
                    writer.close()
    finally:
        for writer in writers:
            writer.close()


async def main():
    connections = set()

    def handle(reader, writer):
        task = asyncio.ensure_future(forward(reader, writer))
        connections.add((task, writer))
        task.add_done_callback(lambda _: connections.discard((task, writer)))

    server = await asyncio.start_server(handle, port=LISTEN_PORT, reuse_address=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    print(f"forwarding port {LISTEN_PORT} to 127.0.0.1:{TARGET_PORT}", flush=True)
    await stop.wait()
    # Stop accepting, give open connections a moment to finish, then close the rest.
    server.close()
    pending = [task for task, _ in connections]
    if pending:
        await asyncio.wait(pending, timeout=SHUTDOWN_GRACE)
    for task, writer in list(connections):
        writer.close()
        task.cancel()
    await asyncio.gather(*(task for task, _ in connections), return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
