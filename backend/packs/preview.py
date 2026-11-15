"""Build numbered contact sheets for the reply model."""

import hashlib
import io
import math
import sqlite3
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


def discard_meme_tool_context(
    messages: list[Message], *, preview_only: bool = False
) -> bool:
    """Drop preview images and matched meme tool calls from the agent context.

    Args:
        messages: Live AstrBot messages, edited in place before reuse or saving.
        preview_only: Preserve search and send results while the agent is still running.

    Returns:
        Whether a matching tool call was present in the context.
    """
    tool_names = (
        {PREVIEW_TOOL} if preview_only else {PREVIEW_TOOL, "search_memes", "send_meme"}
    )
    removed_ids = set()
    for message in messages:
        for call in message.tool_calls or ():
            if isinstance(call, dict):
                call = ToolCall.model_validate(call)
            if call.function.name in tool_names:
                removed_ids.add(call.id)

    retained = []
    marker = f"[Image from tool '{PREVIEW_TOOL}', path='"
    for message in messages:
        if message.role == "tool" and message.tool_call_id in removed_ids:
            continue
        if message.tool_calls:
            calls = [
                call
                for call in message.tool_calls
                if (call.get("id") if isinstance(call, dict) else call.id)
                not in removed_ids
            ]
            if len(calls) != len(message.tool_calls):
                if not calls:
                    continue
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
    return bool(removed_ids)
