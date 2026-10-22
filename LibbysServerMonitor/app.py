"""Windows desktop monitor for a Garry's Mod / Source server."""

from __future__ import annotations

import bz2
import codecs
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
QUERY_TIMEOUT_SECONDS = 1.2
QUERY_ATTEMPTS = 3
FAILURES_TO_OFFLINE = 3
SUCCESSES_TO_RECOVER = 2
INFO_REQUEST = b"\xff\xff\xff\xff\x54Source Engine Query\x00"
PLAYER_REQUEST = b"\xff\xff\xff\xff\x55\xff\xff\xff\xff"
LOG_INITIAL_BYTES = 256 * 1024
LOG_READ_BYTES = 64 * 1024
LOG_MAX_LINES = 5000
MAX_HISTORY_ENTRIES = 500
MAX_BAD_PLAYERS = 500
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
    score: float
    duration_seconds: float


def _read_cstring(data: bytes, offset: int) -> tuple[str, int]:
    end = data.find(b"\x00", offset)
    if end < 0:
        raise QueryError("Resposta A2S_INFO incompleta (texto sem terminador).")
    return data[offset:end].decode("utf-8", errors="replace"), end + 1


def parse_info_packet(data: bytes) -> ServerInfo:
    """Parse a complete, reassembled Source engine A2S_INFO response."""
    if len(data) < 6 or data[:4] != b"\xff\xff\xff\xff" or data[4] != 0x49:
        raise QueryError("Resposta UDP não corresponde a A2S_INFO.")

    offset = 5
    protocol = data[offset]
    offset += 1
    name, offset = _read_cstring(data, offset)
    map_name, offset = _read_cstring(data, offset)
    folder, offset = _read_cstring(data, offset)
    game, offset = _read_cstring(data, offset)
    # app id (2), three player fields, four flags and a minimum empty version string.
    if len(data) < offset + 10:
        raise QueryError("Resposta A2S_INFO incompleta (campos básicos).")
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
        raise QueryError("Versão A2S_INFO inválida.")

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
        raise QueryError("Resposta UDP não corresponde a A2S_PLAYER.")
    player_count = data[5]
    offset = 6
    players = []
    for _ in range(player_count):
        if offset >= len(data):
            raise QueryError("Resposta A2S_PLAYER incompleta (índice do jogador).")
        player_index = data[offset]
        offset += 1
        name, offset = _read_cstring(data, offset)
        if len(data) < offset + 8:
            raise QueryError("Resposta A2S_PLAYER incompleta (pontuação ou tempo).")
        score, duration = struct.unpack_from("<ff", data, offset)
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
                raise QueryError("Resposta A2S_INFO dividida incompleta.")
            sock.settimeout(remaining)
            try:
                packet = sock.recv(65535)
            except socket.timeout as exc:
                raise QueryError("Resposta A2S_INFO dividida incompleta.") from exc

        if len(packet) < 10 or packet[:4] != b"\xfe\xff\xff\xff":
            continue
        raw_id = struct.unpack_from("<I", packet, 4)[0]
        this_compressed = bool(raw_id & 0x80000000)
        this_id = raw_id & 0x7FFFFFFF
        count = packet[8]
        index = packet[9]
        if count == 0 or count > 64 or index >= count:
            raise QueryError("Cabeçalho de resposta dividida inválido.")
        if request_id is None:
            request_id, packet_count, compressed = this_id, count, this_compressed
        elif (this_id, count, this_compressed) != (request_id, packet_count, compressed):
            continue
        if index in packets:
            continue

        payload_offset = 10
        if compressed and index == 0:
            if len(packet) < 18:
                raise QueryError("Cabeçalho comprimido A2S_INFO incompleto.")
            expected_size, expected_crc = struct.unpack_from("<II", packet, 10)
            payload_offset = 18
        packets[index] = packet[payload_offset:]

    joined = b"".join(packets[index] for index in range(packet_count or 0))
    if compressed:
        try:
            joined = bz2.decompress(joined)
        except (OSError, EOFError) as exc:
            raise QueryError("Não foi possível descomprimir a resposta A2S_INFO.") from exc
        if len(joined) != expected_size or zlib.crc32(joined) != expected_crc:
            raise QueryError("Tamanho ou checksum da resposta A2S_INFO inválido.")
    return joined


