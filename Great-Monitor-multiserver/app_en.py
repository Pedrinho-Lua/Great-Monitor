"""Windows desktop monitor for a Garry's Mod / Source server."""

from __future__ import annotations

import bz2
import codecs
import ipaddress
import json
import math
import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk


APP_NAME = "LibbysServerMonitor"
SERVER_NAME = "Libby's Test Server"
SERVER_HOST = "103.170.155.169"
SERVER_PORT = 27017
POLL_INTERVAL_SECONDS = 10
MIN_POLL_INTERVAL_SECONDS = 5
MAX_POLL_INTERVAL_SECONDS = 300
MAX_DEVELOPER_POLL_INTERVAL_SECONDS = int(threading.TIMEOUT_MAX)
QUERY_TIMEOUT_SECONDS = 1.2
QUERY_ATTEMPTS = 3
FAILURES_TO_OFFLINE = 3
SUCCESSES_TO_RECOVER = 2
PLAYER_QUERY_REFRESH_SECONDS = 10
INFO_REQUEST = b"\xff\xff\xff\xff\x54Source Engine Query\x00"
PLAYER_REQUEST = b"\xff\xff\xff\xff\x55\xff\xff\xff\xff"
LOG_INITIAL_BYTES = 256 * 1024
LOG_READ_BYTES = 64 * 1024
LOG_MAX_LINES = 5000
DEVELOPER_LOG_MAX_LINES = 3000
CHAT_LOG_FILE_NAME = "console.txt"
LEGACY_CHAT_LOG_FILE_NAME = "libbys_server_monitor_chat.txt"
MAX_HISTORY_ENTRIES = 500
MAX_BAD_PLAYERS = 500
MAX_SERVERS = 100
DEFAULT_SERVER_ID = "default"
PLAYER_QUERY_TIMEOUT_SECONDS = 1.0
CONSOLE_FONT_FAMILIES = ("Consolas", "Cascadia Mono", "Courier New", "Lucida Console")
DEFAULT_CONSOLE_BACKGROUND = "#111820"
DEFAULT_CONSOLE_FOREGROUND = "#d6e2ec"


class QueryError(Exception):
    """The server did not return a valid Source A2S_INFO response."""


@dataclass(frozen=True)
class ServerInfo:
    name: str
    map_name: str
    folder: str
    game: str
    app_id: int
    players: int
    max_players: int
    bots: int
    server_type: str
    environment: str
    visibility: int
    vac: int
    version: str


@dataclass(frozen=True)
class PlayerInfo:
    index: int
    name: str
    score: int
    duration_seconds: float


def _read_cstring(data: bytes, offset: int) -> tuple[str, int]:
    end = data.find(b"\x00", offset)
    if end < 0:
        raise QueryError("Incomplete A2S_INFO response (unterminated text field).")
    return data[offset:end].decode("utf-8", errors="replace"), end + 1


def parse_info_packet(data: bytes) -> ServerInfo:
    """Parse a complete, reassembled Source engine A2S_INFO response."""
    if len(data) < 6 or data[:4] != b"\xff\xff\xff\xff" or data[4] != 0x49:
        raise QueryError("UDP response is not an A2S_INFO packet.")

    offset = 5
    protocol = data[offset]
    offset += 1
    name, offset = _read_cstring(data, offset)
    map_name, offset = _read_cstring(data, offset)
    folder, offset = _read_cstring(data, offset)
    game, offset = _read_cstring(data, offset)
    # app id (2), three player fields, four flags and a minimum empty version string.
    if len(data) < offset + 10:
        raise QueryError("Incomplete A2S_INFO response (basic fields).")
    app_id = struct.unpack_from("<H", data, offset)[0]
    offset += 2
    players, max_players, bots = struct.unpack_from("BBB", data, offset)
    offset += 3
    server_type = chr(data[offset])
    environment = chr(data[offset + 1])
    visibility = data[offset + 2]
    vac = data[offset + 3]
    offset += 4
    version, offset = _read_cstring(data, offset)

    # The EDF byte and its optional fields follow version on newer servers.
    # We intentionally ignore those optional fields; A2S_INFO is valid without
    # needing to parse them to decide whether the server answered.
    if protocol == 0:
        raise QueryError("Invalid A2S_INFO protocol version.")

    return ServerInfo(
        name=name or SERVER_NAME,
        map_name=map_name,
        folder=folder,
        game=game,
        app_id=app_id,
        players=players,
        max_players=max_players,
        bots=bots,
        server_type=server_type,
        environment=environment,
        visibility=visibility,
        vac=vac,
        version=version,
    )


def parse_player_packet(data: bytes) -> list[PlayerInfo]:
    """Parse a complete Source A2S_PLAYER response."""
    if len(data) < 6 or data[:5] != b"\xff\xff\xff\xff\x44":
        raise QueryError("UDP response is not an A2S_PLAYER packet.")
    player_count = data[5]
    offset = 6
    players = []
    for _ in range(player_count):
        if offset >= len(data):
            raise QueryError("Incomplete A2S_PLAYER response (player index).")
        player_index = data[offset]
        offset += 1
        name, offset = _read_cstring(data, offset)
        if len(data) < offset + 8:
            raise QueryError("Incomplete A2S_PLAYER response (score or duration).")
        score, duration = struct.unpack_from("<if", data, offset)
        offset += 8
        players.append(PlayerInfo(player_index, name, score, duration))
    return players


def _reassemble_split_packets(first_packet: bytes, sock: socket.socket, deadline: float) -> bytes:
    """Receive and join a Source split response, including bzip2 responses."""
    packets: dict[int, bytes] = {}
    request_id: Optional[int] = None
    packet_count: Optional[int] = None
    compressed = False
    expected_size: Optional[int] = None
    expected_crc: Optional[int] = None

    pending = [first_packet]
    while pending or (packet_count is None or len(packets) < packet_count):
        if pending:
            packet = pending.pop()
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QueryError("Incomplete split A2S_INFO response.")
            sock.settimeout(remaining)
            try:
                packet = sock.recv(65535)
            except socket.timeout as exc:
                raise QueryError("Incomplete split A2S_INFO response.") from exc

        if len(packet) < 10 or packet[:4] != b"\xfe\xff\xff\xff":
            continue
        raw_id = struct.unpack_from("<I", packet, 4)[0]
        this_compressed = bool(raw_id & 0x80000000)
        this_id = raw_id & 0x7FFFFFFF
        count = packet[8]
        index = packet[9]
        if count == 0 or count > 64 or index >= count:
            raise QueryError("Invalid split response header.")
        if request_id is None:
            request_id, packet_count, compressed = this_id, count, this_compressed
        elif (this_id, count, this_compressed) != (request_id, packet_count, compressed):
            continue
        if index in packets:
            continue

        payload_offset = 10
        if compressed and index == 0:
            if len(packet) < 18:
                raise QueryError("Incomplete compressed A2S_INFO header.")
            expected_size, expected_crc = struct.unpack_from("<II", packet, 10)
            payload_offset = 18
        packets[index] = packet[payload_offset:]

    joined = b"".join(packets[index] for index in range(packet_count or 0))
    if compressed:
        try:
            joined = bz2.decompress(joined)
        except (OSError, EOFError) as exc:
            raise QueryError("Could not decompress the A2S_INFO response.") from exc
        if len(joined) != expected_size or zlib.crc32(joined) != expected_crc:
            raise QueryError("Invalid A2S_INFO response size or checksum.")
    return joined


def _receive_info_response(sock: socket.socket, deadline: float) -> ServerInfo:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QueryError("Timed out waiting for A2S_INFO.")
        sock.settimeout(remaining)
        try:
            packet = sock.recv(65535)
        except socket.timeout as exc:
            raise QueryError("Timed out waiting for A2S_INFO.") from exc

        if len(packet) >= 9 and packet[:5] == b"\xff\xff\xff\xff\x41":
            # Valve's S2C_CHALLENGE contains a four-byte token after 0x41.
            sock.send(INFO_REQUEST + packet[5:9])
            continue
        if packet[:4] == b"\xfe\xff\xff\xff":
            packet = _reassemble_split_packets(packet, sock, deadline)
        return parse_info_packet(packet)


def query_server(
    host: str = SERVER_HOST,
    port: int = SERVER_PORT,
    timeout: float = QUERY_TIMEOUT_SECONDS,
    attempts: int = QUERY_ATTEMPTS,
) -> ServerInfo:
    """Query the server with bounded retries and optional challenge handling."""
    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((host, port))
                sock.settimeout(timeout)
                sock.send(INFO_REQUEST)
                return _receive_info_response(sock, time.monotonic() + timeout)
        except (OSError, QueryError) as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(0.15 * (attempt + 1))
    detail = str(last_error) if last_error else "no response"
    raise QueryError(f"No valid response after {attempts} attempts: {detail}")


def _receive_player_response(sock: socket.socket, deadline: float) -> list[PlayerInfo]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QueryError("Timed out waiting for A2S_PLAYER.")
        sock.settimeout(remaining)
        try:
            packet = sock.recv(65535)
        except socket.timeout as exc:
            raise QueryError("Timed out waiting for A2S_PLAYER.") from exc

        if len(packet) >= 9 and packet[:5] == b"\xff\xff\xff\xff\x41":
            sock.send(PLAYER_REQUEST[:5] + packet[5:9])
            continue
        if packet[:4] == b"\xfe\xff\xff\xff":
            packet = _reassemble_split_packets(packet, sock, deadline)
        return parse_player_packet(packet)


def query_players(
    host: str = SERVER_HOST,
    port: int = SERVER_PORT,
    timeout: float = PLAYER_QUERY_TIMEOUT_SECONDS,
    attempts: int = QUERY_ATTEMPTS,
) -> list[PlayerInfo]:
    """Query the public Source player list with challenge and timeout handling."""
    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((host, port))
                sock.settimeout(timeout)
                sock.send(PLAYER_REQUEST)
                return _receive_player_response(sock, time.monotonic() + timeout)
        except (OSError, QueryError) as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(0.15 * (attempt + 1))
    detail = str(last_error) if last_error else "no response"
    raise QueryError(f"Player list unavailable after {attempts} attempts: {detail}")


