"""Stateful protection of literal regions before marker recognition."""

import re

from .text_safety import _protected_reference_spans


def is_escaped(text: str, position: int) -> bool:
    """Check Markdown escape parity at a source position.

    Args:
        text: Original source text.
        position: Position of the possible delimiter.

    Returns:
        Whether an odd number of backslashes precedes the delimiter.
    """
    cursor = position
    while cursor and text[cursor - 1] == "\\":
        cursor -= 1
    return (position - cursor) % 2 == 1


class ContextScanner:
    """Track literal regions across protocol lines without rendering Markdown."""

    _fence = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
    _reason = re.compile(r"</?(think|thinking|analysis|reasoning)\b[^>]*>", re.I)
    _ticks = re.compile(r"`+")
    MAX_DEPTH = 64

    def __init__(self):
        self.fence: tuple[str, int] | None = None
        self.reasoning: list[str] = []
        self.ticks = 0
        self.comment = False
        self.link: tuple[str, int] | None = None
        self.overflow = False

    @property
    def active(self) -> bool:
        """Return whether a protected region continues from the previous line."""
        return bool(
            self.fence or self.reasoning or self.ticks or self.comment or self.link
        )

    def scan(self, line: str) -> list[tuple[int, int]]:
        """Locate protected spans and retain unfinished context for the next line.

        Args:
            line: One original source line, including its line ending.

        Returns:
            Ordered non-overlapping protected character intervals.
        """
        spans = _protected_reference_spans(line)
        fence = self._fence.match(line.rstrip("\r\n"))
        if self.fence:
            if (
                fence
                and fence[1][0] == self.fence[0]
                and len(fence[1]) >= self.fence[1]
                and not fence[2].strip()
            ):
                self.fence = None
            return [(0, len(line))]
        if not self.active and fence and not (fence[1][0] == "`" and "`" in fence[2]):
            self.fence = (fence[1][0], len(fence[1]))
            return [(0, len(line))]
        if not self.active and line.startswith(("    ", "\t")):
            return [(0, len(line))]

        start = 0 if self.active else None
        cursor = 0
        while cursor < len(line):
            if self.comment:
                end = line.find("-->", cursor)
                if end < 0:
                    break
                cursor = end + 3
                self.comment = False
                spans.append((start or 0, cursor))
                start = None
                continue
            if self.ticks:
                match = self._ticks.search(line, cursor)
                if not match:
                    break
                cursor = match.end()
                if len(match[0]) == self.ticks:
                    self.ticks = 0
                    spans.append((start or 0, cursor))
                    start = None
                continue
            if self.reasoning:
                match = self._reason.search(line, cursor)
                if not match:
                    break
                cursor = match.end()
                name = match[1].lower()
                if not match[0].startswith("</"):
                    if len(self.reasoning) >= self.MAX_DEPTH:
                        self.overflow = True
                        return [(0, len(line))]
                    self.reasoning.append(name)
                elif name == self.reasoning[-1]:
                    self.reasoning.pop()
                    if not self.reasoning:
                        spans.append((start or 0, cursor))
                        start = None
                continue
            if self.link:
                phase, depth = self.link
                opening, closing = ("[", "]") if phase == "label" else ("(", ")")
                if line[cursor] == "\\" and cursor + 1 < len(line):
                    cursor += 2
                    continue
                if not is_escaped(line, cursor):
                    if line[cursor] == opening:
                        depth += 1
                    elif line[cursor] == closing:
                        depth -= 1
                cursor += 1
                self.link = (phase, depth)
                if depth == 0:
                    if phase == "label" and cursor < len(line) and line[cursor] == "(":
                        self.link = ("destination", 1)
                        cursor += 1
                    else:
                        self.link = None
                        spans.append((start or 0, cursor))
                        start = None
                continue
            if line[cursor] == "\\" and cursor + 1 < len(line):
                cursor += 2
                continue
            if line.startswith("<!--", cursor):
                self.comment = True
                start = cursor
                cursor += 4
                continue
            if line[cursor] == "`":
                match = self._ticks.match(line, cursor)
                self.ticks = len(match[0])
                start = cursor
                cursor += self.ticks
                continue
            reason = self._reason.match(line, cursor)
            if reason and not reason[0].startswith("</"):
                self.reasoning.append(reason[1].lower())
                start = cursor
                cursor = reason.end()
                continue
            if line[cursor] == "[":
                # Balanced labels protect nested markers and destinations. A bare
                # complete [category] stays available to the marker recognizer.
                end, depth = cursor + 1, 1
                while end < len(line) and depth:
                    if line[end] == "\\" and end + 1 < len(line):
                        end += 2
                        continue
                    if not is_escaped(line, end):
                        depth += (line[end] == "[") - (line[end] == "]")
                    end += 1
                if depth:
                    self.link = ("label", depth)
                    start = cursor
                    break
                if end < len(line) and line[end] in "([":
                    start = cursor
                    self.link = ("destination" if line[end] == "(" else "label", 1)
                    cursor = end + 1
                    continue
                if end < len(line) and line[end] == ":" and not line[:cursor].strip():
                    spans.append((cursor, len(line)))
                    break
                if "[" in line[cursor + 1 : end - 1]:
                    spans.append((cursor, end))
                cursor = end
                continue
            cursor += 1
        if start is not None:
            spans.append((start, len(line)))
        merged = []
        for begin, end in sorted(spans):
            if merged and begin <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((begin, end))
        return merged
