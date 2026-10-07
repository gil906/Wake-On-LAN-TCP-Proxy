import asyncio
import os
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch
import unittest

import wakeforward as proxy


class ConfigurationTests(unittest.TestCase):
    def test_default_listener_is_loopback(self):
        env = dict(os.environ)
        env.pop("WAKE_HOST", None)
        result = subprocess.run(
            [sys.executable, "-c", "import wakeforward; assert wakeforward.HOST == '127.0.0.1'"],
            env=env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_valid_mappings(self):
        self.assertEqual(
            proxy.parse_mappings("9000:sleeping-server.example:8080,9001:localhost:8443"),
            [(9000, "sleeping-server.example", 8080), (9001, "localhost", 8443)],
        )

    def test_missing_invalid_and_out_of_range_mappings_fail(self):
        for value in ["", "9000::8080", "0:localhost:8080", "9000:localhost:65536", "bad"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                proxy.parse_mappings(value)

    def test_missing_invalid_and_multiple_macs(self):
        for value in ["", "not-a-mac", "zz:00:00:00:00:01", "0000 0000 00"]:
            with self.subTest(value=value), patch.object(proxy, "WOL_MAC", value):
                with self.assertRaises(ValueError):
                    proxy.mac_addresses()
        with patch.object(proxy, "WOL_MAC", "02:00:00:00:00:01,02-00-00-00-00-02"):
            self.assertEqual(proxy.mac_addresses(), [
                bytes.fromhex("020000000001"), bytes.fromhex("020000000002"),
            ])

    def test_magic_packet_contents_without_transmission(self):
        sender = MagicMock()
        sender.__enter__.return_value = sender
        with patch.object(proxy.socket, "socket", return_value=sender), \
             patch.object(proxy, "WOL_MAC", "02:00:00:00:00:01"), \
             patch.object(proxy, "BCASTS", ["255.255.255.255"]):
            proxy.send_wol()
        packet, address = sender.sendto.call_args.args
        self.assertEqual(packet, b"\xff" * 6 + bytes.fromhex("020000000001") * 16)
        self.assertEqual(len(packet), 102)
        self.assertEqual(address, ("255.255.255.255", 9))

    def test_send_failure_is_explicit(self):
        sender = MagicMock()
        sender.__enter__.return_value = sender
        sender.sendto.side_effect = OSError("no route")
        with patch.object(proxy.socket, "socket", return_value=sender), \
             patch.object(proxy, "WOL_MAC", "02:00:00:00:00:01"):
            with self.assertRaisesRegex(OSError, "No Wake-on-LAN"):
                proxy.send_wol()


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_awake_target_does_not_send_wake_packet(self):
        with patch.object(proxy, "reachable", AsyncMock(return_value=True)), \
             patch.object(proxy, "send_wol") as send:
            self.assertTrue(await proxy.ensure_awake("localhost", 8080))
        send.assert_not_called()

    async def test_sleeping_target_is_woken(self):
        with patch.object(proxy, "reachable", AsyncMock(side_effect=[False, True])), \
             patch.object(proxy, "send_wol") as send:
            self.assertTrue(await proxy.ensure_awake("localhost", 8080))
        send.assert_called_once()

    async def test_wake_timeout(self):
        with patch.object(proxy, "reachable", AsyncMock(return_value=False)), \
             patch.object(proxy, "send_wol") as send, \
             patch.object(proxy, "WAKE_TIMEOUT", 0):
            self.assertFalse(await proxy.ensure_awake("localhost", 8080))
        send.assert_called_once()

    async def test_reachable_and_unreachable_ports(self):
        async def close(reader, writer):
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(close, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        self.assertTrue(await proxy.reachable("127.0.0.1", port))
        server.close()
        await server.wait_closed()
        self.assertFalse(await proxy.reachable("127.0.0.1", port))

    async def test_bidirectional_forwarding_and_half_close(self):
        completed = asyncio.Event()

        async def target(reader, writer):
            data = await reader.read()
            if data:
                writer.write(b"response:" + data)
                await writer.drain()
            writer.close()
            await writer.wait_closed()

        backend = await asyncio.start_server(target, "127.0.0.1", 0)
        target_port = backend.sockets[0].getsockname()[1]

        async def forward(reader, writer):
            try:
                await proxy.handle(reader, writer, "127.0.0.1", target_port)
            finally:
                completed.set()

        server = await asyncio.start_server(forward, "127.0.0.1", 0)
        listen_port = server.sockets[0].getsockname()[1]
        writer = None
        try:
            with patch.object(proxy, "send_wol") as send:
                reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
                writer.write(b"hello")
                await writer.drain()
                writer.write_eof()
                response = await asyncio.wait_for(reader.read(), 3)
                self.assertEqual(response, b"response:hello")
                await asyncio.wait_for(completed.wait(), 3)
                send.assert_not_called()
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()
            server.close()
            backend.close()
            await server.wait_closed()
            await backend.wait_closed()

    async def test_failed_wake_closes_client(self):
        reader = AsyncMock()
        writer = MagicMock()
        writer.wait_closed = AsyncMock()
        with patch.object(proxy, "ensure_awake", AsyncMock(return_value=False)):
            await proxy.handle(reader, writer, "localhost", 8080)
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
