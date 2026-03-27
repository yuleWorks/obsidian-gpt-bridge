import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from websockets.asyncio.server import ServerConnection, serve


HOST = "127.0.0.1"
PORT = 8765
DEFAULT_NOTE_DIRECTORY = "Inbox/ChatGPT"
DEFAULT_NOTE_PREFIX = "ChatGPT Capture"
HEARTBEAT_INTERVAL_SECONDS = 20
UNDEFINED_FILENAME = "_undefined.md"


@dataclass
class BridgeState:
    vault_path: Path
    note_directory: str = DEFAULT_NOTE_DIRECTORY
    theme_folder: str | None = None
    clients: set[ServerConnection] = field(default_factory=set)
    pending_capture: asyncio.Future | None = None


def timestamped_title(prefix: str = DEFAULT_NOTE_PREFIX) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    return f"{prefix} {stamp}"


def extract_heading_title(text: str) -> str | None:
    for line in text.splitlines():
        match = re.match(r"^\s*#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return None


def strip_done_sentinel(text: str) -> str:
    lines = text.splitlines()

    while lines and not lines[-1].strip():
        lines.pop()

    if lines and lines[-1].strip() == "!!!DONE!!!":
        lines.pop()

    return "\n".join(lines).rstrip()


def sanitize_folder_name(folder_name: str) -> str:
    sanitized = folder_name.strip().strip("/").strip("\\")
    sanitized = re.sub(r"[<>:\"|?*]", "-", sanitized)
    sanitized = sanitized.replace("\\", "/")
    sanitized = "/".join(part.strip() for part in sanitized.split("/") if part.strip())
    return sanitized


def sanitize_file_stem(name: str) -> str:
    sanitized = re.sub(r"[<>:\"/\\|?*]", "-", name.strip())
    sanitized = re.sub(r"\s+", " ", sanitized).strip().rstrip(".")
    return sanitized or timestamped_title()


def extract_folder_name(text: str) -> tuple[str, str | None]:
    lines = text.splitlines()
    separator_indexes = [index for index, line in enumerate(lines) if line.strip() == "---"]

    if len(separator_indexes) < 2:
        return text, None

    folder_line_index = separator_indexes[1] + 1
    if folder_line_index >= len(lines):
        return text, None

    folder_line = lines[folder_line_index].strip()
    if not folder_line.startswith("/") or len(folder_line) == 1:
        return text, None

    folder_name = sanitize_folder_name(folder_line[1:])
    if not folder_name:
        return text, None

    updated_lines = lines[:folder_line_index] + lines[folder_line_index + 1 :]
    return "\n".join(updated_lines).rstrip(), folder_name


def normalize_term(term: str) -> str:
    cleaned = term.strip()
    if cleaned.startswith("[[") and cleaned.endswith("]]"):
        cleaned = cleaned[2:-2].strip()
    cleaned = cleaned.strip('"\'')
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.casefold()


def clean_related_term(term: str) -> str:
    cleaned = term.strip()
    if cleaned.startswith("[[") and cleaned.endswith("]]"):
        cleaned = cleaned[2:-2].strip()
    cleaned = cleaned.strip('"\'')
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def split_related_terms(value: str) -> list[str]:
    parts = re.split(r"\s*[;,]\s*", value.strip())
    terms = []

    for part in parts:
        term = clean_related_term(part)
        if term:
            terms.append(term)

    return terms


def dedupe_terms(terms: list[str]) -> list[str]:
    seen: set[str] = set()
    unique_terms: list[str] = []

    for term in terms:
        normalized = normalize_term(term)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_terms.append(term)

    return unique_terms


def extract_related_terms(text: str) -> list[str]:
    lines = text.splitlines()

    for index, line in enumerate(lines):
        stripped = line.strip()

        inline_match = re.match(r"^(?:[-*+]\s*)?related terms\s*:\s*(.+)$", stripped, re.IGNORECASE)
        if inline_match:
            return dedupe_terms(split_related_terms(inline_match.group(1)))

        heading_match = re.match(r"^#{1,6}\s+related(?:\s+terms)?\s*$", stripped, re.IGNORECASE)
        if not heading_match:
            continue

        related_terms: list[str] = []
        for following_line in lines[index + 1 :]:
            following = following_line.strip()

            if not following:
                if related_terms:
                    break
                continue

            if following == "---" or re.match(r"^#{1,6}\s+", following):
                break

            bullet_match = re.match(r"^[-*+]\s+(.+)$", following)
            if bullet_match:
                related_terms.extend(split_related_terms(bullet_match.group(1)))
            else:
                related_terms.extend(split_related_terms(following))

        return dedupe_terms(related_terms)

    return []


def build_note_payload(text: str) -> tuple[str, str, str | None]:
    cleaned_text = strip_done_sentinel(text)
    cleaned_text, folder_name = extract_folder_name(cleaned_text)
    generated_title = timestamped_title()
    heading_title = extract_heading_title(cleaned_text)
    note_title = heading_title or generated_title
    note_body = f"{cleaned_text.rstrip()}\n\n{generated_title}"
    return note_title, note_body, folder_name


async def broadcast_json(state: BridgeState, payload: dict) -> None:
    if not state.clients:
        print("No extension client is connected.")
        return

    message = json.dumps(payload)
    stale_clients = []

    for client in state.clients:
        try:
            await client.send(message)
        except Exception:
            stale_clients.append(client)

    for client in stale_clients:
        state.clients.discard(client)


def resolve_theme_directory(state: BridgeState) -> str:
    if state.theme_folder:
        return f"{state.note_directory}/{state.theme_folder}"
    return state.note_directory


def resolve_note_directory(state: BridgeState, folder_name: str | None) -> str:
    theme_directory = resolve_theme_directory(state)
    if folder_name:
        return f"{theme_directory}/{folder_name}"
    return theme_directory


def resolve_theme_directory_path(state: BridgeState) -> Path:
    folder_parts = [part for part in state.note_directory.split("/") if part]
    if state.theme_folder:
        folder_parts.extend(part for part in state.theme_folder.split("/") if part)
    return state.vault_path.joinpath(*folder_parts)


def resolve_note_directory_path(state: BridgeState, folder_name: str | None) -> Path:
    theme_path = resolve_theme_directory_path(state)
    if not folder_name:
        return theme_path
    return theme_path.joinpath(*[part for part in folder_name.split("/") if part])


def build_note_path(state: BridgeState, note_title: str, folder_name: str | None) -> Path:
    note_directory_path = resolve_note_directory_path(state, folder_name)
    note_directory_path.mkdir(parents=True, exist_ok=True)
    return note_directory_path / f"{sanitize_file_stem(note_title)}.md"


def parse_note_file(path: Path) -> tuple[str, list[str]] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    title = extract_heading_title(text) or path.stem
    related_terms = extract_related_terms(text)
    return title, related_terms


def iter_theme_note_files(theme_path: Path):
    for note_path in sorted(theme_path.rglob("*.md")):
        if note_path.name == UNDEFINED_FILENAME:
            continue
        yield note_path


def write_undefined_terms_file(state: BridgeState, current_title: str, current_body: str) -> None:
    theme_path = resolve_theme_directory_path(state)
    theme_path.mkdir(parents=True, exist_ok=True)

    notes: list[tuple[str, list[str]]] = []
    current_title_key = normalize_term(current_title)

    for note_path in iter_theme_note_files(theme_path):
        parsed = parse_note_file(note_path)
        if not parsed:
            continue

        note_title, related_terms = parsed
        if normalize_term(note_title) == current_title_key:
            continue
        notes.append((note_title, related_terms))

    notes.append((current_title, extract_related_terms(current_body)))

    defined_terms = {normalize_term(note_title) for note_title, _ in notes}
    undefined_entries: list[tuple[str, str]] = []

    for note_title, related_terms in notes:
        source_key = normalize_term(note_title)
        for term in related_terms:
            normalized_term = normalize_term(term)
            if not normalized_term or normalized_term == source_key:
                continue
            if normalized_term in defined_terms:
                continue
            undefined_entries.append((term, note_title))

    undefined_entries.sort(key=lambda item: (normalize_term(item[0]), normalize_term(item[1])))
    undefined_lines = [f'- "{term}" within [[{source_title}]]' for term, source_title in undefined_entries]
    undefined_file = theme_path / UNDEFINED_FILENAME
    undefined_file.write_text("\n".join(undefined_lines) + ("\n" if undefined_lines else ""), encoding="utf-8")


def create_note_from_text(state: BridgeState, text: str) -> tuple[str, Path]:
    note_title, note_body, folder_name = build_note_payload(text)
    note_path = build_note_path(state, note_title, folder_name)
    note_path.write_text(note_body + "\n", encoding="utf-8")
    write_undefined_terms_file(state, note_title, note_body)
    return note_title, note_path


async def handle_extension_message(state: BridgeState, raw_message: str) -> None:
    try:
        payload = json.loads(raw_message)
    except json.JSONDecodeError:
        print("Received invalid JSON from extension.")
        return

    message_type = payload.get("type")

    if message_type == "extension_hello":
        print("Extension connected and ready.")
        return

    if message_type == "pong":
        return

    if message_type == "capture_error":
        error = payload.get("error", "Unknown capture error.")
        print(f"Capture failed: {error}")

        if state.pending_capture and not state.pending_capture.done():
            state.pending_capture.set_exception(RuntimeError(error))
        state.pending_capture = None
        return

    if message_type == "assistant_message":
        text = payload.get("text", "").strip()
        metadata = payload.get("metadata", {})
        print("\nReceived assistant message from extension.")
        if metadata:
            print(f"Page: {metadata.get('pageTitle', '(unknown title)')}")

        if state.pending_capture and not state.pending_capture.done():
            state.pending_capture.set_result(text)
            state.pending_capture = None
            return

        if metadata.get("autoCaptured"):
            note_title, note_path = create_note_from_text(state, text)
            print(f"Wrote note: {note_title}")
            print(f"Path: {note_path}")
            return

        print("No pending capture request was waiting for this message.")
        state.pending_capture = None
        return

    print(f"Ignoring unknown extension message type: {message_type}")


async def websocket_handler(websocket: ServerConnection, state: BridgeState) -> None:
    state.clients.add(websocket)
    print(f"Extension client connected. Active clients: {len(state.clients)}")

    try:
        async for raw_message in websocket:
            await handle_extension_message(state, raw_message)
    finally:
        state.clients.discard(websocket)
        print(f"Extension client disconnected. Active clients: {len(state.clients)}")


async def heartbeat_loop(state: BridgeState) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        if state.clients:
            await broadcast_json(state, {"type": "ping"})


async def capture_latest_message(state: BridgeState) -> None:
    if state.pending_capture and not state.pending_capture.done():
        print("A capture request is already in progress.")
        return

    if not state.clients:
        print("No extension client is connected.")
        return

    loop = asyncio.get_running_loop()
    state.pending_capture = loop.create_future()

    await broadcast_json(state, {"type": "capture_latest_assistant_message"})

    try:
        text = await asyncio.wait_for(state.pending_capture, timeout=30)
    except asyncio.TimeoutError:
        state.pending_capture = None
        print("Timed out waiting for the extension response.")
        return
    except RuntimeError as error:
        print(f"Capture aborted: {error}")
        return

    note_title, note_path = create_note_from_text(state, text)
    print(f"Wrote note: {note_title}")
    print(f"Path: {note_path}")


async def print_help() -> None:
    print("\nCommands:")
    print("  capture        Request the latest assistant message and write an Obsidian note")
    print("  setdir <path>  Change the destination root inside the vault")
    print("  help           Show this command list")
    print("  exit           Stop the bridge")


async def command_loop(state: BridgeState) -> None:
    await print_help()

    while True:
        raw_command = await asyncio.to_thread(input, "bridge> ")
        command = raw_command.strip()

        if not command:
            continue

        if command == "exit":
            print("Shutting down bridge.")
            return

        if command == "help":
            await print_help()
            continue

        if command == "capture":
            await capture_latest_message(state)
            continue

        if command == "ping":
            await broadcast_json(state, {"type": "ping"})
            continue

        if command.startswith("setdir "):
            new_directory = command.removeprefix("setdir ").strip().strip("/").strip("\\")
            if not new_directory:
                print("Please provide a folder path.")
                continue
            state.note_directory = new_directory
            print(f"Note directory updated to: {state.note_directory}")
            continue

        print(f"Unknown command: {command}")
        await print_help()


async def main() -> None:
    vault_path_arg = sys.argv[1].strip() if len(sys.argv) > 1 else None
    theme_folder_arg = sys.argv[2].strip() if len(sys.argv) > 2 else None

    if not vault_path_arg:
        vault_path_arg = await asyncio.to_thread(input, "Obsidian vault path: ")
        vault_path_arg = vault_path_arg.strip()

    if not vault_path_arg:
        raise SystemExit("An Obsidian vault path is required.")

    if theme_folder_arg is None:
        theme_folder_arg = await asyncio.to_thread(
            input,
            "Theme folder under Inbox/ChatGPT (leave blank to use Inbox/ChatGPT directly): ",
        )
        theme_folder_arg = theme_folder_arg.strip()

    vault_path = Path(vault_path_arg).expanduser()
    theme_folder = sanitize_folder_name(theme_folder_arg) if theme_folder_arg else None

    state = BridgeState(vault_path=vault_path, theme_folder=theme_folder)

    print(f"Starting websocket bridge on ws://{HOST}:{PORT}")
    print(f"Vault path: {state.vault_path}")
    print(f"Default note directory: {state.note_directory}")
    if state.theme_folder:
        print(f"Active theme folder: {state.theme_folder}")
    else:
        print("Active theme folder: (none)")
    print(f"Heartbeat interval: {HEARTBEAT_INTERVAL_SECONDS} seconds")
    print(f"Undefined-term sync enabled for: {state.vault_path}")

    async with serve(lambda websocket: websocket_handler(websocket, state), HOST, PORT):
        heartbeat_task = asyncio.create_task(heartbeat_loop(state))
        try:
            await command_loop(state)
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBridge stopped.")
