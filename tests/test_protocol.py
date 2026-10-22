import bz2
import queue
import socket
import struct
import tempfile
import types
import unittest
from pathlib import Path
import zlib
from unittest.mock import patch

import app


def make_info_packet() -> bytes:
    return (
        b"\xff\xff\xff\xff\x49"
        + b"\x11"
        + b"Libby's Test Server\x00"
        + b"gm_construct\x00"
        + b"garrysmod\x00"
        + b"Garry's Mod\x00"
        + struct.pack("<H", 4000)
        + bytes((3, 24, 0))
        + b"d"
        + b"w"
        + b"\x00"
        + b"\x01"
        + b"test-version\x00"
        + b"\x00"
    )


def make_player_packet(players) -> bytes:
    packet = bytearray(b"\xff\xff\xff\xff\x44" + bytes((len(players),)))
    for index, (name, score, duration) in enumerate(players):
        packet.extend(bytes((index,)))
        packet.extend(name.encode("utf-8") + b"\x00")
        packet.extend(struct.pack("<if", score, duration))
    return bytes(packet)


class FakeSocket:
    def __init__(self, responder):
        self.responder = responder
        self.sent = []
        self.responses = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def connect(self, _address):
        pass

    def settimeout(self, _timeout):
        pass

    def send(self, payload):
        self.sent.append(payload)
        self.responses.extend(self.responder(payload, len(self.sent)))

    def recv(self, _size):
        if not self.responses:
            raise socket.timeout("no more fake responses")
        return self.responses.pop(0)


def fake_socket_module(responder):
    return types.SimpleNamespace(
        AF_INET=socket.AF_INET,
        SOCK_DGRAM=socket.SOCK_DGRAM,
        timeout=socket.timeout,
        socket=lambda *_args: FakeSocket(responder),
    )


