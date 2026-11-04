"""识别标记前，有状态地保护字面文本区域。"""

import re

from .text_safety import _protected_reference_spans


def is_escaped(text: str, position: int) -> bool:
    """检查来源文本指定位置的 Markdown 转义奇偶性。

    Args:
        text: 原始来源文本。
        position: 可能的分隔符位置。

    Returns:
        分隔符前是否有奇数个反斜杠。
    """
    cursor = position
    while cursor and text[cursor - 1] == "\\":
        cursor -= 1
    return (position - cursor) % 2 == 1


class ContextScanner:
    """跨协议行跟踪字面文本区域，不渲染 Markdown。"""

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
        """返回受保护区域是否从上一行延续。"""
        return bool(
            self.fence or self.reasoning or self.ticks or self.comment or self.link
        )

    def scan(
        self, line: str, *, literal_ticks: set[int] | None = None
    ) -> list[tuple[int, int]]:
        """定位受保护区间，为下一行保留未完成的上下文。

        Args:
            line: 一行原始来源文本，包含行尾符。
            literal_ticks: 无法开启匹配代码片段的反引号序列，
                以本行内的字符偏移表示。

        Returns:
            有序且不重叠的受保护字符区间。
        """
        spans = _protected_reference_spans(line, protect_inline_code=False)
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
        plain_label_end = 0
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
                if literal_ticks is not None and cursor in literal_ticks:
                    cursor = match.end()
                    continue
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
            if line[cursor] == "[" and cursor >= plain_label_end:
                # 成对的标签可保护嵌套标记和目标地址；单独且完整的
                # [category] 仍交由标记识别器处理。
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
                if "`" in line[cursor + 1 : end - 1]:
                    # 代码可能在链接标签内开始，并在括号外闭合。
                    # 遍历标签内容时，跳过对嵌套括号的重复扫描。
                    plain_label_end = end
                    cursor += 1
                    continue
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
