"""Incremental, source-preserving parsing of meme markers in chat text."""

import re

from .context import ContextScanner, is_escaped
from .text_safety import strip_internal_image_ref_lines
from .types import MemeToken, ParseResult


class MemeParser:
    """Parse lines incrementally while streaming unambiguous text prefixes.

    Args:
        categories: Category keys from the request's active pack.
        alternative: Recognize bracket, parenthesis and colon markers.
        loose: Recognize a category occupying its own line.
        repeated: Recognize standalone repeated category keys.
        remove_invalid: Consume invalid bracket and parenthesis markers.
        semantic: Accept semantic IDs instead of category keys.
        strip_references: Remove standalone internal image references.

    Notes:
        Ambiguous Markdown is buffered until a line boundary. Oversized lines
        and the remaining response are preserved without interpretation. This is a chat protocol scanner,
        not a complete CommonMark renderer. No network or filesystem IO occurs.
    """

    MAX_LINE = 16384
    _markers = re.compile(
        r"&&(?P<strict>[^&\r\n]{1,256})&&"
        r"|\[(?P<bracket>[^\[\]\r\n]{1,256})\]"
        r"|\((?P<paren>[^()\r\n]{1,256})\)"
        r"|(?<![\w:]):(?P<colon>[^:\s]{1,256}):(?![\w:])"
        r"|(?<![\w&])(?P<bare>(?i:meme:[0-9a-f]{12,64}))(?![\w&])"
    )

    def __init__(
        self,
        categories=(),
        *,
        alternative=False,
        loose=False,
        repeated=False,
        remove_invalid=False,
        semantic=False,
        strip_references=False,
    ):
        self.categories = frozenset(categories)
        self.alternative = alternative
        self.loose = loose
        self.repeated = repeated
        self.remove_invalid = remove_invalid
        self.semantic = semantic
        self.strip_references = strip_references
        self.tokens: list[MemeToken] = []
        self._line = ""
        self._emitted = 0
        self._offset = 0
        self._context = ContextScanner()
        self.diagnostics: list[str] = []
        self._passthrough = False
        self._finished = False

    @classmethod
    def parse(cls, text: str, categories=(), **options) -> ParseResult:
        """Parse a complete response using the identical incremental engine.

        Args:
            text: Original complete response.
            categories: Available category keys for this request.
            **options: Constructor options controlling recognized marker forms.

        Returns:
            Immutable visible text, source tokens and resource diagnostics.
        """
        parser = cls(categories, **options)
        visible = parser.feed(text) + parser.finish()
        return ParseResult(visible, tuple(parser.tokens), tuple(parser.diagnostics))

    @property
    def selections(self) -> list[str]:
        """Return validated selections in source order without duplicates."""
        return list(dict.fromkeys(token.value for token in self.tokens if token.valid))

    def feed(self, text: str) -> str:
        """Consume a text delta and return text safe to send now.

        Args:
            text: Newly received text, not the accumulated response.

        Returns:
            Visible text whose interpretation no longer needs more input.

        Raises:
            RuntimeError: If called after finish.
        """
        if self._finished:
            raise RuntimeError("Cannot feed a finished meme parser")
        output = []
        for part in text.splitlines(keepends=True):
            # Only LF terminates protocol lines; other separators stay literal.
            complete = part.endswith("\n")
            if self._passthrough:
                output.append(part)
                self._offset += len(part)
                continue
            self._line += part
            if len(self._line) > self.MAX_LINE:
                output.append(self._line[self._emitted :])
                self._offset += len(self._line)
                self._line = ""
                self._emitted = 0
                self._passthrough = True
                self.diagnostics.append("line_limit_exceeded")
                continue
            if complete:
                output.append(self._consume_line())
            elif not self._context.active:
                # A completed ordinary word before any syntax is immutable.
                # Keep indentation and potential standalone fallback tokens.
                syntax = re.search(r"[&`~\[\]()<>:\\\n]", self._line)
                prefix = self._line[: syntax.start()] if syntax else self._line
                if self.semantic:
                    pending_id = re.search(r"(?i)\bm(?:e(?:m(?:e)?)?)?$", prefix)
                    if pending_id:
                        prefix = prefix[: pending_id.start()]
                if self.strip_references and re.fullmatch(
                    r"(?i)\s*f(?:i(?:l(?:e)?)?)?", prefix
                ):
                    prefix = ""
                boundary = len(prefix)
                first = prefix.strip().split(" ", 1)[0]
                fallback = self.loose or self.repeated
                ambiguous = fallback and any(
                    key.startswith(first) or first.startswith(key)
                    for key in self.categories
                )
                if (
                    boundary > self._emitted
                    and prefix.strip()
                    and not ambiguous
                    and not self._line.startswith(("    ", "\t"))
                ):
                    output.append(self._line[self._emitted : boundary])
                    self._emitted = boundary
        return "".join(output)

    def finish(self) -> str:
        """Flush the final line; incomplete markers remain literal text.

        Returns:
            Remaining visible text. Repeated calls return an empty string.
        """
        if self._finished:
            return ""
        self._finished = True
        return self._consume_line() if self._line else ""

    def _consume_line(self) -> str:
        """Resolve one bounded line and update cross-line protection state.

        Returns:
            The un-emitted part of the line after recognized marker edits.
        """
        line = self._line
        continued_context = self._context.active
        protected = self._context.scan(line)
        if self._context.overflow:
            self._passthrough = True
            self.diagnostics.append("context_limit_exceeded")

        edits = []
        if (
            self.strip_references
            and not continued_context
            and not strip_internal_image_ref_lines(line)
        ):
            self.tokens.append(
                MemeToken(
                    self._offset, self._offset + len(line), line, "reference", "", False
                )
            )
            self._offset += len(line)
            self._line = ""
            self._emitted = 0
            return ""
        protected_index = 0
        for match in self._markers.finditer(line):
            while (
                protected_index < len(protected)
                and protected[protected_index][1] <= match.start()
            ):
                protected_index += 1
            if (
                protected_index < len(protected)
                and protected[protected_index][0] < match.end()
            ):
                continue
            if is_escaped(line, match.start()):
                continue
            kind = match.lastgroup
            if kind == "paren":
                before, after = line[: match.start()], line[match.end() :]
                if (
                    re.search(r"[a-zA-Z0-9_]$", before)
                    or re.match(r"[a-zA-Z0-9_]", after)
                    or (
                        re.search(r"[a-zA-Z]\s*$", before)
                        and re.match(r"\s*[a-zA-Z]", after)
                    )
                ):
                    continue
            value = match[kind].strip()
            if kind == "bare" and not self.semantic:
                continue
            if kind in {"strict", "bare"}:
                semantic_id = re.fullmatch(r"meme:[0-9a-f]{12,64}", value, re.I)
                valid = bool(semantic_id) if self.semantic else value in self.categories
                if semantic_id:
                    value = value.lower()
                consume = True
            elif self.alternative and not self.semantic:
                valid = value in self.categories
                consume = valid or (
                    self.remove_invalid and kind in {"bracket", "paren"}
                )
            else:
                continue
            if consume:
                edits.append((match.start(), match.end()))
                self.tokens.append(
                    MemeToken(
                        self._offset + match.start(),
                        self._offset + match.end(),
                        match[0],
                        kind,
                        value,
                        valid,
                    )
                )

        if (
            not edits
            and not self.semantic
            and not protected
            and (self.loose or self.repeated)
        ):
            word = line.strip().rstrip("。！？，、.!?,")
            value = word if self.loose and word in self.categories else ""
            if not value and self.repeated:
                value = next(
                    (
                        key
                        for key in sorted(
                            self.categories, key=lambda key: (-len(key), key)
                        )
                        if len(key) >= 2
                        and len(word) >= 2 * len(key)
                        and word == key * (len(word) // len(key))
                    ),
                    "",
                )
            if value and not line.startswith(("    ", "\t")):
                start = len(line) - len(line.lstrip())
                end = start + len(word)
                edits.append((start, end))
                self.tokens.append(
                    MemeToken(
                        self._offset + start,
                        self._offset + end,
                        word,
                        "fallback",
                        value,
                        True,
                    )
                )

        parts = []
        previous = self._emitted
        for start, end in edits:
            parts.append(line[previous:start])
            previous = end
        parts.append(line[previous:])
        self._offset += len(line)
        self._line = ""
        self._emitted = 0
        return "".join(parts)