class ConsoleLogTailer:
    """Read new bytes from a local Garry's Mod console log file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.offset = 0
        self.signature: Optional[tuple[int, int]] = None
        self.initialized = False
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.pending_carriage_return = False

    def read_available(self) -> tuple[str, bool, bool]:
        """Return (new text, reset_display, skipped_old_content)."""
        stat = self.path.stat()
        signature = (getattr(stat, "st_dev", 0), getattr(stat, "st_ino", 0))
        reset_display = self.signature is not None and (
            signature != self.signature or stat.st_size < self.offset
        )
        if reset_display:
            self.offset = 0
            self.initialized = True
            self.decoder.reset()
            self.pending_carriage_return = False

        skipped_old_content = False
        with self.path.open("rb") as log_file:
            if not self.initialized:
                start = max(0, stat.st_size - LOG_INITIAL_BYTES)
                log_file.seek(start)
                if start:
                    log_file.readline()
                    skipped_old_content = True
                self.offset = log_file.tell()
                self.initialized = True
            else:
                log_file.seek(self.offset)
            chunk = log_file.read(LOG_READ_BYTES)
            self.offset = log_file.tell()

        self.signature = signature
        text = self.decoder.decode(chunk, final=False)
        if self.pending_carriage_return:
            text = "\r" + text
            self.pending_carriage_return = False
        if text.endswith("\r"):
            text = text[:-1]
            self.pending_carriage_return = True
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return text, reset_display, skipped_old_content


def format_chat_log_record(line: str, target_host: str, target_port: int) -> Optional[str]:
    """Format an addon-marked console row or legacy client-addon row."""
    line = line.rstrip("\r\n")
    marker = "[LSM_CHAT]"
    if marker in line:
        line = line[line.index(marker) + len(marker):].lstrip("\t ")
    fields = line.split("\t", 5)
    if len(fields) != 6:
        return None
    timestamp, address, channel, player_state, player_name, message = fields
    address_host, separator, address_port = address.rpartition(":")
    if not separator or address_host != target_host:
        return None
    try:
        if int(address_port) != target_port:
            return None
    except ValueError:
        return None

    tags = []
    if channel == "TEAM":
        tags.append("Team")
    if player_state == "DEAD":
        tags.append("Dead")
    tag_text = f" [{', '.join(tags)}]" if tags else ""
    return f"[{timestamp}]{tag_text} {player_name}: {message}"


def settings_file() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    return base / "settings.json"


def load_settings() -> dict:
    try:
        loaded = json.loads(settings_file().read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            return {}
        for legacy_key in ("notifications_enabled", "notify_online", "notify_offline"):
            loaded.pop(legacy_key, None)
        return loaded
    except (OSError, ValueError, TypeError):
        return {}


def save_settings(settings: dict) -> None:
    path = settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, parsed))


def _valid_hex_color(value, default: str) -> str:
    if (
        isinstance(value, str)
        and len(value) == 7
        and value.startswith("#")
        and all(character in "0123456789abcdefABCDEF" for character in value[1:])
    ):
        return value
    return default


def _valid_server_host(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    host = value.strip().rstrip(".")
    if not host or len(host) > 253 or any(character.isspace() for character in host):
        return None
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        pass
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    labels = host.split(".")
    if not labels or any(
        not label
        or len(label) > 63
        or not label[0].isalnum()
        or not label[-1].isalnum()
        or any(not (character.isalnum() or character == "-") for character in label)
        for label in labels
    ):
        return None
    return host


def parse_server_address(value, default_port=SERVER_PORT) -> Optional[tuple[str, int]]:
    """Validate a host or host:port value, with a separate fallback port."""
    if not isinstance(value, str):
        return None
    address = value.strip()
    host_text = address
    port_value = default_port
    if address.count(":") == 1:
        possible_host, possible_port = address.rsplit(":", 1)
        if possible_port.isdecimal():
            host_text = possible_host
            port_value = possible_port
    host = _valid_server_host(host_text)
    if host is None:
        return None
    try:
        port = int(port_value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not 1 <= port <= 65535:
        return None
    return host, port


def _valid_server_name(value, default: str = SERVER_NAME) -> str:
    if not isinstance(value, str):
        return default
    name = value.strip()
    return name[:64] if name else default


def servers_from_settings(settings: dict) -> list[dict[str, object]]:
    saved = settings.get("servers", [])
    servers = []
    seen_ids = set()
    if isinstance(saved, list):
        for index, entry in enumerate(saved):
            if not isinstance(entry, dict):
                continue
            endpoint = parse_server_address(entry.get("host"), entry.get("port", SERVER_PORT))
            if endpoint is None:
                continue
            host, port = endpoint
            server_id = entry.get("id")
            if not isinstance(server_id, str) or not server_id.strip() or len(server_id) > 64:
                server_id = f"saved-{index + 1}"
            server_id = server_id.strip()
            if server_id in seen_ids:
                server_id = f"{server_id}-{index + 1}"
            seen_ids.add(server_id)
            servers.append({
                "id": server_id,
                "name": _valid_server_name(entry.get("name"), host),
                "host": host,
                "port": port,
            })
            if len(servers) >= MAX_SERVERS:
                break
    if servers:
        return servers
    endpoint = parse_server_address(
        settings.get("server_host", SERVER_HOST), settings.get("server_port", SERVER_PORT)
    ) or (SERVER_HOST, SERVER_PORT)
    host, port = endpoint
    return [{
        "id": DEFAULT_SERVER_ID,
        "name": _valid_server_name(settings.get("server_name"), host),
        "host": host,
        "port": port,
    }]


def customization_from_settings(settings: dict) -> dict:
    font_family = settings.get("console_font_family", "Consolas")
    if font_family not in CONSOLE_FONT_FAMILIES:
        font_family = "Consolas"
    developer_mode = settings.get("developer_mode", False) is True
    developer_console_enabled = developer_mode and settings.get("developer_console_enabled", developer_mode) is True
    language = settings.get("language", "pt")
    if language not in ("pt", "en"):
        language = "pt"
    return {
        "language": language,
        "server_name": _valid_server_name(settings.get("server_name")),
        "server_host": (parse_server_address(
            settings.get("server_host", SERVER_HOST), settings.get("server_port", SERVER_PORT)
        ) or (SERVER_HOST, SERVER_PORT))[0],
        "server_port": (parse_server_address(
            settings.get("server_host", SERVER_HOST), settings.get("server_port", SERVER_PORT)
        ) or (SERVER_HOST, SERVER_PORT))[1],
        "poll_interval_seconds": _bounded_int(
            settings.get("poll_interval_seconds", POLL_INTERVAL_SECONDS),
            POLL_INTERVAL_SECONDS,
            1 if developer_mode else MIN_POLL_INTERVAL_SECONDS,
            MAX_DEVELOPER_POLL_INTERVAL_SECONDS if developer_mode else MAX_POLL_INTERVAL_SECONDS,
        ),
        "console_background": _valid_hex_color(
            settings.get("console_background"), DEFAULT_CONSOLE_BACKGROUND
        ),
        "console_foreground": _valid_hex_color(
            settings.get("console_foreground"), DEFAULT_CONSOLE_FOREGROUND
        ),
        "console_font_family": font_family,
        "console_font_size": _bounded_int(settings.get("console_font_size", 9), 9, 8, 20),
        "console_auto_scroll": settings.get("console_auto_scroll", True) is True,
        "console_wrap_lines": settings.get("console_wrap_lines", False) is True,
        "developer_mode": developer_mode,
        "developer_console_enabled": developer_console_enabled,
        "availability_history_enabled": settings.get("availability_history_enabled", True) is True,
        "history_record_offline": settings.get("history_record_offline", True) is True,
        "history_record_online": settings.get("history_record_online", True) is True,
    }


def history_from_settings(settings: dict) -> list[dict[str, str]]:
    history = settings.get("availability_history", [])
    if not isinstance(history, list):
        return []
    cleaned = []
    for entry in reversed(history):
        if not isinstance(entry, dict):
            continue
        timestamp = entry.get("timestamp")
        event = entry.get("event")
        if isinstance(timestamp, str) and event in ("online", "offline"):
            cleaned.append({"timestamp": timestamp[:32], "event": event})
            if len(cleaned) >= MAX_HISTORY_ENTRIES:
                break
    return list(reversed(cleaned))


def server_histories_from_settings(
    settings: dict, servers: list[dict[str, object]], selected_server_id: str
) -> dict[str, list[dict[str, str]]]:
    saved = settings.get("server_histories", {})
    histories = {}
    if isinstance(saved, dict):
        for server in servers:
            server_id = str(server["id"])
            histories[server_id] = history_from_settings({
                "availability_history": saved.get(server_id, [])
            })
    if not any(histories.values()) and settings.get("availability_history"):
        histories[selected_server_id] = history_from_settings(settings)
    return histories


def append_history_event(history: list[dict[str, str]], event: str, timestamp: str) -> list[dict[str, str]]:
    if event not in ("online", "offline"):
        return history[-MAX_HISTORY_ENTRIES:]
    updated = [*history, {"timestamp": timestamp[:32], "event": event}]
    return updated[-MAX_HISTORY_ENTRIES:]


def bad_players_from_settings(settings: dict) -> list[str]:
    saved_names = settings.get("bad_players", [])
    if not isinstance(saved_names, list):
        return []
    names = []
    seen = set()
    for value in saved_names:
        if not isinstance(value, str):
            continue
        name = value.strip()[:128]
        key = name.casefold()
        if name and key not in seen:
            names.append(name)
            seen.add(key)
            if len(names) >= MAX_BAD_PLAYERS:
                break
    return names


def format_player_duration(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        return "—"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


class StateTracker:
    """Debounce transient UDP loss before declaring offline or recovered."""

    def __init__(self) -> None:
        self.stable_state: Optional[str] = None
        self.failure_streak = 0
        self.success_streak = 0

    def observe(self, online: bool) -> tuple[str, Optional[str]]:
        event: Optional[str] = None
        if online:
            self.success_streak += 1
            self.failure_streak = 0
            if self.stable_state is None:
                self.stable_state = "online"
            elif self.stable_state == "offline" and self.success_streak >= SUCCESSES_TO_RECOVER:
                self.stable_state = "online"
                event = "online"
        else:
            self.failure_streak += 1
            self.success_streak = 0
            if self.stable_state is None and self.failure_streak >= FAILURES_TO_OFFLINE:
                self.stable_state = "offline"
            elif self.stable_state == "online" and self.failure_streak >= FAILURES_TO_OFFLINE:
                self.stable_state = "offline"
                event = "offline"
        return self.stable_state or "unknown", event


class MonitorWorker(threading.Thread):
    def __init__(
        self,
        output_queue: queue.Queue,
        stop_event: threading.Event,
        interval_seconds: int = POLL_INTERVAL_SECONDS,
        host: str = SERVER_HOST,
        port: int = SERVER_PORT,
        developer_mode: bool = False,
        server_id: str = DEFAULT_SERVER_ID,
    ) -> None:
        super().__init__(daemon=True, name=f"A2S-monitor-{server_id[:24]}")
        self.output_queue = output_queue
        self.stop_event = stop_event
        self.server_id = server_id
        self.developer_mode = developer_mode
        self.interval_seconds = _bounded_int(
            interval_seconds,
            POLL_INTERVAL_SECONDS,
            1 if developer_mode else MIN_POLL_INTERVAL_SECONDS,
            MAX_DEVELOPER_POLL_INTERVAL_SECONDS if developer_mode else MAX_POLL_INTERVAL_SECONDS,
        )
        self.schedule_changed = threading.Event()
        self.target_changed = threading.Event()
        self.target_lock = threading.Lock()
        self.host = host
        self.port = port
        self.target_generation = 0
        self.tracker = StateTracker()
        self.cached_player_list: Optional[list[PlayerInfo]] = None
        self.cached_player_error = "Waiting to query the player list."
        self.last_player_query_at: Optional[float] = None

    def set_target(self, host: str, port: int) -> None:
        with self.target_lock:
            if (host, port) == (self.host, self.port):
                return
            self.host = host
            self.port = port
            self.target_generation += 1
            self.tracker = StateTracker()
            self.cached_player_list = None
            self.cached_player_error = "Waiting to query the player list."
            self.last_player_query_at = None
        self.target_changed.set()
        self.schedule_changed.set()

    def _target_snapshot(self) -> tuple[str, int, int]:
        with self.target_lock:
            return self.host, self.port, self.target_generation

    def set_interval_seconds(self, seconds: int, developer_mode: Optional[bool] = None) -> None:
        if developer_mode is not None:
            self.developer_mode = developer_mode
        self.interval_seconds = _bounded_int(
            seconds,
            POLL_INTERVAL_SECONDS,
            1 if self.developer_mode else MIN_POLL_INTERVAL_SECONDS,
            MAX_DEVELOPER_POLL_INTERVAL_SECONDS if self.developer_mode else MAX_POLL_INTERVAL_SECONDS,
        )
        self.schedule_changed.set()

    def run(self) -> None:
        next_check = time.monotonic()
        while not self.stop_event.is_set():
            delay = next_check - time.monotonic()
            if delay > 0:
                self.schedule_changed.wait(delay)
                if self.stop_event.is_set():
                    break
            if self.schedule_changed.is_set():
                self.schedule_changed.clear()
                if self.target_changed.is_set():
                    self.target_changed.clear()
                    next_check = time.monotonic()
                else:
                    next_check = time.monotonic() + self.interval_seconds
                continue
            checked_at = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            target_host, target_port, target_generation = self._target_snapshot()
            try:
                query_started = time.perf_counter()
                info = query_server(target_host, target_port)
                response_ms = round((time.perf_counter() - query_started) * 1000)
                with self.target_lock:
                    if target_generation != self.target_generation:
                        continue
                    refresh_players = (
                        self.last_player_query_at is None
                        or time.monotonic() - self.last_player_query_at >= PLAYER_QUERY_REFRESH_SECONDS
                    )
                refreshed_player_list = None
                refreshed_player_error = ""
                refreshed_player_at = None
                if refresh_players:
                    try:
                        refreshed_player_list = query_players(
                            target_host,
                            target_port,
                            timeout=PLAYER_QUERY_TIMEOUT_SECONDS,
                            attempts=1,
                        )
                    except QueryError as exc:
                        # A2S_PLAYER may be disabled while A2S_INFO still works.
                        refreshed_player_error = str(exc)
                    refreshed_player_at = time.monotonic()
                with self.target_lock:
                    if target_generation != self.target_generation:
                        continue
                    if refresh_players:
                        self.last_player_query_at = refreshed_player_at
                        if refreshed_player_list is not None:
                            self.cached_player_list = refreshed_player_list
                        self.cached_player_error = refreshed_player_error
                    player_list = self.cached_player_list
                    player_error = self.cached_player_error
                    stable_state, event = self.tracker.observe(True)
                    success_streak = self.tracker.success_streak
                payload = {
                    "server_id": self.server_id,
                    "ok": True,
                    "target_host": target_host,
                    "target_port": target_port,
                    "info": info,
                    "player_list": player_list,
                    "player_error": player_error,
                    "response_ms": response_ms,
                    "state": stable_state,
                    "event": event,
                    "failure_streak": 0,
                    "success_streak": success_streak,
                    "checked_at": checked_at,
                    "error": "",
                }
            except QueryError as exc:
                with self.target_lock:
                    if target_generation != self.target_generation:
                        continue
                    stable_state, event = self.tracker.observe(False)
                    failure_streak = self.tracker.failure_streak
                    player_list = self.cached_player_list
                    player_error = (
                        "The server did not respond; the list may be stale."
                        if player_list is not None
                        else "The server did not respond; the list is unavailable."
                    )
                payload = {
                    "server_id": self.server_id,
                    "ok": False,
                    "target_host": target_host,
                    "target_port": target_port,
                    "info": None,
                    "player_list": player_list,
                    "player_error": player_error,
                    "response_ms": None,
                    "state": stable_state,
                    "event": event,
                    "failure_streak": failure_streak,
                    "success_streak": 0,
                    "checked_at": checked_at,
                    "error": str(exc),
                }
            self.output_queue.put(payload)
            scheduled_next_check = next_check + self.interval_seconds
            completed_at = time.monotonic()
            next_check = (
                scheduled_next_check
                if scheduled_next_check > completed_at
                else completed_at + self.interval_seconds
            )


def _registry_module():
    if os.name != "nt":
        return None
    import winreg

    return winreg


def _run_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    python = Path(sys.executable).resolve()
    pythonw = python.with_name("pythonw.exe")
    launcher = pythonw if pythonw.exists() else python
    script = Path(__file__).resolve()
    return f'"{launcher}" "{script}"'


def autostart_enabled() -> bool:
    winreg = _registry_module()
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
        return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    winreg = _registry_module()
    if winreg is None:
        raise OSError("Automatic startup is only available on Windows.")
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _run_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


class MonitorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Monitor — Libby's Test Server")
        self.root.geometry("760x620")
        self.root.minsize(620, 500)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.output_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: Optional[MonitorWorker] = None
        self.workers: dict[str, MonitorWorker] = {}
        self.worker_stop_events: dict[str, threading.Event] = {}
        self.server_results: dict[str, dict] = {}
        self.last_status = "Waiting for the first response…"
        self.developer_log_entries: list[str] = []
        self.developer_log_states: dict[str, str] = {}
        self.settings = load_settings()
        self.customization = customization_from_settings(self.settings)
        self.servers = servers_from_settings(self.settings)
        stored_selected_id = self.settings.get("selected_server_id")
        server_ids = {str(server["id"]) for server in self.servers}
        self.selected_server_id = (
            stored_selected_id
            if isinstance(stored_selected_id, str) and stored_selected_id in server_ids
            else str(self.servers[0]["id"])
        )
        self.selected_server = next(server for server in self.servers if str(server["id"]) == self.selected_server_id)
        self.customization.update({
            "server_name": self.selected_server["name"],
            "server_host": self.selected_server["host"],
            "server_port": self.selected_server["port"],
        })
        self.developer_mode_enabled = self.customization["developer_mode"]
        self.developer_console_enabled = self.customization["developer_console_enabled"]
        self.language = self.customization["language"]
        self.history_enabled = self.customization["availability_history_enabled"]
        self.history_record_offline = self.customization["history_record_offline"]
        self.history_record_online = self.customization["history_record_online"]
        self.server_name = str(self.selected_server["name"])
        self.server_host = str(self.selected_server["host"])
        self.server_port = int(self.selected_server["port"])
        self.poll_interval_seconds = self.customization["poll_interval_seconds"]
        self.server_name_var = tk.StringVar(root, value=self.server_name)
        self.server_host_var = tk.StringVar(root, value=self.server_host)
        self.server_port_var = tk.StringVar(root, value=str(self.server_port))
        self.poll_interval_var = tk.StringVar(
            root, value=str(self.customization["poll_interval_seconds"])
        )
        self.console_background_var = tk.StringVar(
            root, value=self.customization["console_background"]
        )
        self.console_foreground_var = tk.StringVar(
            root, value=self.customization["console_foreground"]
        )
        self.console_font_family_var = tk.StringVar(
            root, value=self.customization["console_font_family"]
        )
        self.console_font_size_var = tk.StringVar(
            root, value=str(self.customization["console_font_size"])
        )
        self.console_auto_scroll_var = tk.BooleanVar(
            root, value=self.customization["console_auto_scroll"]
        )
        self.console_wrap_lines_var = tk.BooleanVar(
            root, value=self.customization["console_wrap_lines"]
        )
        self.developer_mode_var = tk.BooleanVar(
            root, value=self.customization["developer_mode"]
        )
        self.developer_console_var = tk.BooleanVar(
            root, value=self.customization["developer_console_enabled"]
        )
        self.language_var = tk.StringVar(
            root, value="Portuguese" if self.language == "pt" else "English"
        )
        self.history_enabled_var = tk.BooleanVar(
            root, value=self.customization["availability_history_enabled"]
        )
        self.history_record_offline_var = tk.BooleanVar(
            root, value=self.customization["history_record_offline"]
        )
        self.history_record_online_var = tk.BooleanVar(
            root, value=self.customization["history_record_online"]
        )
        self.console_auto_scroll = self.customization["console_auto_scroll"]
        saved_log_path = self.settings.get("console_log_path", "")
        self.console_log_path = Path(saved_log_path) if saved_log_path else None
        self.console_tailer = ConsoleLogTailer(self.console_log_path) if self.console_log_path else None
        self.server_histories = server_histories_from_settings(
            self.settings, self.servers, self.selected_server_id
        )
        self.availability_history = self.server_histories.get(self.selected_server_id, [])
        self.bad_players = bad_players_from_settings(self.settings)
        self.live_players: Optional[list[PlayerInfo]] = None
        self.live_players_error = "Waiting for the first check."

        self._build_ui()
        self._dev_log(
            "APP",
            f"App started; monitoring {self.server_host}:{self.server_port} every {self.poll_interval_seconds}s.",
        )
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        self.autostart_box.configure(variable=self.autostart_var)
        if os.name == "nt" and not autostart_enabled():
            # The requested default is to start with Windows on first launch.
            try:
                set_autostart(True)
                self.autostart_var.set(True)
            except OSError as exc:
                self.startup_message.set(f"Could not enable automatic startup: {exc}")
                self._dev_log("STARTUP", f"Could not enable automatic startup: {exc}", level="WARN")
        self._refresh_server_list()
        for server in self.servers:
            self._start_server_worker(server)
        self.worker = self.workers.get(self.selected_server_id)
        self._dev_log("MONITOR", "A2S monitors started for all servers.")
        self.root.after(200, self._drain_results)
        self.root.after(500, self._poll_console_log)

    def _build_ui(self) -> None:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)
        servers_tab = ttk.Frame(self.notebook, padding=(16, 14))
        monitor_tab = ttk.Frame(self.notebook, padding=(24, 22))
        players_tab = ttk.Frame(self.notebook, padding=(16, 14))
        bad_players_tab = ttk.Frame(self.notebook, padding=(16, 14))
        console_tab = ttk.Frame(self.notebook, padding=(16, 14))
        history_tab = ttk.Frame(self.notebook, padding=(16, 14))
        customization_tab = ttk.Frame(self.notebook, padding=(20, 18))
        self.customization_tab = customization_tab
        self.developer_tab = ttk.Frame(self.notebook, padding=(16, 14))
        self.notebook.add(servers_tab, text="Servers")
        self.notebook.add(monitor_tab, text="Monitor")
        self.notebook.add(players_tab, text="Players")
        self.notebook.add(bad_players_tab, text="Bad Players")
        self.notebook.add(console_tab, text="Console")
        self.notebook.add(history_tab, text="History")
        self.notebook.add(customization_tab, text="Customization")
        if self.developer_mode_enabled and self.developer_console_enabled:
            self.notebook.add(self.developer_tab, text="Developer")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        outer = monitor_tab
        self.server_title_label = ttk.Label(outer, text=self.server_name, font=("Segoe UI", 17, "bold"))
        self.server_title_label.pack(anchor="w")
        self.server_address_label = ttk.Label(
            outer,
            text=f"Garry's Mod  ·  {self.server_host}:{self.server_port}",
            font=("Segoe UI", 10),
        )
        self.server_address_label.pack(anchor="w", pady=(3, 18))

        status_frame = ttk.LabelFrame(outer, text="Server status", padding=(16, 12))
        status_frame.pack(fill="x")
        self.status_label = ttk.Label(status_frame, text=self.last_status, font=("Segoe UI", 14, "bold"))
        self.status_label.pack(anchor="w")
        self.detail_label = ttk.Label(status_frame, text="The first check is in progress.")
        self.detail_label.pack(anchor="w", pady=(5, 0))

        info_frame = ttk.LabelFrame(outer, text="Information", padding=(16, 12))
        info_frame.pack(fill="x", pady=(14, 0))
        self.players_label = ttk.Label(info_frame, text="Players: —")
        self.players_label.pack(anchor="w")
        self.map_label = ttk.Label(info_frame, text="Map: —")
        self.map_label.pack(anchor="w", pady=(4, 0))
        self.version_label = ttk.Label(info_frame, text="Version: —")
        self.version_label.pack(anchor="w", pady=(4, 0))
        self.response_label = ttk.Label(info_frame, text="A2S response time: —")
        self.response_label.pack(anchor="w", pady=(4, 0))

        prefs = ttk.Frame(outer)
        prefs.pack(fill="x", pady=(16, 0))
        self.autostart_box = ttk.Checkbutton(
            prefs,
            text="Start automatically with Windows",
            command=self._change_autostart,
        )
        self.autostart_box.pack(anchor="w")
        self.startup_message = tk.StringVar(value="")
        ttk.Label(prefs, textvariable=self.startup_message, foreground="#8a4b00", wraplength=500).pack(
            anchor="w", pady=(3, 4)
        )
        ttk.Separator(outer).pack(fill="x", pady=(14, 8))
        self.checked_label = ttk.Label(outer, text="Last check: —")
        self.checked_label.pack(anchor="w")
        self.check_interval_label = ttk.Label(
            outer,
            text=self._check_interval_text(),
            wraplength=520,
            foreground="#555555",
        )
        self.check_interval_label.pack(anchor="w", pady=(9, 0))

        self._build_servers_tab(servers_tab)
        self._build_players_tab(players_tab)
        self._build_bad_players_tab(bad_players_tab)
        self._build_console_tab(console_tab)
        self._build_history_tab(history_tab)
        self._build_customization_tab(customization_tab)
        self._build_developer_tab(self.developer_tab)

    def _build_servers_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Monitored servers", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(
            parent,
            text="All saved servers are checked in parallel. Select a server to show its details in the other tabs.",
            wraplength=690,
            justify="left",
        ).pack(anchor="w", pady=(5, 12))

        form = ttk.LabelFrame(parent, text="Add server", padding=(12, 9))
        form.pack(fill="x", pady=(0, 10))
        ttk.Label(form, text="Name").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.server_add_name_var = tk.StringVar(value="")
        ttk.Entry(form, textvariable=self.server_add_name_var, width=24).grid(
            row=0, column=1, sticky="ew", pady=4
        )
        ttk.Label(form, text="Address (IP/hostname:port)").grid(
            row=0, column=2, sticky="w", padx=(12, 8), pady=4
        )
        self.server_add_address_var = tk.StringVar(value="")
        address_entry = ttk.Entry(form, textvariable=self.server_add_address_var)
        address_entry.grid(row=0, column=3, sticky="ew", pady=4)
        address_entry.bind("<Return>", self._add_server)
        ttk.Button(form, text="Add", command=self._add_server).grid(
            row=0, column=4, sticky="e", padx=(8, 0), pady=4
        )
        form.columnconfigure(3, weight=1)

        list_frame = ttk.Frame(parent)
        list_frame.pack(fill="both", expand=True)
        columns = ("name", "address", "status", "players")
        self.servers_tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=14)
        for column, heading in (
            ("name", "Server"), ("address", "Address"), ("status", "Status"), ("players", "Players")
        ):
            self.servers_tree.heading(column, text=heading)
        self.servers_tree.column("name", width=180, anchor="w")
        self.servers_tree.column("address", width=230, anchor="w")
        self.servers_tree.column("status", width=125, anchor="center")
        self.servers_tree.column("players", width=90, anchor="center")
        vertical = ttk.Scrollbar(list_frame, orient="vertical", command=self.servers_tree.yview)
        self.servers_tree.configure(yscrollcommand=vertical.set)
        self.servers_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.servers_tree.bind("<<TreeviewSelect>>", self._on_server_list_selection)

        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(10, 0))
        ttk.Button(controls, text="Remove selected", command=self._remove_server).pack(side="left")
        self.servers_status = tk.StringVar(value="")
        ttk.Label(controls, textvariable=self.servers_status, foreground="#555555").pack(
            side="left", padx=(12, 0)
        )

    def _server_record(self, server_id: str) -> Optional[dict[str, object]]:
        return next((server for server in self.servers if str(server["id"]) == server_id), None)

    def _sync_server_settings(self) -> None:
        self.settings["servers"] = [dict(server) for server in self.servers]
        self.settings["selected_server_id"] = self.selected_server_id
        self.settings["server_histories"] = {
            server_id: list(entries) for server_id, entries in self.server_histories.items()
        }
        self.settings["server_name"] = self.server_name
        self.settings["server_host"] = self.server_host
        self.settings["server_port"] = self.server_port
        self.settings["availability_history"] = self.availability_history

    def _save_server_settings(self) -> None:
        self._sync_server_settings()
        try:
            save_settings(self.settings)
            if hasattr(self, "servers_status"):
                self.servers_status.set("Server list saved.")
        except OSError as exc:
            if hasattr(self, "servers_status"):
                self.servers_status.set(f"Could not save the server list: {exc}")

    def _start_server_worker(self, server: dict[str, object]) -> None:
        server_id = str(server["id"])
        if server_id in self.workers:
            return
        stop_event = threading.Event()
        worker = MonitorWorker(
            self.output_queue,
            stop_event,
            self.poll_interval_seconds,
            host=str(server["host"]),
            port=int(server["port"]),
            developer_mode=self.developer_mode_enabled,
            server_id=server_id,
        )
        self.worker_stop_events[server_id] = stop_event
        self.workers[server_id] = worker
        worker.start()

    def _stop_server_worker(self, server_id: str) -> None:
        stop_event = self.worker_stop_events.pop(server_id, None)
        worker = self.workers.pop(server_id, None)
        if stop_event is not None:
            stop_event.set()
        if worker is not None:
            worker.schedule_changed.set()
        self.server_results.pop(server_id, None)

    def _server_row_values(self, server: dict[str, object]) -> tuple[str, str, str, str]:
        server_id = str(server["id"])
        result = self.server_results.get(server_id)
        address = f"{server['host']}:{server['port']}"
        if result is None:
            return str(server["name"]), address, "Waiting", "—"
        state = result.get("state", "unknown")
        if state == "offline":
            status = "Unresponsive"
        elif result.get("ok") and state == "online":
            status = "Online"
        elif state == "online":
            status = "Online · checking"
        else:
            status = "Checking"
        info = result.get("info")
        player_text = f"{info.players}/{info.max_players}" if info is not None else "—"
        return str(server["name"]), address, status, player_text

    def _refresh_server_list(self) -> None:
        if not hasattr(self, "servers_tree"):
            return
        self._refreshing_server_list = True
        try:
            for item in self.servers_tree.get_children():
                self.servers_tree.delete(item)
            for server in self.servers:
                server_id = str(server["id"])
                self.servers_tree.insert(
                    "", "end", iid=server_id, values=self._server_row_values(server)
                )
            if self.selected_server_id in {str(server["id"]) for server in self.servers}:
                self.servers_tree.selection_set(self.selected_server_id)
                self.servers_tree.focus(self.selected_server_id)
        finally:
            self._refreshing_server_list = False

    def _update_server_list_row(self, server_id: str) -> None:
        if not hasattr(self, "servers_tree"):
            return
        server = self._server_record(server_id)
        if server is None:
            return
        if not self.servers_tree.exists(server_id):
            self._refresh_server_list()
            return
        self.servers_tree.item(server_id, values=self._server_row_values(server))

    def _on_server_list_selection(self, _event=None) -> None:
        if getattr(self, "_refreshing_server_list", False) or not hasattr(self, "servers_tree"):
            return
        selected = self.servers_tree.selection()
        if selected and selected[0] != self.selected_server_id:
            self._select_server(selected[0])

    def _select_server(self, server_id: str) -> None:
        server = self._server_record(server_id)
        if server is None:
            return
        self.selected_server_id = server_id
        self.selected_server = server
        self.server_name = str(server["name"])
        self.server_host = str(server["host"])
        self.server_port = int(server["port"])
        self.worker = self.workers.get(server_id)
        self.server_name_var.set(self.server_name)
        self.server_host_var.set(self.server_host)
        self.server_port_var.set(str(self.server_port))
        self.customization.update({
            "server_name": self.server_name,
            "server_host": self.server_host,
            "server_port": self.server_port,
        })
        self.availability_history = self.server_histories.get(server_id, [])
        if hasattr(self, "server_title_label"):
            self.server_title_label.configure(text=self.server_name)
            self.server_address_label.configure(text=f"Garry's Mod  ·  {self.server_host}:{self.server_port}")
            self.check_interval_label.configure(text=self._check_interval_text())
            result = self.server_results.get(server_id)
            if result is None:
                self._reset_selected_server_details()
            else:
                self._render_result({**result, "event": None})
            self._refresh_history_view()
        self._save_server_settings()
        self._dev_log("SERVERS", f"Selected server: {self.server_name} ({self.server_host}:{self.server_port}).")

    def _reset_selected_server_details(self) -> None:
        self.live_players = None
        self.live_players_error = "Waiting for the first check."
        self.last_status = "Waiting for the first response…"
        self.checked_label.configure(text="Last check: —")
        self.status_label.configure(text=self.last_status)
        self.detail_label.configure(text="The first check is in progress.")
        self.players_label.configure(text="Players: —")
        self.map_label.configure(text="Map: —")
        self.version_label.configure(text="Version: —")
        self.response_label.configure(text="A2S response time: —")
        self._update_players(None, self.live_players_error)

    def _add_server(self, _event=None) -> None:
        name = self.server_add_name_var.get().strip()[:64]
        endpoint = parse_server_address(self.server_add_address_var.get(), SERVER_PORT)
        if endpoint is None:
            self.servers_status.set("Enter a valid IPv4/hostname and port (1–65535).")
            return
        host, port = endpoint
        if not name:
            name = host
        if any((str(item["host"]), int(item["port"])) == (host, port) for item in self.servers):
            self.servers_status.set("That address is already on the list.")
            return
        if len(self.servers) >= MAX_SERVERS:
            self.servers_status.set(f"The list has reached its {MAX_SERVERS}-server limit.")
            return
        server = {"id": uuid.uuid4().hex, "name": name, "host": host, "port": port}
        self.servers.append(server)
        self.server_histories[server["id"]] = []
        self.server_add_name_var.set("")
        self.server_add_address_var.set("")
        self._start_server_worker(server)
        self._refresh_server_list()
        self.servers_tree.selection_set(server["id"])
        self._select_server(server["id"])
        self.servers_status.set("Server added; monitoring started.")
        self._dev_log("SERVERS", f"Server added: {name} ({host}:{port}).")

    def _remove_server(self) -> None:
        selected = self.servers_tree.selection()
        if not selected:
            self.servers_status.set("Select a server to remove.")
            return
        if len(self.servers) <= 1:
            self.servers_status.set("Keep at least one server in the list.")
            return
        server_id = selected[0]
        server = self._server_record(server_id)
        self._stop_server_worker(server_id)
        self.servers = [item for item in self.servers if str(item["id"]) != server_id]
        self.server_histories.pop(server_id, None)
        if server_id == self.selected_server_id:
            self.selected_server_id = str(self.servers[0]["id"])
        self._refresh_server_list()
        self._select_server(self.selected_server_id)
        if server is not None:
            self._dev_log("SERVERS", f"Server removed: {server['name']}.")
        self.servers_status.set("Server removed.")

    def _check_interval_text(self) -> str:
        return (
            f"Checking {self.server_host}:{self.server_port} every {self.poll_interval_seconds} seconds. "
            "Three consecutive checks without a response are needed to mark it offline; this does not confirm a crash."
        )

    def _build_customization_tab(self, parent: ttk.Frame) -> None:
        actions = ttk.Frame(parent, padding=(8, 6))
        actions.pack(side="bottom", fill="x", padx=8, pady=(4, 6))
        actions.columnconfigure(1, weight=1)
        apply_button = ttk.Button(
            actions,
            text="Apply and Save",
            command=self._apply_customizations,
            width=18,
            padding=(12, 7),
        )
        apply_button.grid(row=0, column=0, sticky="w")
        self.customization_status = tk.StringVar(value="")
        status_label = ttk.Label(
            actions,
            textvariable=self.customization_status,
            foreground="#555555",
            anchor="w",
            justify="left",
        )
        status_label.grid(row=0, column=1, sticky="ew", padx=(12, 0))
        actions.bind(
            "<Configure>",
            lambda event: status_label.configure(
                wraplength=max(120, event.width - apply_button.winfo_reqwidth() - 28)
            ),
        )

        scroll_area = ttk.Frame(parent)
        scroll_area.pack(side="top", fill="both", expand=True)
        customization_canvas = tk.Canvas(scroll_area, borderwidth=0, highlightthickness=0)
        customization_scrollbar = ttk.Scrollbar(
            scroll_area, orient="vertical", command=customization_canvas.yview
        )
        customization_canvas.configure(yscrollcommand=customization_scrollbar.set)
        customization_canvas.pack(side="left", fill="both", expand=True)
        customization_scrollbar.pack(side="right", fill="y")
        content = ttk.Frame(customization_canvas, padding=(20, 18))
        content_window = customization_canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind(
            "<Configure>",
            lambda _event: customization_canvas.configure(
                scrollregion=customization_canvas.bbox("all")
            ),
        )
        customization_canvas.bind(
            "<Configure>",
            lambda event: customization_canvas.itemconfigure(content_window, width=event.width),
        )

        ttk.Label(content, text="Customize the app", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(
            content,
            text="Changes are saved on this computer and applied when you click Apply and Save.",
            wraplength=690,
            foreground="#555555",
        ).pack(anchor="w", pady=(4, 14))

        language_frame = ttk.LabelFrame(content, text="Language", padding=(14, 9))
        language_frame.pack(fill="x", pady=(0, 12))
        ttk.Label(language_frame, text="App language").pack(side="left")
        ttk.Combobox(
            language_frame,
            textvariable=self.language_var,
            values=("Portuguese", "English"),
            state="readonly",
            width=18,
        ).pack(side="left", padx=(10, 0))

        server_frame = ttk.LabelFrame(content, text="Server to monitor", padding=(14, 10))
        server_frame.pack(fill="x", pady=(0, 12))
        ttk.Label(server_frame, text="Display name").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(server_frame, textvariable=self.server_name_var).grid(
            row=0, column=1, columnspan=3, sticky="ew", pady=3
        )
        ttk.Label(server_frame, text="Server IP").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(server_frame, textvariable=self.server_host_var).grid(
            row=1, column=1, sticky="ew", pady=3
        )
        ttk.Label(server_frame, text="Port").grid(row=1, column=2, sticky="w", padx=(12, 6), pady=3)
        ttk.Spinbox(
            server_frame,
            from_=1,
            to=65535,
            textvariable=self.server_port_var,
            width=8,
        ).grid(row=1, column=3, sticky="w", pady=3)
        server_frame.columnconfigure(1, weight=1)

        interval_frame = ttk.LabelFrame(content, text="Monitoring", padding=(14, 10))
        interval_frame.pack(fill="x", pady=(0, 12))
        interval_row = ttk.Frame(interval_frame)
        interval_row.pack(fill="x")
        ttk.Label(interval_row, text="Check every").pack(side="left")
        self.poll_interval_spinbox = ttk.Spinbox(
            interval_row,
            from_=1 if self.developer_mode_enabled else MIN_POLL_INTERVAL_SECONDS,
            to=(MAX_DEVELOPER_POLL_INTERVAL_SECONDS if self.developer_mode_enabled else MAX_POLL_INTERVAL_SECONDS),
            increment=1,
            width=10,
            textvariable=self.poll_interval_var,
        )
        self.poll_interval_spinbox.pack(side="left", padx=(8, 6))
        self.poll_interval_range_label = ttk.Label(interval_row)
        self.poll_interval_range_label.pack(side="left")
        self._update_poll_interval_bounds(self.developer_mode_enabled)

        appearance = ttk.LabelFrame(content, text="Console appearance", padding=(14, 10))
        appearance.pack(fill="x", pady=(0, 12))
        font_row = ttk.Frame(appearance)
        font_row.pack(fill="x", pady=(0, 10))
        ttk.Label(font_row, text="Font").pack(side="left")
        ttk.Combobox(
            font_row,
            textvariable=self.console_font_family_var,
            values=CONSOLE_FONT_FAMILIES,
            state="readonly",
            width=20,
        ).pack(side="left", padx=(8, 18))
        ttk.Label(font_row, text="Size").pack(side="left")
        ttk.Spinbox(
            font_row,
            from_=8,
            to=20,
            increment=1,
            width=5,
            textvariable=self.console_font_size_var,
        ).pack(side="left", padx=(8, 5))

        colors_row = ttk.Frame(appearance)
        colors_row.pack(fill="x", pady=(0, 8))
        ttk.Label(colors_row, text="Background").pack(side="left")
        ttk.Button(
            colors_row,
            text="Choose color",
            command=lambda: self._choose_console_color("background"),
        ).pack(side="left", padx=(8, 5))
        self.console_background_swatch = tk.Label(
            colors_row,
            text=self.console_background_var.get(),
            background=self.console_background_var.get(),
            foreground="#ffffff",
            width=11,
            relief="groove",
        )
        self.console_background_swatch.pack(side="left", padx=(0, 18))
        ttk.Label(colors_row, text="Text").pack(side="left")
        ttk.Button(
            colors_row,
            text="Choose color",
            command=lambda: self._choose_console_color("foreground"),
        ).pack(side="left", padx=(8, 5))
        self.console_foreground_swatch = tk.Label(
            colors_row,
            text=self.console_foreground_var.get(),
            background=self.console_foreground_var.get(),
            foreground="#111111",
            width=11,
            relief="groove",
        )
        self.console_foreground_swatch.pack(side="left")

        ttk.Checkbutton(
            appearance,
            text="Wrap long lines",
            variable=self.console_wrap_lines_var,
        ).pack(anchor="w", pady=(2, 2))
        ttk.Checkbutton(
            appearance,
            text="Automatically scroll to new messages",
            variable=self.console_auto_scroll_var,
        ).pack(anchor="w")

        developer_frame = ttk.LabelFrame(content, text="Developer mode", padding=(14, 9))
        developer_frame.pack(fill="x", pady=(0, 10))
        ttk.Checkbutton(
            developer_frame,
            text="Enable developer mode",
            variable=self.developer_mode_var,
            command=self._on_developer_mode_toggle,
        ).pack(anchor="w")
        self.developer_console_check = ttk.Checkbutton(
            developer_frame,
            text="Enable developer console",
            variable=self.developer_console_var,
        )
        self.developer_console_check.pack(anchor="w", padx=(22, 0), pady=(3, 0))
        if not self.developer_mode_enabled:
            self.developer_console_check.configure(state="disabled")
        ttk.Label(
            developer_frame,
            text="Developer mode unlocks advanced options. Check this to show or hide the Developer tab.",
            wraplength=650,
            foreground="#555555",
        ).pack(anchor="w", padx=(22, 0), pady=(2, 0))

        history_settings_frame = ttk.LabelFrame(content, text="History", padding=(14, 9))
        history_settings_frame.pack(fill="x", pady=(0, 10))
        ttk.Checkbutton(
            history_settings_frame,
            text="Enable history",
            variable=self.history_enabled_var,
            command=self._on_history_enabled_toggle,
        ).pack(anchor="w")
        ttk.Label(
            history_settings_frame,
            text="Filter history: choose which new changes to record. Existing entries will be preserved.",
            wraplength=650,
            foreground="#555555",
        ).pack(anchor="w", padx=(22, 0), pady=(2, 4))
        self.history_record_offline_check = ttk.Checkbutton(
            history_settings_frame,
            text="Record when the server becomes unavailable",
            variable=self.history_record_offline_var,
        )
        self.history_record_offline_check.pack(anchor="w", padx=(22, 0))
        self.history_record_online_check = ttk.Checkbutton(
            history_settings_frame,
            text="Record when the server comes back online",
            variable=self.history_record_online_var,
        )
        self.history_record_online_check.pack(anchor="w", padx=(22, 0))
        self._on_history_enabled_toggle()

    def _build_developer_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Developer console", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(
            parent,
            text=(
                "Follow app actions and updates. Each line describes an action the app performed; "
                "it does not show the complete source code."
            ),
            wraplength=690,
            justify="left",
            foreground="#555555",
        ).pack(anchor="w", pady=(5, 10))
        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(0, 8))
        ttk.Button(controls, text="Clear console", command=self._clear_developer_log).pack(side="left")
        self.developer_log_status = tk.StringVar(value="Waiting for events…")
        ttk.Label(controls, textvariable=self.developer_log_status, foreground="#555555").pack(
            side="left", padx=(12, 0)
        )
        log_frame = ttk.Frame(parent)
        log_frame.pack(fill="both", expand=True)
        self.developer_text = tk.Text(
            log_frame,
            wrap="none",
            state="disabled",
            font=(self.customization["console_font_family"], self.customization["console_font_size"]),
            background=self.customization["console_background"],
            foreground=self.customization["console_foreground"],
            insertbackground=self.customization["console_foreground"],
            relief="sunken",
            borderwidth=1,
        )
        if self.customization["console_wrap_lines"]:
            self.developer_text.configure(wrap="word")
        vertical = ttk.Scrollbar(log_frame, orient="vertical", command=self.developer_text.yview)
        horizontal = ttk.Scrollbar(log_frame, orient="horizontal", command=self.developer_text.xview)
        self.developer_text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.developer_text.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

    def _on_tab_changed(self, _event=None) -> None:
        if not hasattr(self, "notebook"):
            return
        selected = self.notebook.select()
        if not selected:
            return
        try:
            label = self.notebook.tab(selected, "text")
        except tk.TclError:
            return
        self._dev_log("UI", f"Tab selected: {label}.")

    def _dev_log(self, category: str, message: str, level: str = "INFO") -> None:
        safe_message = str(message).replace("\r", " ").replace("\n", " ")
        timestamp = datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{timestamp}] [{level.upper()}] [{category}] {safe_message}\n"
        self.developer_log_entries.append(line)
        if len(self.developer_log_entries) > DEVELOPER_LOG_MAX_LINES:
            del self.developer_log_entries[: len(self.developer_log_entries) - DEVELOPER_LOG_MAX_LINES]
        if hasattr(self, "developer_text"):
            self.developer_text.configure(state="normal")
            self.developer_text.insert("end", line)
            end_line = int(self.developer_text.index("end-1c").split(".")[0])
            excess_lines = end_line - DEVELOPER_LOG_MAX_LINES
            if excess_lines > 0:
                self.developer_text.delete("1.0", f"{excess_lines + 1}.0")
            if self.console_auto_scroll:
                self.developer_text.see("end")
            self.developer_text.configure(state="disabled")
            if hasattr(self, "developer_log_status"):
                self.developer_log_status.set(f"{len(self.developer_log_entries)} recent events")

    def _dev_log_state(self, key: str, message: str, level: str = "INFO") -> None:
        if self.developer_log_states.get(key) == message:
            return
        self.developer_log_states[key] = message
        self._dev_log("FILE", message, level=level)

    def _clear_developer_log(self) -> None:
        self.developer_log_entries.clear()
        if hasattr(self, "developer_text"):
            self.developer_text.configure(state="normal")
            self.developer_text.delete("1.0", "end")
            self.developer_text.configure(state="disabled")
        self._dev_log("DEV", "Console cleared.")

    def _set_developer_mode(self, enabled: bool) -> None:
        if enabled == self.developer_mode_enabled:
            return
        self.developer_mode_enabled = enabled
        self._update_poll_interval_bounds(enabled)
        if enabled:
            self._dev_log("DEV", "Developer mode enabled; advanced options unlocked.")
        else:
            self._dev_log("DEV", "Developer mode disabled.")

    def _sync_developer_console_tab(self) -> None:
        should_show = self.developer_mode_enabled and self.developer_console_enabled
        is_shown = str(self.developer_tab) in self.notebook.tabs()
        if should_show and not is_shown:
            self.notebook.add(self.developer_tab, text="Developer")
            self._dev_log("DEV", "Developer console enabled.")
        elif not should_show and is_shown:
            if self.notebook.select() == str(self.developer_tab):
                self.notebook.select(self.customization_tab)
            self.notebook.forget(self.developer_tab)
            self._dev_log("DEV", "Developer console disabled.")

    def _update_poll_interval_bounds(self, enabled: bool) -> None:
        if not hasattr(self, "poll_interval_spinbox"):
            return
        minimum = 1 if enabled else MIN_POLL_INTERVAL_SECONDS
        maximum = MAX_DEVELOPER_POLL_INTERVAL_SECONDS if enabled else MAX_POLL_INTERVAL_SECONDS
        self.poll_interval_spinbox.configure(from_=minimum, to=maximum)
        if enabled:
            range_text = f"seconds (1 to {maximum:,})"
        else:
            range_text = f"seconds ({minimum} to {maximum})"
        self.poll_interval_range_label.configure(text=range_text)
        try:
            current = int(self.poll_interval_var.get())
        except (TypeError, ValueError):
            return
        adjusted = min(max(current, minimum), maximum)
        if adjusted != current:
            self.poll_interval_var.set(str(adjusted))

    def _on_developer_mode_toggle(self) -> None:
        enabled = self.developer_mode_var.get()
        self._update_poll_interval_bounds(enabled)
        if hasattr(self, "developer_console_check"):
            self.developer_console_check.configure(state="normal" if enabled else "disabled")
        if not enabled:
            self.developer_console_var.set(False)

    def _on_history_enabled_toggle(self) -> None:
        state = "normal" if self.history_enabled_var.get() else "disabled"
        self.history_record_offline_check.configure(state=state)
        self.history_record_online_check.configure(state=state)

    def _build_players_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Connected players", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(
            parent,
            text="The player list is queried no more than once every 10 seconds; with longer check intervals, it updates with the monitor. Some servers hide player names or do not respond to player queries.",
            wraplength=690,
            justify="left",
        ).pack(anchor="w", pady=(5, 10))
        self.players_tab_status = tk.StringVar(value=self.live_players_error)
        ttk.Label(parent, textvariable=self.players_tab_status, foreground="#555555").pack(anchor="w", pady=(0, 8))

        table_frame = ttk.Frame(parent)
        table_frame.pack(fill="both", expand=True)
        columns = ("index", "name", "score", "duration", "flagged")
        self.players_tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        for column, heading in (
            ("index", "Index"),
            ("name", "Player"),
            ("score", "Score"),
            ("duration", "Time"),
            ("flagged", "Personal list"),
        ):
            self.players_tree.heading(column, text=heading)
        self.players_tree.column("index", width=48, anchor="center", stretch=False)
        self.players_tree.column("name", width=250, anchor="w")
        self.players_tree.column("score", width=85, anchor="center", stretch=False)
        self.players_tree.column("duration", width=110, anchor="center", stretch=False)
        self.players_tree.column("flagged", width=105, anchor="center", stretch=False)
        self.players_tree.tag_configure("on_list", background="#ffe7e7")
        vertical = ttk.Scrollbar(table_frame, orient="vertical", command=self.players_tree.yview)
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.players_tree.xview)
        self.players_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.players_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self._render_live_players()

    def _build_bad_players_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Bad Players", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(
            parent,
            text="Your personal name list. Add or remove names manually; if someone on the list is online, the Players tab will highlight that row.",
            wraplength=690,
            justify="left",
        ).pack(anchor="w", pady=(5, 12))

        input_row = ttk.Frame(parent)
        input_row.pack(fill="x", pady=(0, 10))
        self.bad_player_input = tk.StringVar(value="")
        entry = ttk.Entry(input_row, textvariable=self.bad_player_input)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", self._add_bad_player)
        ttk.Button(input_row, text="Add", command=self._add_bad_player).pack(side="left", padx=(8, 0))

        list_frame = ttk.Frame(parent)
        list_frame.pack(fill="both", expand=True)
        self.bad_players_tree = ttk.Treeview(list_frame, columns=("name",), show="headings", height=16)
        self.bad_players_tree.heading("name", text="Name on your list")
        self.bad_players_tree.column("name", anchor="w")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.bad_players_tree.yview)
        self.bad_players_tree.configure(yscrollcommand=scrollbar.set)
        self.bad_players_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(10, 0))
        ttk.Button(controls, text="Remove selected", command=self._remove_bad_player).pack(side="left")
        self.bad_players_status = tk.StringVar(value="")
        ttk.Label(controls, textvariable=self.bad_players_status, foreground="#555555").pack(side="left", padx=(12, 0))
        self._refresh_bad_players_view()

    def _render_live_players(self) -> None:
        if not hasattr(self, "players_tree"):
            return
        for item in self.players_tree.get_children():
            self.players_tree.delete(item)
        if self.live_players is None:
            self.players_tab_status.set(self.live_players_error or "Could not retrieve the player list.")
            return

        bad_names = {name.casefold() for name in self.bad_players}
        flagged_count = 0
        for player in self.live_players:
            flagged = player.name.casefold() in bad_names
            flagged_count += int(flagged)
            score = str(player.score)
            values = (
                player.index,
                player.name or "(no name)",
                score,
                format_player_duration(player.duration_seconds),
                "On list" if flagged else "",
            )
            self.players_tree.insert("", "end", values=values, tags=("on_list",) if flagged else ())

        count = len(self.live_players)
        if count == 0:
            summary = "No players connected."
        else:
            summary = f"{count} player{'s' if count != 1 else ''} connected."
        if flagged_count:
            summary += f" {flagged_count} match{'es' if flagged_count != 1 else ''} on your personal list."
        if self.live_players_error:
            summary += " The list may be stale."
        self.players_tab_status.set(summary)

    def _update_players(self, player_list: Optional[list[PlayerInfo]], error: str = "") -> None:
        self.live_players = player_list
        self.live_players_error = error
        self._render_live_players()

    def _refresh_bad_players_view(self) -> None:
        if not hasattr(self, "bad_players_tree"):
            return
        for item in self.bad_players_tree.get_children():
            self.bad_players_tree.delete(item)
        for index, name in enumerate(self.bad_players):
            self.bad_players_tree.insert("", "end", iid=str(index), values=(name,))
        if hasattr(self, "bad_players_status"):
            self.bad_players_status.set(f"{len(self.bad_players)} of {MAX_BAD_PLAYERS} names")
        self._render_live_players()

    def _save_bad_players(self, message: str) -> None:
        self.settings["bad_players"] = self.bad_players
        self._refresh_bad_players_view()
        try:
            save_settings(self.settings)
            self.bad_players_status.set(message)
            self._dev_log("PLAYERS", message)
        except OSError as exc:
            self.bad_players_status.set(f"Change applied, but could not save: {exc}")
            self._dev_log("PLAYERS", f"{message}; saving failed: {exc}", level="ERROR")

    def _add_bad_player(self, _event=None) -> None:
        name = self.bad_player_input.get().strip()[:128]
        if not name:
            self.bad_players_status.set("Enter a player name.")
            return
        if any(saved.casefold() == name.casefold() for saved in self.bad_players):
            self.bad_players_status.set("That name is already on the list.")
            return
        if len(self.bad_players) >= MAX_BAD_PLAYERS:
            self.bad_players_status.set(f"The list has reached its limit of {MAX_BAD_PLAYERS} names.")
            return
        self.bad_players.append(name)
        self.bad_player_input.set("")
        self._save_bad_players("Name added.")

    def _remove_bad_player(self) -> None:
        selected = self.bad_players_tree.selection()
        if not selected:
            self.bad_players_status.set("Select a name to remove.")
            return
        name = self.bad_players_tree.item(selected[0], "values")[0]
        self.bad_players = [saved for saved in self.bad_players if saved.casefold() != name.casefold()]
        self._save_bad_players("Name removed.")

    def _choose_console_color(self, target: str) -> None:
        is_background = target == "background"
        color_var = self.console_background_var if is_background else self.console_foreground_var
        color = colorchooser.askcolor(color=color_var.get(), parent=self.root, title="Choose console color")[1]
        if not color:
            return
        color_var.set(color)
        swatch = self.console_background_swatch if is_background else self.console_foreground_swatch
        swatch.configure(text=color, background=color)

    def _apply_customizations(self) -> None:
        try:
            interval_seconds = int(self.poll_interval_var.get())
            font_size = int(self.console_font_size_var.get())
            server_port = int(self.server_port_var.get())
        except (TypeError, ValueError):
            self.customization_status.set("Enter valid numbers for the interval, font size, and port.")
            return
        endpoint = parse_server_address(self.server_host_var.get(), self.server_port_var.get())
        if endpoint is None:
            self.customization_status.set("Enter a valid IPv4 address or hostname and a port from 1 to 65535.")
            return
        server_host, server_port = endpoint
        self.server_port_var.set(str(server_port))
        if not 1 <= server_port <= 65535:
            self.customization_status.set("The port must be between 1 and 65535.")
            return
        developer_mode = self.developer_mode_var.get()
        minimum_interval = 1 if developer_mode else MIN_POLL_INTERVAL_SECONDS
        maximum_interval = MAX_DEVELOPER_POLL_INTERVAL_SECONDS if developer_mode else MAX_POLL_INTERVAL_SECONDS
        if not minimum_interval <= interval_seconds <= maximum_interval:
            self.customization_status.set(
                f"The interval must be between {minimum_interval} and {maximum_interval:,} seconds."
            )
            return
        if not 8 <= font_size <= 20:
            self.customization_status.set("The font size must be between 8 and 20.")
            return

        developer_console_enabled = developer_mode and self.developer_console_var.get()
        language = "en" if self.language_var.get() == "English" else "pt"
        availability_history_enabled = self.history_enabled_var.get()
        history_record_offline = self.history_record_offline_var.get()
        history_record_online = self.history_record_online_var.get()
        server_name = _valid_server_name(self.server_name_var.get(), server_host)
        values = {
            "language": language,
            "server_name": server_name,
            "server_host": server_host,
            "server_port": server_port,
            "poll_interval_seconds": interval_seconds,
            "console_background": _valid_hex_color(
                self.console_background_var.get(), DEFAULT_CONSOLE_BACKGROUND
            ),
            "console_foreground": _valid_hex_color(
                self.console_foreground_var.get(), DEFAULT_CONSOLE_FOREGROUND
            ),
            "console_font_family": self.console_font_family_var.get()
            if self.console_font_family_var.get() in CONSOLE_FONT_FAMILIES
            else "Consolas",
            "console_font_size": font_size,
            "console_auto_scroll": self.console_auto_scroll_var.get(),
            "console_wrap_lines": self.console_wrap_lines_var.get(),
            "developer_mode": developer_mode,
            "developer_console_enabled": developer_console_enabled,
            "availability_history_enabled": availability_history_enabled,
            "history_record_offline": history_record_offline,
            "history_record_online": history_record_online,
        }
        old_interval = self.poll_interval_seconds
        old_target = (self.server_host, self.server_port)
        developer_mode_changed = values["developer_mode"] != self.developer_mode_enabled
        developer_console_changed = values["developer_console_enabled"] != self.developer_console_enabled
        language_changed = language != self.language
        history_settings_changed = any(
            values[key] != getattr(self, attribute)
            for key, attribute in (
                ("availability_history_enabled", "history_enabled"),
                ("history_record_offline", "history_record_offline"),
                ("history_record_online", "history_record_online"),
            )
        )
        target_changed = (server_host, server_port) != (self.server_host, self.server_port)
        self.customization = values
        self.developer_console_enabled = developer_console_enabled
        self.history_enabled = availability_history_enabled
        self.history_record_offline = history_record_offline
        self.history_record_online = history_record_online
        self.server_name = server_name
        self.server_host = server_host
        self.server_port = server_port
        self.poll_interval_seconds = interval_seconds
        self.console_auto_scroll = values["console_auto_scroll"]
        self.selected_server.update({"name": server_name, "host": server_host, "port": server_port})
        for worker in self.workers.values():
            worker.set_interval_seconds(interval_seconds, developer_mode=developer_mode)
        if target_changed and self.worker is not None:
            self.worker.set_target(server_host, server_port)
            self.server_results.pop(self.selected_server_id, None)
            self._refresh_server_list()
        self.server_title_label.configure(text=server_name)
        self.server_address_label.configure(text=f"Garry's Mod  ·  {server_host}:{server_port}")
        self.check_interval_label.configure(text=self._check_interval_text())
        if target_changed:
            self.last_status = "Waiting for a response from the new server…"
            self.status_label.configure(text=self.last_status)
            self.detail_label.configure(text="The new IP will be checked shortly.")
            self.players_label.configure(text="Players: —")
            self.map_label.configure(text="Map: —")
            self.version_label.configure(text="Version: —")
            self.response_label.configure(text="A2S response time: —")
            self._update_players(None, "Waiting to query the new server.")
        self.console_text.configure(
            background=values["console_background"],
            foreground=values["console_foreground"],
            insertbackground=values["console_foreground"],
            font=(values["console_font_family"], values["console_font_size"]),
            wrap="word" if values["console_wrap_lines"] else "none",
        )
        self.developer_text.configure(
            background=values["console_background"],
            foreground=values["console_foreground"],
            insertbackground=values["console_foreground"],
            font=(values["console_font_family"], values["console_font_size"]),
            wrap="word" if values["console_wrap_lines"] else "none",
        )
        self.settings.update(values)
        self._sync_server_settings()
        self._refresh_server_list()
        settings_saved = True
        try:
            save_settings(self.settings)
            self.customization_status.set("Settings applied and saved.")
            self._dev_log("CONFIG", "Preferences saved on this computer.")
        except OSError as exc:
            settings_saved = False
            self.customization_status.set(f"Applied, but could not save: {exc}")
            self._dev_log("CONFIG", f"Preferences applied, but saving failed: {exc}", level="ERROR")
        if interval_seconds != old_interval:
            self._dev_log("CONFIG", f"Check interval changed to {interval_seconds}s.")
        if target_changed:
            self._dev_log("CONFIG", f"Server changed from {old_target[0]}:{old_target[1]} to {server_host}:{server_port}.")
        if developer_mode_changed:
            self._set_developer_mode(values["developer_mode"])
        if developer_mode_changed or developer_console_changed:
            self._sync_developer_console_tab()
        if history_settings_changed:
            self._refresh_history_view()
        if language_changed and settings_saved:
            self.language = language
            self.customization_status.set("Language saved. Restarting the app…")
            self.root.after(200, self._restart_for_language_change)

    def _restart_for_language_change(self) -> None:
        if getattr(sys, "frozen", False):
            command = [sys.executable, *sys.argv[1:]]
            working_directory = Path(sys.executable).resolve().parent
        else:
            script_path = Path(sys.argv[0]).resolve()
            command = [sys.executable, str(script_path), *sys.argv[1:]]
            working_directory = script_path.parent
        try:
            subprocess.Popen(command, cwd=str(working_directory), close_fds=True)
        except OSError as exc:
            messagebox.showerror("Language", f"The language was saved, but the app could not restart: {exc}", parent=self.root)
            return
        self.close()

    def _build_console_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Garry's Mod console", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(
            parent,
            text=(
                "This tab follows your local Garry's Mod console, including messages the client receives while you play. "
                "Enable Garry's Mod console logging in Steam's launch options, start the game, "
                "then select the generated .log or .txt file."
            ),
            wraplength=690,
            justify="left",
        ).pack(anchor="w", pady=(6, 10))
        ttk.Label(
            parent,
            text="This is a local file: the tab cannot access the server's private console or other players' commands.",
            wraplength=690,
            justify="left",
            foreground="#555555",
        ).pack(anchor="w", pady=(0, 12))

        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(0, 8))
        ttk.Button(controls, text="Select log file", command=self._choose_console_log).pack(side="left")
        ttk.Button(controls, text="Clear view", command=self._clear_console_view).pack(side="left", padx=(8, 0))
        self.console_path_label = ttk.Label(parent, text=self._console_path_text(), wraplength=690, foreground="#555555")
        self.console_path_label.pack(anchor="w", pady=(0, 4))
        self.console_tail_status = ttk.Label(parent, text="Waiting for a log file…", foreground="#555555")
        self.console_tail_status.pack(anchor="w", pady=(0, 8))

        log_frame = ttk.Frame(parent)
        log_frame.pack(fill="both", expand=True)
        self.console_text = tk.Text(
            log_frame,
            wrap="none",
            state="disabled",
            font=(
                self.customization["console_font_family"],
                self.customization["console_font_size"],
            ),
            background=self.customization["console_background"],
            foreground=self.customization["console_foreground"],
            insertbackground=self.customization["console_foreground"],
            relief="sunken",
            borderwidth=1,
        )
        if self.customization["console_wrap_lines"]:
            self.console_text.configure(wrap="word")
        vertical = ttk.Scrollbar(log_frame, orient="vertical", command=self.console_text.yview)
        horizontal = ttk.Scrollbar(log_frame, orient="horizontal", command=self.console_text.xview)
        self.console_text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.console_text.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

    def _build_history_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Availability history", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(
            parent,
            text="See when the monitor confirmed that the server became unavailable or came back online. "
            "The history is saved on this computer and keeps up to 500 changes.",
            wraplength=690,
            justify="left",
        ).pack(anchor="w", pady=(5, 12))

        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(0, 8))
        ttk.Button(controls, text="Clear history", command=self._clear_availability_history).pack(side="left")
        self.history_summary = tk.StringVar(value="")
        ttk.Label(controls, textvariable=self.history_summary, foreground="#555555").pack(side="left", padx=(12, 0))

        history_frame = ttk.Frame(parent)
        history_frame.pack(fill="both", expand=True)
        self.history_tree = ttk.Treeview(history_frame, columns=("timestamp", "change"), show="headings")
        self.history_tree.heading("timestamp", text="Date and time")
        self.history_tree.heading("change", text="Confirmed change")
        self.history_tree.column("timestamp", width=180, anchor="w", stretch=False)
        self.history_tree.column("change", width=430, anchor="w")
        scrollbar = ttk.Scrollbar(history_frame, orient="vertical", command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=scrollbar.set)
        self.history_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        history_frame.rowconfigure(0, weight=1)
        history_frame.columnconfigure(0, weight=1)
        self._refresh_history_view()

    def _refresh_history_view(self) -> None:
        if not hasattr(self, "history_tree"):
            return
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        labels = {
            "offline": "Server became unavailable (3 checks without a response)",
            "online": "Server came back online (2 consecutive responses)",
        }
        for entry in reversed(self.availability_history):
            self.history_tree.insert("", "end", values=(entry["timestamp"], labels[entry["event"]]))
        count = len(self.availability_history)
        self.history_summary.set(f"{count} change{'s' if count != 1 else ''} recorded")
        if not self.history_enabled:
            self.history_summary.set(f"{count} saved entr{'y' if count == 1 else 'ies'}; new recording is disabled")

    def _record_history_event(self, event: str, timestamp: str, server_id: Optional[str] = None) -> None:
        server_id = server_id or self.selected_server_id
        if not self.history_enabled:
            self._dev_log("HISTORY", "Change not recorded because history is disabled.")
            return
        if event == "offline" and not self.history_record_offline:
            self._dev_log("HISTORY", "Unavailability event was skipped by the current filter.")
            return
        if event == "online" and not self.history_record_online:
            self._dev_log("HISTORY", "Online recovery was skipped by the current filter.")
            return
        history = self.server_histories.get(server_id, [])
        history = append_history_event(history, event, timestamp)
        self.server_histories[server_id] = history
        if server_id == self.selected_server_id:
            self.availability_history = history
            self._refresh_history_view()
        self._sync_server_settings()
        try:
            save_settings(self.settings)
        except OSError as exc:
            if server_id == self.selected_server_id:
                self.history_summary.set(f"Change shown, but could not be saved: {exc}")

    def _clear_availability_history(self) -> None:
        if not self.availability_history:
            return
        if not messagebox.askyesno("Clear history", "Delete all saved availability changes?", parent=self.root):
            return
        self.availability_history = []
        self.server_histories[self.selected_server_id] = []
        self._sync_server_settings()
        self._refresh_history_view()
        try:
            save_settings(self.settings)
        except OSError as exc:
            self.history_summary.set(f"History cleared for this session, but could not be saved: {exc}")

    def _console_path_text(self) -> str:
        if self.console_log_path is None:
            return "File: none selected"
        return f"File: {self.console_log_path}"

    def _choose_console_log(self) -> None:
        initial_dir = str(self.console_log_path.parent) if self.console_log_path else None
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Select the Garry's Mod console log",
            initialdir=initial_dir,
            filetypes=[
                ("Log and text files", "console.txt console.log *.log *.txt"),
                ("All files", "*.*"),
            ],
        )
        if not selected:
            return
        self.console_log_path = Path(selected)
        self.console_tailer = ConsoleLogTailer(self.console_log_path)
        self.console_path_label.configure(text=self._console_path_text())
        self._clear_console_view()
        self.settings["console_log_path"] = str(self.console_log_path)
        try:
            save_settings(self.settings)
            self.console_tail_status.configure(text="Log selected. Following new messages…")
            self._dev_log("CONSOLE", f"Console log file selected: {self.console_log_path}.")
        except OSError as exc:
            self.console_tail_status.configure(text=f"Log selected, but could not save the preference: {exc}")
            self._dev_log("CONSOLE", f"File selected, but saving the preference failed: {exc}", level="ERROR")

    def _clear_console_view(self) -> None:
        if not hasattr(self, "console_text"):
            return
        self.console_text.configure(state="normal")
        self.console_text.delete("1.0", "end")
        self.console_text.configure(state="disabled")

    def _append_console_text(self, text: str) -> None:
        if not text:
            return
        self.console_text.configure(state="normal")
        self.console_text.insert("end", text)
        end_line = int(self.console_text.index("end-1c").split(".")[0])
        excess_lines = end_line - LOG_MAX_LINES
        if excess_lines > 0:
            self.console_text.delete("1.0", f"{excess_lines + 1}.0")
        if self.console_auto_scroll:
            self.console_text.see("end")
        self.console_text.configure(state="disabled")

    def _poll_console_log(self) -> None:
        if self.console_tailer is not None:
            try:
                text, reset_display, skipped_old_content = self.console_tailer.read_available()
                if reset_display:
                    self._clear_console_view()
                    self.console_tail_status.configure(text="The log file was recreated or truncated; monitoring restarted.")
                    self._dev_log("CONSOLE", "Log file recreated or truncated; reading restarted.")
                if skipped_old_content:
                    self._append_console_text("[Showing the most recent part of the file. New messages appear below.]\n")
                    self._dev_log("CONSOLE", "Skipped old content to keep the view focused on recent updates.")
                self._append_console_text(text)
                if text:
                    self._dev_log("CONSOLE", f"File update received ({text.count(chr(10)) or 1} line(s)).")
                if self.console_log_path and self.console_log_path.is_file():
                    self.console_tail_status.configure(text="Following the file; updates automatically every second.")
                    self._dev_log_state("console-file", f"Following {self.console_log_path}.")
            except FileNotFoundError:
                self.console_tail_status.configure(
                    text="Log file not found. Enable Garry's Mod console logging and select its .log or .txt file."
                )
                self._dev_log_state("console-file", "Console log file was not found.", level="WARN")
            except OSError as exc:
                self.console_tail_status.configure(text=f"Could not read the log: {exc}")
                self._dev_log_state("console-file", f"Could not read the console log: {exc}", level="ERROR")
        elif hasattr(self, "console_tail_status"):
            self.console_tail_status.configure(
                text="Enable console logging in Garry's Mod and select its .log or .txt file to get started."
            )
            self._dev_log_state("console-file", "No console file has been selected.")
        if self.root.winfo_exists():
            self.root.after(1000, self._poll_console_log)

    def _change_autostart(self) -> None:
        try:
            set_autostart(self.autostart_var.get())
            self.startup_message.set("Automatic startup updated.")
            self._dev_log("STARTUP", f"Automatic startup {'enabled' if self.autostart_var.get() else 'disabled'}.")
        except OSError as exc:
            self.autostart_var.set(not self.autostart_var.get())
            self.startup_message.set(f"Could not update: {exc}")
            self._dev_log("STARTUP", f"Could not update automatic startup: {exc}", level="ERROR")

    def _drain_results(self) -> None:
        while True:
            try:
                result = self.output_queue.get_nowait()
            except queue.Empty:
                break
            self._render_result(result)
        if self.root.winfo_exists():
            self.root.after(200, self._drain_results)

    def _render_result(self, result: dict) -> None:
        server_id = str(result.get("server_id", self.selected_server_id))
        if self._server_record(server_id) is None:
            return
        self.server_results[server_id] = result
        self._update_server_list_row(server_id)
        if result.get("event") in ("online", "offline"):
            server = self._server_record(server_id)
            server_name = server["name"] if server else server_id
            if self.language == "pt":
                message = f"{server_name}: servidor confirmado online." if result["event"] == "online" else f"{server_name}: servidor confirmado indisponível."
                self._dev_log("ESTADO", message, level="INFO" if result["event"] == "online" else "WARN")
            else:
                message = f"{server_name}: server confirmed online." if result["event"] == "online" else f"{server_name}: server confirmed unavailable."
                self._dev_log("STATE", message, level="INFO" if result["event"] == "online" else "WARN")
            self._record_history_event(result["event"], result["checked_at"], server_id)
        if server_id != self.selected_server_id:
            return
        self.checked_label.configure(text=f"Last check: {result['checked_at']}")
        state = result["state"]
        failure_streak = result["failure_streak"]
        success_streak = result["success_streak"]

        if result["ok"]:
            info = result["info"]
            self.players_label.configure(text=f"Players: {info.players}/{info.max_players}")
            self._update_players(result.get("player_list"), result.get("player_error", ""))
            self.map_label.configure(text=f"Map: {info.map_name or '—'}")
            self.version_label.configure(text=f"Version: {info.version or '—'}")
            self.response_label.configure(text=f"A2S response time: {result['response_ms']} ms")
            if state == "offline":
                self.last_status = f"No response · confirming recovery ({success_streak}/{SUCCESSES_TO_RECOVER})"
                self.detail_label.configure(text="The server responded; waiting for one more response to confirm it is back.")
            else:
                self.last_status = "Online"
                self.detail_label.configure(text=f"Valid response from {info.name}.")
        else:
            self._update_players(
                result.get("player_list"),
                result.get("player_error", "Server is not responding; player list is unavailable."),
            )
            self.response_label.configure(text="A2S response time: no response")
            if state == "online":
                self.last_status = f"Online · no response ({failure_streak}/{FAILURES_TO_OFFLINE})"
                self.detail_label.configure(text="Waiting to confirm that the server is unavailable.")
            elif state == "offline":
                self.last_status = "No response after 3 checks"
                self.detail_label.configure(text="The server did not respond to recent checks. This may indicate a network issue or a stopped server.")
            else:
                self.last_status = f"Checking · no response ({failure_streak}/{FAILURES_TO_OFFLINE})"
                self.detail_label.configure(text="UDP attempts were exhausted for this cycle; waiting for the next check.")

        self.status_label.configure(text=self.last_status)
        if result["ok"]:
            info = result["info"]
            self._dev_log(
                "MONITOR",
                f"A2S_INFO responded in {result['response_ms']} ms; state={state}; players={info.players}/{info.max_players}; map={info.map_name or '—'}.",
            )
        else:
            self._dev_log(
                "MONITOR",
                f"No A2S_INFO response; state={state}; consecutive failures={failure_streak}/{FAILURES_TO_OFFLINE}.",
                level="WARN" if state == "offline" else "INFO",
            )
    def close(self) -> None:
        self._dev_log("APP", "Closing application.")
        self.stop_event.set()
        for stop_event in self.worker_stop_events.values():
            stop_event.set()
        for worker in self.workers.values():
            worker.schedule_changed.set()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    MonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
