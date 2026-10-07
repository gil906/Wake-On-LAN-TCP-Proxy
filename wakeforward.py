#!/usr/bin/env python3
"""Wake a configured server on demand and forward TCP connections to it."""
import asyncio
import os
import socket
import time

MAPPINGS = os.environ.get("WAKE_MAPPINGS", "")
WOL_MAC = os.environ.get("WOL_MAC", "")
BCASTS = os.environ.get("WOL_BROADCASTS", "255.255.255.255").split(",")
HOST = os.environ.get("WAKE_HOST", "127.0.0.1")
WAKE_TIMEOUT = int(os.environ.get("WAKE_TIMEOUT", "120"))
PROBE_TIMEOUT = float(os.environ.get("PROBE_TIMEOUT", "1.5"))


def mac_addresses():
    addresses = []
    for value in WOL_MAC.split(","):
        compact = value.strip().replace(":", "").replace("-", "")
        if len(compact) != 12:
            raise ValueError("WOL_MAC must contain six-byte MAC addresses")
        try:
            address = bytes.fromhex(compact)
        except ValueError as error:
            raise ValueError("WOL_MAC must contain hexadecimal MAC addresses") from error
        if len(address) != 6:
            raise ValueError("WOL_MAC must contain six-byte MAC addresses")
        addresses.append(address)
    return addresses


def parse_mappings(value):
    mappings = []
    for mapping in value.split(","):
        if not mapping.strip():
            continue
        fields = mapping.strip().split(":")
        if len(fields) != 3 or not fields[1].strip():
            raise ValueError("Use listen_port:target_host:target_port mappings")
        listen, host, target = fields
        listen, target = int(listen), int(target)
        if not 1 <= listen <= 65535 or not 1 <= target <= 65535:
            raise ValueError("Mapping ports must be between 1 and 65535")
        mappings.append((listen, host.strip(), target))
    if not mappings:
        raise ValueError("Set WAKE_MAPPINGS before starting the proxy")
    return mappings


def send_wol():
    sent = False
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for mac in mac_addresses():
            packet = b"\xff" * 6 + mac * 16
            for broadcast in BCASTS:
                try:
                    sender.sendto(packet, (broadcast.strip(), 9))
                    sent = True
                except OSError as error:
                    print(f"[wakeforward] Wake-on-LAN send failed: {error}", flush=True)
    if not sent:
        raise OSError("No Wake-on-LAN packet could be sent")


async def reachable(host, port):
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), PROBE_TIMEOUT,
        )
    except (OSError, TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def ensure_awake(host, port):
    if await reachable(host, port):
        return True
    print(f"[wakeforward] {host}:{port} unavailable; sending Wake-on-LAN", flush=True)
    send_wol()
    deadline = time.monotonic() + WAKE_TIMEOUT
    while time.monotonic() < deadline:
        if await reachable(host, port):
            print(f"[wakeforward] {host}:{port} is ready", flush=True)
            return True
        await asyncio.sleep(2)
        if time.monotonic() < deadline:
            send_wol()
    print(f"[wakeforward] {host}:{port} not ready within {WAKE_TIMEOUT}s", flush=True)
    return False


async def pipe(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()
        await writer.drain()


async def handle(local_reader, local_writer, host, port):
    remote_writer = None
    try:
        if not await ensure_awake(host, port):
            return
        remote_reader, remote_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), PROBE_TIMEOUT,
        )
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(pipe(local_reader, remote_writer))
            tasks.create_task(pipe(remote_reader, local_writer))
    except* (OSError, TimeoutError) as errors:
        for error in errors.exceptions:
            print(f"[wakeforward] Connection forwarding failed: {error}", flush=True)
    finally:
        for writer in (local_writer, remote_writer):
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass


async def main():
    mappings = parse_mappings(MAPPINGS)
    mac_addresses()
    if WAKE_TIMEOUT <= 0 or PROBE_TIMEOUT <= 0:
        raise ValueError("Timeouts must be positive")
    if any(not broadcast.strip() for broadcast in BCASTS):
        raise ValueError("WOL_BROADCASTS must contain nonempty addresses")
    servers = []
    try:
        for listen, host, target in mappings:
            def make_handler(destination, port):
                async def callback(reader, writer):
                    await handle(reader, writer, destination, port)
                return callback
            server = await asyncio.start_server(
                make_handler(host, target), HOST, listen,
            )
            print(f"[wakeforward] listening on {HOST}:{listen} -> {host}:{target}", flush=True)
            servers.append(server)
        await asyncio.gather(*(server.serve_forever() for server in servers))
    finally:
        for server in servers:
            server.close()
        await asyncio.gather(*(server.wait_closed() for server in servers))


if __name__ == "__main__":
    asyncio.run(main())