class ProtocolTests(unittest.TestCase):
    def test_chat_log_record_is_formatted_for_the_selected_server(self):
        record = "2026-10-18 12:34:56\t103.170.155.169:27017\tTEAM\tDEAD\tAlice\tHello everyone"
        self.assertEqual(
            app.format_chat_log_record(record, "103.170.155.169", 27017),
            "[2026-10-18 12:34:56] [Equipe, Morto] Alice: Hello everyone",
        )
        self.assertIsNone(app.format_chat_log_record(record, "192.0.2.2", 27017))
        self.assertIsNone(app.format_chat_log_record(record, "103.170.155.169", 27015))
        self.assertIsNone(app.format_chat_log_record("invalid record", "103.170.155.169", 27017))

    def test_customization_settings_are_validated_and_bounded(self):
        settings = app.customization_from_settings(
            {
                "server_name": "Custom GMod",
                "server_host": "192.0.2.9",
                "server_port": 28015,
                "poll_interval_seconds": 2,
                "console_background": "not-a-color",
                "console_foreground": "#aabbcc",
                "console_font_family": "Missing Font",
                "console_font_size": 30,
                "console_auto_scroll": False,
                "console_wrap_lines": True,
            }
        )
        self.assertEqual(settings["server_name"], "Custom GMod")
        self.assertEqual(settings["server_host"], "192.0.2.9")
        self.assertEqual(settings["server_port"], 28015)
        self.assertEqual(settings["poll_interval_seconds"], 5)
        self.assertEqual(settings["console_background"], app.DEFAULT_CONSOLE_BACKGROUND)
        self.assertEqual(settings["console_foreground"], "#aabbcc")
        self.assertEqual(settings["console_font_family"], "Consolas")
        self.assertEqual(settings["console_font_size"], 20)
        self.assertFalse(settings["console_auto_scroll"])
        self.assertTrue(settings["console_wrap_lines"])

    def test_custom_server_settings_require_ipv4_and_bound_the_port(self):
        settings = app.customization_from_settings({"server_host": "example.org", "server_port": 70000})
        self.assertEqual(settings["server_host"], app.SERVER_HOST)
        self.assertEqual(settings["server_port"], 65535)
        self.assertIsNone(app._valid_server_host("2001:db8::1"))
        self.assertEqual(app._valid_server_host(" 192.0.2.10 "), "192.0.2.10")

    def test_availability_history_is_sanitized_and_limited(self):
        entries = [{"timestamp": f"t{i}", "event": "online"} for i in range(app.MAX_HISTORY_ENTRIES + 2)]
        entries.extend([{"timestamp": "bad", "event": "unknown"}, "bad"])
        history = app.history_from_settings({"availability_history": entries})
        self.assertEqual(len(history), app.MAX_HISTORY_ENTRIES)
        self.assertEqual(history[0]["timestamp"], "t2")
        self.assertEqual(history[-1]["event"], "online")

    def test_availability_history_records_only_confirmed_transitions(self):
        history = app.append_history_event([], "offline", "17/10/2026 14:00:00")
        history = app.append_history_event(history, "online", "17/10/2026 14:01:00")
        history = app.append_history_event(history, "unknown", "ignored")
        self.assertEqual([entry["event"] for entry in history], ["offline", "online"])
        self.assertEqual(len(history), 2)

    def test_bad_player_names_are_trimmed_deduplicated_and_limited(self):
        names = app.bad_players_from_settings(
            {"bad_players": [" Alex ", "alex", "", None, "Riley"]}
        )
        self.assertEqual(names, ["Alex", "Riley"])
        self.assertEqual(app.format_player_duration(3661.8), "01:01:01")
        self.assertEqual(app.format_player_duration(float("nan")), "—")

    def test_parses_info_fields(self):
        info = app.parse_info_packet(make_info_packet())
        self.assertEqual(info.name, "Libby's Test Server")
        self.assertEqual(info.map_name, "gm_construct")
        self.assertEqual((info.players, info.max_players), (3, 24))
        self.assertEqual(info.version, "test-version")

    def test_handles_a2s_challenge(self):
        def responder(payload, _send_number):
            if payload == app.INFO_REQUEST:
                return [b"\xff\xff\xff\xff\x41" + b"cook"]
            if payload == app.INFO_REQUEST + b"cook":
                return [make_info_packet()]
            return []

        fake_module = fake_socket_module(responder)
        with patch.object(app, "socket", fake_module):
            info = app.query_server("127.0.0.1", 27015, timeout=1, attempts=1)
        self.assertEqual(info.name, "Libby's Test Server")

    def test_parses_a2s_player_response(self):
        players = app.parse_player_packet(make_player_packet([("Alice", 8, 125.0), ("Bób", 2, 60.0)]))
        self.assertEqual([(player.index, player.name) for player in players], [(0, "Alice"), (1, "Bób")])
        self.assertEqual(players[0].score, 8)
        self.assertEqual(players[0].duration_seconds, 125.0)
        self.assertEqual(app.parse_player_packet(make_player_packet([])), [])

    def test_queries_a2s_players_with_challenge(self):
        player_response = make_player_packet([("Alice", 8, 125.0)])

        def responder(payload, _send_number):
            if payload == app.PLAYER_REQUEST:
                return [b"\xff\xff\xff\xff\x41" + b"cook"]
            if payload == app.PLAYER_REQUEST[:5] + b"cook":
                return [player_response]
            return []

        fake_module = fake_socket_module(responder)
        with patch.object(app, "socket", fake_module):
            players = app.query_players("127.0.0.1", 27015, timeout=1, attempts=1)
        self.assertEqual([player.name for player in players], ["Alice"])

    def test_reassembles_split_info_response(self):
        response = make_info_packet()
        split_at = len(response) // 2
        packet_zero = b"\xfe\xff\xff\xff" + struct.pack("<IBB", 1234, 2, 0) + response[:split_at]
        packet_one = b"\xfe\xff\xff\xff" + struct.pack("<IBB", 1234, 2, 1) + response[split_at:]

        def responder(_payload, _send_number):
            return [packet_zero, packet_one]

        fake_module = fake_socket_module(responder)
        with patch.object(app, "socket", fake_module):
            info = app.query_server("127.0.0.1", 27015, timeout=1, attempts=1)
        self.assertEqual(info.map_name, "gm_construct")

    def test_reassembles_compressed_split_info_response(self):
        response = make_info_packet()
        compressed = bz2.compress(response)
        packet = (
            b"\xfe\xff\xff\xff"
            + struct.pack("<IBBII", 0x80000000 | 1234, 1, 0, len(response), zlib.crc32(response))
            + compressed
        )
        fake_module = fake_socket_module(lambda _payload, _send_number: [packet])
        with patch.object(app, "socket", fake_module):
            info = app.query_server("127.0.0.1", 27015, timeout=1, attempts=1)
        self.assertEqual(info.version, "test-version")

    def test_retries_after_a_timed_out_query(self):
        requests = 0

        def responder(_payload, _send_number):
            nonlocal requests
            requests += 1
            return [] if requests == 1 else [make_info_packet()]

        fake_module = fake_socket_module(responder)
        with patch.object(app, "socket", fake_module):
            info = app.query_server("127.0.0.1", 27015, timeout=0.01, attempts=2)
        self.assertEqual(info.name, "Libby's Test Server")
        self.assertEqual(requests, 2)

    def test_requires_three_failures_and_two_successes_for_transitions(self):
        tracker = app.StateTracker()
        self.assertEqual(tracker.observe(True), ("online", None))
        self.assertEqual(tracker.observe(False), ("online", None))
        self.assertEqual(tracker.observe(False), ("online", None))
        self.assertEqual(tracker.observe(False), ("offline", "offline"))
        self.assertEqual(tracker.observe(True), ("offline", None))
        self.assertEqual(tracker.observe(True), ("online", "online"))

    def test_monitor_reports_a2s_response_duration_in_milliseconds(self):
        class StopAfterOneCycle:
            calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls > 1

            def wait(self, _seconds):
                return False

        results = queue.Queue()
        worker = app.MonitorWorker(results, StopAfterOneCycle())
        with patch.object(app, "query_server", return_value=app.parse_info_packet(make_info_packet())), patch.object(
            app, "query_players", return_value=[]
        ), patch.object(
            app.time, "monotonic", return_value=0
        ), patch.object(app.time, "perf_counter", side_effect=(10.0, 10.047)):
            worker.run()
        result = results.get_nowait()
        self.assertTrue(result["ok"])
        self.assertEqual(result["response_ms"], 47)

    def test_monitor_spaces_slow_cycles_and_throttles_player_queries(self):
        clock = {"now": 0.0}
        waits = []
        cycles = 0

        class StopAfterTwoCycles:
            def is_set(self):
                return cycles >= 2

        def query_info(_host, _port):
            nonlocal cycles
            cycles += 1
            if cycles == 1:
                clock["now"] = 5.0
            return app.parse_info_packet(make_info_packet())

        results = queue.Queue()
        worker = app.MonitorWorker(results, StopAfterTwoCycles(), interval_seconds=1, developer_mode=True)

        def wait_for_schedule(seconds):
            waits.append(seconds)
            clock["now"] += seconds
            return False

        with patch.object(app, "query_server", side_effect=query_info), patch.object(
            app, "query_players", return_value=[]
        ) as player_query, patch.object(
            app.time, "monotonic", side_effect=lambda: clock["now"]
        ), patch.object(app.time, "perf_counter", side_effect=(10.0, 10.047, 20.0, 20.047)), patch.object(
            worker.schedule_changed, "wait", side_effect=wait_for_schedule
        ):
            worker.run()

        self.assertEqual(player_query.call_count, 1)
        self.assertEqual(waits, [1.0])
        self.assertEqual(results.get_nowait()["player_list"], [])
        self.assertEqual(results.get_nowait()["player_list"], [])

    def test_monitor_uses_custom_server_ip_and_port(self):
        class StopAfterOneCycle:
            calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls > 1

            def wait(self, _seconds):
                return False

        queried = []

        def fake_query(host, port):
            queried.append((host, port))
            return app.parse_info_packet(make_info_packet())

        results = queue.Queue()
        worker = app.MonitorWorker(results, StopAfterOneCycle(), host="192.0.2.20", port=28015)
        with patch.object(app, "query_server", side_effect=fake_query), patch.object(
            app, "query_players", return_value=[]
        ), patch.object(app.time, "monotonic", return_value=0), patch.object(
            app.time, "perf_counter", side_effect=(10.0, 10.02)
        ):
            worker.run()
        result = results.get_nowait()
        self.assertEqual(queried, [("192.0.2.20", 28015)])
        self.assertEqual((result["target_host"], result["target_port"]), ("192.0.2.20", 28015))

    def test_console_tailer_reads_existing_and_appended_text(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            log_path = Path(temp_dir) / "console.txt"
            log_path.write_text("linha inicial\n", encoding="utf-8")
            tailer = app.ConsoleLogTailer(log_path)

            text, reset, skipped = tailer.read_available()
            self.assertEqual(text, "linha inicial\n")
            self.assertFalse(reset)
            self.assertFalse(skipped)

            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write("mensagem do servidor\n")
            text, reset, skipped = tailer.read_available()
            self.assertEqual(text, "mensagem do servidor\n")
            self.assertFalse(reset)
            self.assertFalse(skipped)

    def test_console_tailer_detects_truncation(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            log_path = Path(temp_dir) / "console.txt"
            log_path.write_text("conteúdo anterior mais longo\n", encoding="utf-8")
            tailer = app.ConsoleLogTailer(log_path)
            tailer.read_available()

            log_path.write_text("novo\n", encoding="utf-8")
            text, reset, _skipped = tailer.read_available()
            self.assertEqual(text, "novo\n")
            self.assertTrue(reset)


if __name__ == "__main__":
    unittest.main()