def _receive_info_response(sock: socket.socket, deadline: float) -> ServerInfo:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QueryError("Tempo limite aguardando A2S_INFO.")
        sock.settimeout(remaining)
        try:
            packet = sock.recv(65535)
        except socket.timeout as exc:
            raise QueryError("Tempo limite aguardando A2S_INFO.") from exc

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
    detail = str(last_error) if last_error else "sem resposta"
    raise QueryError(f"Sem resposta válida após {attempts} tentativas: {detail}")


def _receive_player_response(sock: socket.socket, deadline: float) -> list[PlayerInfo]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QueryError("Tempo limite aguardando A2S_PLAYER.")
        sock.settimeout(remaining)
        try:
            packet = sock.recv(65535)
        except socket.timeout as exc:
            raise QueryError("Tempo limite aguardando A2S_PLAYER.") from exc

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
    detail = str(last_error) if last_error else "sem resposta"
    raise QueryError(f"Lista de jogadores indisponível após {attempts} tentativas: {detail}")


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


def customization_from_settings(settings: dict) -> dict:
    font_family = settings.get("console_font_family", "Consolas")
    if font_family not in CONSOLE_FONT_FAMILIES:
        font_family = "Consolas"
    return {
        "poll_interval_seconds": _bounded_int(
            settings.get("poll_interval_seconds", POLL_INTERVAL_SECONDS),
            POLL_INTERVAL_SECONDS,
            MIN_POLL_INTERVAL_SECONDS,
            MAX_POLL_INTERVAL_SECONDS,
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
    ) -> None:
        super().__init__(daemon=True, name="A2S-server-monitor")
        self.output_queue = output_queue
        self.stop_event = stop_event
        self.interval_seconds = _bounded_int(
            interval_seconds,
            POLL_INTERVAL_SECONDS,
            MIN_POLL_INTERVAL_SECONDS,
            MAX_POLL_INTERVAL_SECONDS,
        )
        self.schedule_changed = threading.Event()
        self.tracker = StateTracker()

    def set_interval_seconds(self, seconds: int) -> None:
        self.interval_seconds = _bounded_int(
            seconds,
            POLL_INTERVAL_SECONDS,
            MIN_POLL_INTERVAL_SECONDS,
            MAX_POLL_INTERVAL_SECONDS,
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
                    next_check = time.monotonic() + self.interval_seconds
                    continue
            checked_at = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            try:
                query_started = time.perf_counter()
                info = query_server()
                response_ms = round((time.perf_counter() - query_started) * 1000)
                try:
                    player_list = query_players(timeout=PLAYER_QUERY_TIMEOUT_SECONDS, attempts=1)
                    player_error = ""
                except QueryError as exc:
                    # A2S_PLAYER may be disabled while A2S_INFO still works.
                    # Keep monitoring availability independently from the name list.
                    player_list = None
                    player_error = str(exc)
                stable_state, event = self.tracker.observe(True)
                payload = {
                    "ok": True,
                    "info": info,
                    "player_list": player_list,
                    "player_error": player_error,
                    "response_ms": response_ms,
                    "state": stable_state,
                    "event": event,
                    "failure_streak": 0,
                    "success_streak": self.tracker.success_streak,
                    "checked_at": checked_at,
                    "error": "",
                }
            except QueryError as exc:
                stable_state, event = self.tracker.observe(False)
                payload = {
                    "ok": False,
                    "info": None,
                    "player_list": None,
                    "player_error": "Servidor sem resposta; lista indisponível.",
                    "response_ms": None,
                    "state": stable_state,
                    "event": event,
                    "failure_streak": self.tracker.failure_streak,
                    "success_streak": 0,
                    "checked_at": checked_at,
                    "error": str(exc),
                }
            self.output_queue.put(payload)
            next_check = max(next_check + self.interval_seconds, time.monotonic())


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
        raise OSError("A inicialização automática está disponível somente no Windows.")
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
        self.last_status = "Aguardando primeira resposta…"
        self.settings = load_settings()
        self.customization = customization_from_settings(self.settings)
        self.poll_interval_seconds = self.customization["poll_interval_seconds"]
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
        self.console_auto_scroll = self.customization["console_auto_scroll"]
        saved_log_path = self.settings.get("console_log_path", "")
        self.console_log_path = Path(saved_log_path) if saved_log_path else None
        self.console_tailer = ConsoleLogTailer(self.console_log_path) if self.console_log_path else None
        self.availability_history = history_from_settings(self.settings)
        self.bad_players = bad_players_from_settings(self.settings)
        self.live_players: Optional[list[PlayerInfo]] = None
        self.live_players_error = "Aguardando a primeira consulta."

        self._build_ui()
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        self.autostart_box.configure(variable=self.autostart_var)
        if os.name == "nt" and not autostart_enabled():
            # The requested default is to start with Windows on first launch.
            try:
                set_autostart(True)
                self.autostart_var.set(True)
            except OSError as exc:
                self.startup_message.set(f"Não foi possível ativar início automático: {exc}")
        self.worker = MonitorWorker(
            self.output_queue,
            self.stop_event,
            self.customization["poll_interval_seconds"],
        )
        self.worker.start()
        self.root.after(200, self._drain_results)
        self.root.after(500, self._poll_console_log)

    def _build_ui(self) -> None:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)
        monitor_tab = ttk.Frame(notebook, padding=(24, 22))
        players_tab = ttk.Frame(notebook, padding=(16, 14))
        bad_players_tab = ttk.Frame(notebook, padding=(16, 14))
        console_tab = ttk.Frame(notebook, padding=(16, 14))
        history_tab = ttk.Frame(notebook, padding=(16, 14))
        customization_tab = ttk.Frame(notebook, padding=(20, 18))
        notebook.add(monitor_tab, text="Monitor")
        notebook.add(players_tab, text="Jogadores")
        notebook.add(bad_players_tab, text="Jogadores Ruims")
        notebook.add(console_tab, text="Console")
        notebook.add(history_tab, text="Histórico")
        notebook.add(customization_tab, text="Customização")

        outer = monitor_tab
        ttk.Label(outer, text=SERVER_NAME, font=("Segoe UI", 17, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text=f"Garry's Mod  ·  {SERVER_HOST}:{SERVER_PORT}",
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(3, 18))

        status_frame = ttk.LabelFrame(outer, text="Estado do servidor", padding=(16, 12))
        status_frame.pack(fill="x")
        self.status_label = ttk.Label(status_frame, text=self.last_status, font=("Segoe UI", 14, "bold"))
        self.status_label.pack(anchor="w")
        self.detail_label = ttk.Label(status_frame, text="A primeira verificação está em andamento.")
        self.detail_label.pack(anchor="w", pady=(5, 0))

        info_frame = ttk.LabelFrame(outer, text="Informações", padding=(16, 12))
        info_frame.pack(fill="x", pady=(14, 0))
        self.players_label = ttk.Label(info_frame, text="Jogadores: —")
        self.players_label.pack(anchor="w")
        self.map_label = ttk.Label(info_frame, text="Mapa: —")
        self.map_label.pack(anchor="w", pady=(4, 0))
        self.version_label = ttk.Label(info_frame, text="Versão: —")
        self.version_label.pack(anchor="w", pady=(4, 0))
        self.response_label = ttk.Label(info_frame, text="Tempo da resposta A2S: —")
        self.response_label.pack(anchor="w", pady=(4, 0))

        prefs = ttk.Frame(outer)
        prefs.pack(fill="x", pady=(16, 0))
        self.autostart_box = ttk.Checkbutton(
            prefs,
            text="Iniciar automaticamente com o Windows",
            command=self._change_autostart,
        )
        self.autostart_box.pack(anchor="w")
        self.startup_message = tk.StringVar(value="")
        ttk.Label(prefs, textvariable=self.startup_message, foreground="#8a4b00", wraplength=500).pack(
            anchor="w", pady=(3, 4)
        )
        ttk.Separator(outer).pack(fill="x", pady=(14, 8))
        self.checked_label = ttk.Label(outer, text="Última verificação: —")
        self.checked_label.pack(anchor="w")
        self.check_interval_label = ttk.Label(
            outer,
            text=self._check_interval_text(),
            wraplength=520,
            foreground="#555555",
        )
        self.check_interval_label.pack(anchor="w", pady=(9, 0))

        self._build_players_tab(players_tab)
        self._build_bad_players_tab(bad_players_tab)
        self._build_console_tab(console_tab)
        self._build_history_tab(history_tab)
        self._build_customization_tab(customization_tab)

    def _check_interval_text(self) -> str:
        return (
            f"Verificação a cada {self.poll_interval_seconds} segundos. São necessárias 3 verificações "
            "sem resposta para marcar offline; isso não confirma um crash."
        )

    def _build_customization_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Personalize o aplicativo", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(
            parent,
            text="As alterações são salvas neste computador e aplicadas quando você clica em Aplicar.",
            wraplength=690,
            foreground="#555555",
        ).pack(anchor="w", pady=(4, 14))

        interval_frame = ttk.LabelFrame(parent, text="Monitoramento", padding=(14, 10))
        interval_frame.pack(fill="x", pady=(0, 12))
        interval_row = ttk.Frame(interval_frame)
        interval_row.pack(fill="x")
        ttk.Label(interval_row, text="Atualizar/verificar a cada").pack(side="left")
        ttk.Spinbox(
            interval_row,
            from_=MIN_POLL_INTERVAL_SECONDS,
            to=MAX_POLL_INTERVAL_SECONDS,
            increment=1,
            width=7,
            textvariable=self.poll_interval_var,
        ).pack(side="left", padx=(8, 6))
        ttk.Label(interval_row, text="segundos (de 5 a 300)").pack(side="left")

        appearance = ttk.LabelFrame(parent, text="Console", padding=(14, 10))
        appearance.pack(fill="x", pady=(0, 12))
        font_row = ttk.Frame(appearance)
        font_row.pack(fill="x", pady=(0, 10))
        ttk.Label(font_row, text="Fonte").pack(side="left")
        ttk.Combobox(
            font_row,
            textvariable=self.console_font_family_var,
            values=CONSOLE_FONT_FAMILIES,
            state="readonly",
            width=20,
        ).pack(side="left", padx=(8, 18))
        ttk.Label(font_row, text="Tamanho").pack(side="left")
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
        ttk.Label(colors_row, text="Fundo").pack(side="left")
        ttk.Button(
            colors_row,
            text="Escolher cor",
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
        ttk.Label(colors_row, text="Texto").pack(side="left")
        ttk.Button(
            colors_row,
            text="Escolher cor",
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
            text="Quebrar linhas compridas",
            variable=self.console_wrap_lines_var,
        ).pack(anchor="w", pady=(2, 2))
        ttk.Checkbutton(
            appearance,
            text="Rolar automaticamente para as mensagens novas",
            variable=self.console_auto_scroll_var,
        ).pack(anchor="w")

        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=(2, 0))
        ttk.Button(actions, text="Aplicar e salvar", command=self._apply_customizations).pack(side="left")
        self.customization_status = tk.StringVar(value="")
        ttk.Label(actions, textvariable=self.customization_status, foreground="#555555", wraplength=500).pack(
            side="left", padx=(12, 0)
        )

    def _build_players_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Jogadores conectados", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(
            parent,
            text="A lista é atualizada junto com o monitor. Alguns servidores ocultam os nomes ou não respondem à consulta de jogadores.",
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
            ("index", "ID"),
            ("name", "Jogador"),
            ("score", "Pontuação"),
            ("duration", "Tempo"),
            ("flagged", "Lista pessoal"),
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
        ttk.Label(parent, text="Jogadores Ruims", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(
            parent,
            text="Sua lista pessoal de nomes. Adicione e remova nomes manualmente; quando alguém da lista estiver online, a aba Jogadores vai destacar a linha.",
            wraplength=690,
            justify="left",
        ).pack(anchor="w", pady=(5, 12))

        input_row = ttk.Frame(parent)
        input_row.pack(fill="x", pady=(0, 10))
        self.bad_player_input = tk.StringVar(value="")
        entry = ttk.Entry(input_row, textvariable=self.bad_player_input)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", self._add_bad_player)
        ttk.Button(input_row, text="Adicionar", command=self._add_bad_player).pack(side="left", padx=(8, 0))

        list_frame = ttk.Frame(parent)
        list_frame.pack(fill="both", expand=True)
        self.bad_players_tree = ttk.Treeview(list_frame, columns=("name",), show="headings", height=16)
        self.bad_players_tree.heading("name", text="Nome salvo na lista")
        self.bad_players_tree.column("name", anchor="w")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.bad_players_tree.yview)
        self.bad_players_tree.configure(yscrollcommand=scrollbar.set)
        self.bad_players_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(10, 0))
        ttk.Button(controls, text="Remover selecionado", command=self._remove_bad_player).pack(side="left")
        self.bad_players_status = tk.StringVar(value="")
        ttk.Label(controls, textvariable=self.bad_players_status, foreground="#555555").pack(side="left", padx=(12, 0))
        self._refresh_bad_players_view()

    def _render_live_players(self) -> None:
        if not hasattr(self, "players_tree"):
            return
        for item in self.players_tree.get_children():
            self.players_tree.delete(item)
        if self.live_players is None:
            self.players_tab_status.set(self.live_players_error or "Não foi possível obter a lista de jogadores.")
            return

        bad_names = {name.casefold() for name in self.bad_players}
        flagged_count = 0
        for player in self.live_players:
            flagged = player.name.casefold() in bad_names
            flagged_count += int(flagged)
            score = f"{player.score:g}" if math.isfinite(player.score) else "—"
            values = (
                player.index,
                player.name or "(sem nome)",
                score,
                format_player_duration(player.duration_seconds),
                "Na lista" if flagged else "",
            )
            self.players_tree.insert("", "end", values=values, tags=("on_list",) if flagged else ())

        count = len(self.live_players)
        if count == 0:
            summary = "Nenhum jogador conectado."
        else:
            summary = f"{count} jogador{'es' if count != 1 else ''} conectado{'s' if count != 1 else ''}."
        if flagged_count:
            summary += f" {flagged_count} aparece{'m' if flagged_count != 1 else ''} na sua lista pessoal."
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
            self.bad_players_status.set(f"{len(self.bad_players)} de {MAX_BAD_PLAYERS} nomes")
        self._render_live_players()

    def _save_bad_players(self, message: str) -> None:
        self.settings["bad_players"] = self.bad_players
        self._refresh_bad_players_view()
        try:
            save_settings(self.settings)
            self.bad_players_status.set(message)
        except OSError as exc:
            self.bad_players_status.set(f"Alteração aplicada, mas não foi possível salvar: {exc}")

    def _add_bad_player(self, _event=None) -> None:
        name = self.bad_player_input.get().strip()[:128]
        if not name:
            self.bad_players_status.set("Digite o nome de um jogador.")
            return
        if any(saved.casefold() == name.casefold() for saved in self.bad_players):
            self.bad_players_status.set("Esse nome já está na lista.")
            return
        if len(self.bad_players) >= MAX_BAD_PLAYERS:
            self.bad_players_status.set(f"A lista já atingiu o limite de {MAX_BAD_PLAYERS} nomes.")
            return
        self.bad_players.append(name)
        self.bad_player_input.set("")
        self._save_bad_players("Nome adicionado.")

    def _remove_bad_player(self) -> None:
        selected = self.bad_players_tree.selection()
        if not selected:
            self.bad_players_status.set("Selecione um nome para remover.")
            return
        name = self.bad_players_tree.item(selected[0], "values")[0]
        self.bad_players = [saved for saved in self.bad_players if saved.casefold() != name.casefold()]
        self._save_bad_players("Nome removido.")

    def _choose_console_color(self, target: str) -> None:
        is_background = target == "background"
        color_var = self.console_background_var if is_background else self.console_foreground_var
        color = colorchooser.askcolor(color=color_var.get(), parent=self.root, title="Escolher cor do console")[1]
        if not color:
            return
        color_var.set(color)
        swatch = self.console_background_swatch if is_background else self.console_foreground_swatch
        swatch.configure(text=color, background=color)

    def _apply_customizations(self) -> None:
        try:
            interval_seconds = int(self.poll_interval_var.get())
            font_size = int(self.console_font_size_var.get())
        except (TypeError, ValueError):
            self.customization_status.set("Use números válidos para o intervalo e o tamanho da fonte.")
            return
        if not MIN_POLL_INTERVAL_SECONDS <= interval_seconds <= MAX_POLL_INTERVAL_SECONDS:
            self.customization_status.set("O intervalo deve ficar entre 5 e 300 segundos.")
            return
        if not 8 <= font_size <= 20:
            self.customization_status.set("O tamanho da fonte deve ficar entre 8 e 20.")
            return

        values = {
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
        }
        self.customization = values
        self.poll_interval_seconds = interval_seconds
        self.console_auto_scroll = values["console_auto_scroll"]
        if self.worker is not None:
            self.worker.set_interval_seconds(interval_seconds)
        self.check_interval_label.configure(text=self._check_interval_text())
        self.console_text.configure(
            background=values["console_background"],
            foreground=values["console_foreground"],
            insertbackground=values["console_foreground"],
            font=(values["console_font_family"], values["console_font_size"]),
            wrap="word" if values["console_wrap_lines"] else "none",
        )
        self.settings.update(values)
        try:
            save_settings(self.settings)
            self.customization_status.set("Preferências aplicadas e salvas.")
        except OSError as exc:
            self.customization_status.set(f"Aplicadas, mas não foi possível salvar: {exc}")

    def _build_console_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Console do Garry's Mod", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(
            parent,
            text=(
                "Esta aba acompanha o console local do seu Garry's Mod, incluindo as mensagens que o cliente recebe "
                "enquanto você joga. Para o jogo salvar esse registro, adicione -condebug às opções de inicialização "
                "do Garry's Mod na Steam e abra o jogo. Depois, selecione o arquivo console.txt."
            ),
            wraplength=690,
            justify="left",
        ).pack(anchor="w", pady=(6, 10))
        ttk.Label(
            parent,
            text="O arquivo é local: esta aba não acessa o console privado do servidor nem comandos de outros jogadores.",
            wraplength=690,
            justify="left",
            foreground="#555555",
        ).pack(anchor="w", pady=(0, 12))

        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(0, 8))
        ttk.Button(controls, text="Selecionar arquivo de log", command=self._choose_console_log).pack(side="left")
        ttk.Button(controls, text="Limpar visualização", command=self._clear_console_view).pack(side="left", padx=(8, 0))
        self.console_path_label = ttk.Label(parent, text=self._console_path_text(), wraplength=690, foreground="#555555")
        self.console_path_label.pack(anchor="w", pady=(0, 4))
        self.console_tail_status = ttk.Label(parent, text="Aguardando arquivo de log…", foreground="#555555")
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
        ttk.Label(parent, text="Histórico de disponibilidade", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(
            parent,
            text="Veja quando o monitor confirmou que o servidor ficou indisponível ou voltou a ficar online. "
            "O histórico é salvo neste computador e mantém até 500 mudanças.",
            wraplength=690,
            justify="left",
        ).pack(anchor="w", pady=(5, 12))

        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(0, 8))
        ttk.Button(controls, text="Limpar histórico", command=self._clear_availability_history).pack(side="left")
        self.history_summary = tk.StringVar(value="")
        ttk.Label(controls, textvariable=self.history_summary, foreground="#555555").pack(side="left", padx=(12, 0))

        history_frame = ttk.Frame(parent)
        history_frame.pack(fill="both", expand=True)
        self.history_tree = ttk.Treeview(history_frame, columns=("timestamp", "change"), show="headings")
        self.history_tree.heading("timestamp", text="Data e hora")
        self.history_tree.heading("change", text="Mudança confirmada")
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
            "offline": "Servidor ficou indisponível (3 verificações sem resposta)",
            "online": "Servidor voltou a ficar online (2 respostas seguidas)",
        }
        for entry in reversed(self.availability_history):
            self.history_tree.insert("", "end", values=(entry["timestamp"], labels[entry["event"]]))
        count = len(self.availability_history)
        self.history_summary.set(f"{count} mudança{'s' if count != 1 else ''} registrada{'s' if count != 1 else ''}")

    def _record_history_event(self, event: str, timestamp: str) -> None:
        self.availability_history = append_history_event(self.availability_history, event, timestamp)
        self.settings["availability_history"] = self.availability_history
        self._refresh_history_view()
        try:
            save_settings(self.settings)
        except OSError as exc:
            self.history_summary.set(f"Mudança exibida, mas não foi possível salvar: {exc}")

    def _clear_availability_history(self) -> None:
        if not self.availability_history:
            return
        if not messagebox.askyesno("Limpar histórico", "Apagar todas as mudanças de disponibilidade salvas?", parent=self.root):
            return
        self.availability_history = []
        self.settings["availability_history"] = []
        self._refresh_history_view()
        try:
            save_settings(self.settings)
        except OSError as exc:
            self.history_summary.set(f"Histórico limpo nesta sessão, mas não foi possível salvar: {exc}")

    def _console_path_text(self) -> str:
        if self.console_log_path is None:
            return "Arquivo: nenhum selecionado"
        return f"Arquivo: {self.console_log_path}"

    def _choose_console_log(self) -> None:
        initial_dir = str(self.console_log_path.parent) if self.console_log_path else None
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Selecione o log do console do Garry's Mod",
            initialdir=initial_dir,
            filetypes=[
                ("Logs e textos", "console.txt console.log *.log *.txt"),
                ("Todos os arquivos", "*.*"),
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
            self.console_tail_status.configure(text="Log selecionado. Acompanhando novas mensagens…")
        except OSError as exc:
            self.console_tail_status.configure(text=f"Log selecionado, mas não foi possível salvar a preferência: {exc}")

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
                    self.console_tail_status.configure(text="O arquivo de log foi recriado ou truncado; acompanhamento reiniciado.")
                if skipped_old_content:
                    self._append_console_text("[Exibindo o trecho mais recente do arquivo. Novas mensagens aparecem abaixo.]\n")
                self._append_console_text(text)
                if self.console_log_path and self.console_log_path.is_file():
                    self.console_tail_status.configure(text="Acompanhando o arquivo; atualização automática a cada segundo.")
            except FileNotFoundError:
                self.console_tail_status.configure(
                    text="Arquivo não encontrado. Inicie o Garry's Mod com -condebug e selecione console.txt."
                )
            except OSError as exc:
                self.console_tail_status.configure(text=f"Não foi possível ler o log: {exc}")
        elif hasattr(self, "console_tail_status"):
            self.console_tail_status.configure(
                text="Ative -condebug no Garry's Mod e selecione o arquivo console.txt para começar."
            )
        if self.root.winfo_exists():
            self.root.after(1000, self._poll_console_log)

    def _change_autostart(self) -> None:
        try:
            set_autostart(self.autostart_var.get())
            self.startup_message.set("Início automático atualizado.")
        except OSError as exc:
            self.autostart_var.set(not self.autostart_var.get())
            self.startup_message.set(f"Não foi possível atualizar: {exc}")

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
        self.checked_label.configure(text=f"Última verificação: {result['checked_at']}")
        state = result["state"]
        failure_streak = result["failure_streak"]
        success_streak = result["success_streak"]

        if result["ok"]:
            info = result["info"]
            self.players_label.configure(text=f"Jogadores: {info.players}/{info.max_players}")
            self._update_players(result.get("player_list"), result.get("player_error", ""))
            self.map_label.configure(text=f"Mapa: {info.map_name or '—'}")
            self.version_label.configure(text=f"Versão: {info.version or '—'}")
            self.response_label.configure(text=f"Tempo da resposta A2S: {result['response_ms']} ms")
            if state == "offline":
                self.last_status = f"Sem resposta · validando retorno ({success_streak}/{SUCCESSES_TO_RECOVER})"
                self.detail_label.configure(text="O servidor respondeu; aguardando mais uma resposta para confirmar o retorno.")
            else:
                self.last_status = "Online"
                self.detail_label.configure(text=f"Resposta válida de {info.name}.")
        else:
            self._update_players(None, result.get("player_error", "Servidor sem resposta; lista indisponível."))
            self.response_label.configure(text="Tempo da resposta A2S: sem resposta")
            if state == "online":
                self.last_status = f"Online · sem resposta ({failure_streak}/{FAILURES_TO_OFFLINE})"
                self.detail_label.configure(text="Aguardando para confirmar a indisponibilidade.")
            elif state == "offline":
                self.last_status = "Sem resposta após 3 verificações"
                self.detail_label.configure(text="O servidor não respondeu às verificações recentes. Isso pode indicar falha de rede ou servidor parado.")
            else:
                self.last_status = f"Verificando · sem resposta ({failure_streak}/{FAILURES_TO_OFFLINE})"
                self.detail_label.configure(text="Tentativas UDP esgotadas neste ciclo; aguardando a próxima verificação.")

        self.status_label.configure(text=self.last_status)
        if result["event"] in ("online", "offline"):
            self._record_history_event(result["event"], result["checked_at"])

    def close(self) -> None:
        self.stop_event.set()
        if self.worker is not None:
            self.worker.schedule_changed.set()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    MonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
