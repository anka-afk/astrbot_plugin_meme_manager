"""Build numbered contact sheets for the reply model."""

import hashlib
import io
import json
import math
import sqlite3
from collections import defaultdict, deque
from contextlib import closing
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from ...astrbot_compat import ImageURLPart, Message, TextPart, ToolCall
from ..semantic.storage import file_sha256

PREVIEW_TOOL = "preview_memes"
PREVIEW_TARGET_PREFIX = "preview:"
MAX_PREVIEW_IMAGES = 16
MAX_PREVIEW_PAGES = 2


def build_meme_preview(
    database: Path,
    files: list[Path],
    pack_dirs: dict[str, str | Path],
    after: int,
    limit: int,
) -> tuple[bytes | None, dict[int, tuple[Path, str]], int | None]:
    """Render the next page and allocate permanent numbers for new images.

    Args:
        database: Persistent number registry shared by all packs.
        files: Images in the requested category of the bound packs.
        pack_dirs: Current pack directories keyed by pack ID.
        after: Skip numbers up to and including this value.
        limit: Number of thumbnails requested for the page.

    Returns:
        JPEG bytes, visible numbers mapped to original paths and hashes, and
        the cursor for another page if one exists.
    """
    roots = [
        (pack_id, (Path(path) / "memes").resolve())
        for pack_id, path in pack_dirs.items()
    ]
    assets = {}
    for path in sorted(
        files, key=lambda item: (str(item.parent), item.name.casefold(), item.name)
    ):
        pack_id = next(
            (pack_id for pack_id, root in roots if path.is_relative_to(root)), None
        )
        if pack_id is None or path.is_symlink():
            continue
        try:
            assets.setdefault((pack_id, file_sha256(path)), path)
        except OSError:
            continue
    if not assets:
        return None, {}, None

    database.parent.mkdir(parents=True, exist_ok=True)
    numbered = {}
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS preview_images (
                number INTEGER PRIMARY KEY AUTOINCREMENT,
                pack_id TEXT NOT NULL,
                digest TEXT NOT NULL,
                UNIQUE (pack_id, digest)
            )"""
        )
        # Serialize allocation across simultaneous previews and plugin reloads.
        connection.execute("BEGIN IMMEDIATE")
        for (pack_id, digest), path in assets.items():
            row = connection.execute(
                "SELECT number FROM preview_images WHERE pack_id = ? AND digest = ?",
                (pack_id, digest),
            ).fetchone()
            number = (
                row[0]
                if row
                else connection.execute(
                    "INSERT INTO preview_images (pack_id, digest) VALUES (?, ?)",
                    (pack_id, digest),
                ).lastrowid
            )
            if number > after:
                numbered[number] = (path, digest)

    thumbnails = {}
    seen = {}
    next_after = None
    for number, (path, digest) in sorted(numbered.items()):
        if len(thumbnails) == limit:
            next_after = next(reversed(thumbnails))
            break
        try:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                continue
            with Image.open(io.BytesIO(raw)) as original:
                if original.width * original.height > 40_000_000:
                    continue
                frame = ImageOps.exif_transpose(original)
                frame.thumbnail((184, 176))
                thumbnails[number] = frame.convert("RGBA")
                seen[number] = (path, digest)
        except (OSError, ValueError, Image.DecompressionBombError):
            continue

    if not thumbnails:
        return None, {}, None
    columns = math.ceil(math.sqrt(len(thumbnails)))
    rows = math.ceil(len(thumbnails) / columns)
    cell_width, cell_height = 192, 208
    sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), "#e4e7eb")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=20)
    for index, (number, thumbnail) in enumerate(thumbnails.items()):
        x = index % columns * cell_width
        y = index // columns * cell_height
        draw.rectangle(
            (x + 2, y + 2, x + cell_width - 3, y + cell_height - 3), fill="white"
        )
        sheet.paste(
            thumbnail, (x + (cell_width - thumbnail.width) // 2, y + 4), thumbnail
        )
        draw.text((x + 8, y + 181), f"#{number}", fill="#17202a", font=font)
    with io.BytesIO() as output:
        sheet.save(output, format="JPEG", quality=82, optimize=True)
        return output.getvalue(), seen, next_after


def _has_visible_content(content: object) -> bool:
    """判断助手消息是否包含需要保留的可见正文或非思考内容。"""
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for part in content:
            part_type = getattr(part, "type", None)
            if part_type == "think":
                continue
            if isinstance(part, TextPart) or part_type == "text":
                if str(getattr(part, "text", "") or "").strip():
                    return True
            elif part is not None:
                return True
    return False


def discard_meme_tool_context(
    messages: list[Message],
    *,
    preview_only: bool = False,
    keep_successful_sends: bool = False,
) -> bool:
    """Drop preview images and matched meme tool calls from the agent context.

    Args:
        messages: Live AstrBot messages, edited in place before reuse or saving.
        preview_only: Preserve search and send results while the agent is still running.
        keep_successful_sends: Keep matched successful send calls and compact results
            when cleaning up at the end of the turn.


    Returns:
        Whether a matching tool call was present in the context.
    """
    tool_names = (
        {PREVIEW_TOOL} if preview_only else {PREVIEW_TOOL, "search_memes", "send_meme"}
    )
    removed_calls: set[tuple[int, int]] = set()
    result_edits: dict[int, str | None] = {}
    pending_calls: defaultdict[str, deque[tuple[int, bool]]] = defaultdict(deque)
    decoder = json.JSONDecoder()
    batch_index = 0
    for index, message in enumerate(messages):
        if message.role != "tool":
            # Each assistant batch owns its following tool results. Providers can
            # reuse IDs across batches and for repeated calls in the same batch.
            pending_calls.clear()
            batch_index = index
            for position, call in enumerate(message.tool_calls or ()):
                if isinstance(call, dict):
                    call = ToolCall.model_validate(call)
                if call.function.name in tool_names:
                    removed_calls.add((index, position))
                keep = (
                    keep_successful_sends
                    and call.function.name == "send_meme"
                    and not message._no_save
                )
                pending_calls[call.id].append((position, keep))
            continue

        if message.tool_call_id is None:
            continue
        pending = pending_calls.get(message.tool_call_id)
        if not pending:
            continue
        position, keep = pending.popleft()
        location = (batch_index, position)
        if location not in removed_calls:
            continue
        result_edits[index] = None
        if not keep or not isinstance(message.content, str):
            continue
        # AstrBot can append turn-specific notices after the tool's JSON.
        content = message.content.lstrip()
        try:
            result, end = decoder.raw_decode(content)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(result, dict)
            and result.get("ok") is True
            and result.get("status") == "sent"
        ):
            removed_calls.remove(location)
            result_edits[index] = content[:end]

    retained = []
    marker = f"[Image from tool '{PREVIEW_TOOL}', path='"
    for index, message in enumerate(messages):
        if index in result_edits:
            content = result_edits[index]
            if content is None:
                continue
            if message.content != content:
                message = message.model_copy(update={"content": content})
        elif message.tool_calls:
            calls = [
                call
                for position, call in enumerate(message.tool_calls)
                if (index, position) not in removed_calls
            ]
            if len(calls) != len(message.tool_calls):
                if not calls:
                    if not _has_visible_content(message.content):
                        continue
                    message = message.model_copy(update={"tool_calls": None})
                else:
                    message = message.model_copy(update={"tool_calls": calls})
        elif message.role == "user" and isinstance(message.content, list):
            parts = []
            for part in message.content:
                # AstrBot inserts a path marker directly before each cached tool image.
                if (
                    isinstance(part, ImageURLPart)
                    and parts
                    and isinstance(parts[-1], TextPart)
                ):
                    label = parts[-1].text
                    if (
                        label.startswith(marker)
                        and label == f"{marker}{part.image_url.id}']"
                    ):
                        parts.pop()
                        continue
                parts.append(part)
            if len(parts) != len(message.content):
                if not parts:
                    continue
                message = message.model_copy(update={"content": parts})
        retained.append(message)
    messages[:] = retained
    return bool(removed_calls)
