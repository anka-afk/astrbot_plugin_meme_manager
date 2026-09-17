from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from PIL import Image

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


@dataclass(frozen=True, slots=True)
class ImageMatch:
    """Describe a byte-identical image and an optional visual review hint.

    Args:
        exact_path: Existing image with identical SHA-256, if found.
        similar_path: Static image with a nearby dHash, for human review only.
        distance: Number of differing dHash bits for the visual hint.
    """

    exact_path: Path | None = None
    similar_path: Path | None = None
    distance: int | None = None


class AutoCollectImageIndex:
    """Cache file fingerprints per pack, with bounded least-recently-used packs."""

    def __init__(self, max_packs: int = 8):
        """Initialize an index safe for concurrent background lookups.

        Args:
            max_packs: Maximum number of pack manifests retained in memory.
        """
        self.max_packs = max(1, max_packs)
        self._packs: OrderedDict[Path, dict[Path, tuple[int, int, str, int | None]]] = (
            OrderedDict()
        )
        self._lock = RLock()

    @staticmethod
    def _difference_hash(path: Path) -> int | None:
        """Decode a bounded static image for an advisory visual fingerprint.

        Args:
            path: Image to inspect.

        Returns:
            A 64-bit dHash, or None for animated, oversized, or invalid images.
        """
        try:
            with Image.open(path) as image:
                if (
                    getattr(image, "is_animated", False)
                    or image.width * image.height > 40_000_000
                ):
                    return None
                with image.convert("L") as grayscale:
                    with grayscale.resize((9, 8), Image.Resampling.LANCZOS) as small:
                        pixels = small.tobytes()
                fingerprint = 0
                for y in range(8):
                    for x in range(8):
                        fingerprint = (fingerprint << 1) | (
                            pixels[y * 9 + x] > pixels[y * 9 + x + 1]
                        )
                return fingerprint
        except (OSError, ValueError, Image.DecompressionBombError):
            return None

    def lookup(
        self, pack_root: Path, digest: str, candidate_path: Path | None = None
    ) -> ImageMatch:
        """Refresh cheap file stats and find an exact match or a review hint.

        This method performs disk reads and image decoding. Async callers must
        use a worker thread. Only changed files are hashed again. Visual hashes
        are computed lazily when a candidate image is supplied. Approximate
        matches must never be treated as duplicates: text variants may matter.

        Args:
            pack_root: Directory containing the target pack's images.
            digest: SHA-256 of the candidate's original bytes.
            candidate_path: Optional candidate file for visual similarity hints.

        Returns:
            Exact match or closest static-image hint within five differing bits.
        """
        root = Path(pack_root).resolve()
        with self._lock:
            previous = self._packs.pop(root, {})
            current = {}
            # Prune links before traversal, including Windows directory junctions.
            directories = [root]
            visited = set()
            while directories:
                directory = directories.pop()
                try:
                    resolved = directory.resolve()
                    if not resolved.is_relative_to(root) or resolved in visited:
                        continue
                    visited.add(resolved)
                    children = list(directory.iterdir())
                except (OSError, RuntimeError):
                    continue
                for path in children:
                    try:
                        if path.is_symlink() or not path.resolve().is_relative_to(root):
                            continue
                        if path.is_dir():
                            directories.append(path)
                            continue
                        if (
                            path.suffix.lower() not in IMAGE_EXTENSIONS
                            or not path.is_file()
                        ):
                            continue
                        stat = path.stat()
                        signature = (stat.st_size, stat.st_mtime_ns)
                        cached = previous.get(path)
                        if cached is not None and cached[:2] == signature:
                            if candidate_path is not None and cached[3] == -1:
                                cached = (*cached[:3], self._difference_hash(path))
                            current[path] = cached
                            continue
                        hasher = hashlib.sha256()
                        with path.open("rb") as source:
                            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                hasher.update(chunk)
                        # -1 means not requested yet; None means visual matching
                        # is unavailable, for example for an animated image.
                        visual_hash = (
                            self._difference_hash(path)
                            if candidate_path is not None
                            else -1
                        )
                        # A concurrently replaced file must be re-read next time.
                        after = path.stat()
                        if signature != (after.st_size, after.st_mtime_ns):
                            continue
                        current[path] = (*signature, hasher.hexdigest(), visual_hash)
                    except (OSError, RuntimeError):
                        continue
            self._packs[root] = current
            while len(self._packs) > self.max_packs:
                self._packs.popitem(last=False)
            for path, (_, _, existing_digest, _) in current.items():
                if digest == existing_digest:
                    return ImageMatch(exact_path=path)
            if candidate_path is None:
                return ImageMatch()
            candidate_hash = self._difference_hash(candidate_path)
            if candidate_hash is None:
                return ImageMatch()
            closest = ImageMatch()
            for path, (_, _, _, visual_hash) in current.items():
                if visual_hash is None:
                    continue
                distance = (candidate_hash ^ visual_hash).bit_count()
                if distance <= 5 and (
                    closest.distance is None or distance < closest.distance
                ):
                    closest = ImageMatch(similar_path=path, distance=distance)
            return closest
