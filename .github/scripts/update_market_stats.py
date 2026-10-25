"""Generate README cards from the official AstrBot marketplace snapshot."""

import json
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.request import Request, urlopen

SOURCE_URL = "https://api.soulter.top/astrbot/plugins"
REPOSITORY = "https://github.com/anka-afk/astrbot_plugin_meme_manager"
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "img" / "stats"


def update_cards(market: dict, output_dir: Path) -> dict:
    """Validate a snapshot and generate three dated, accessible SVG cards.

    Args:
        market: Official marketplace JSON, including its metadata.
        output_dir: Directory for generated cards and their source summary.

    Returns:
        The counts, ranks, and source timestamp used for the cards.

    Raises:
        ValueError: If the snapshot is incomplete or the plugin is ambiguous.
    """
    updated_at = market.get("$meta", {}).get("updated_at", "")
    timestamp = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    if timestamp.utcoffset() is None:
        raise ValueError("Market timestamp must include a timezone")
    entries = [value for key, value in market.items() if key != "$meta"]
    for entry in entries:
        for metric in ("download_count", "stars"):
            if (
                not isinstance(entry, dict)
                or type(entry.get(metric)) is not int
                or entry[metric] < 0
            ):
                raise ValueError(f"Missing or invalid market metric: {metric}")
    matches = [entry for entry in entries if entry.get("repo") == REPOSITORY]
    if len(matches) != 1:
        raise ValueError("Expected exactly one market entry for this repository")
    plugin = matches[0]
    stats = {
        "source": SOURCE_URL,
        "source_updated_at": updated_at,
        "repository": REPOSITORY,
        "market_entries": len(entries),
        "download_count": plugin["download_count"],
        "stars": plugin["stars"],
        "download_rank": 1
        + sum(entry["download_count"] > plugin["download_count"] for entry in entries),
        "star_rank": 1 + sum(entry["stars"] > plugin["stars"] for entry in entries),
    }
    # Rank ties share a position; only strictly greater values precede this entry.
    date_label = timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cards = (
        (
            "download-rank",
            "市场下载量排名",
            f"#{stats['download_rank']}",
            "#946200",
            "#fff8e6",
        ),
        (
            "downloads",
            "市场下载量",
            f"{stats['download_count']:,}",
            "#086b91",
            "#eefaff",
        ),
        ("star-rank", "市场 Star 排名", f"#{stats['star_rank']}", "#6550a1", "#f7f4ff"),
    )
    files = {}
    for filename, label, value, accent, background in cards:
        title = escape(f"{label}：{value}；数据更新于 {date_label}")
        files[
            f"{filename}.svg"
        ] = f"""<svg xmlns="http://www.w3.org/2000/svg" width="260" height="132" viewBox="0 0 260 132" role="img" aria-labelledby="title desc">
  <title id="title">{title}</title>
  <desc id="desc">Source: {SOURCE_URL}. Rankings compare entries in the official AstrBot marketplace; equal values share a rank.</desc>
  <rect x="0.5" y="0.5" width="259" height="131" rx="14" fill="{background}" stroke="{accent}" stroke-opacity=".2"/>
  <g font-family="Arial, Microsoft YaHei, sans-serif">
    <text x="20" y="24" font-size="10" letter-spacing="1.2" fill="{accent}">ASTRBOT PLUGIN MARKET</text>
    <text x="20" y="67" font-size="38" font-weight="700" fill="{accent}">{value}</text>
    <text x="20" y="92" font-size="14" fill="#243449">{label}</text>
    <text x="20" y="115" font-size="10" fill="#586779">{date_label}</text>
  </g>
</svg>
"""
    files["snapshot.json"] = json.dumps(stats, ensure_ascii=False, indent=2) + "\n"
    # Validate and render everything before replacing the last successful snapshot.
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in files.items():
        (output_dir / filename).write_text(content, encoding="utf-8")
    return stats


if __name__ == "__main__":
    request = Request(SOURCE_URL, headers={"User-Agent": "meme-manager-readme-stats"})
    with urlopen(request, timeout=30) as response:
        snapshot = json.load(response)
    print(json.dumps(update_cards(snapshot, OUTPUT_DIR), indent=2))
