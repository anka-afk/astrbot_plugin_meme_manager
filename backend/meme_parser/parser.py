"""增量解析聊天文本中的表情标记，并保留来源文本结构。"""

import copy
import re

from .context import ContextScanner, is_escaped
from .text_safety import strip_internal_image_ref_lines
from .types import MemeToken, ParseResult


class MemeParser:
    """增量解析各行，同时输出含义已确定的文本前缀。

    Args:
        categories: 当前请求所用表情包的分类键。
        alternative: 识别方括号、圆括号与冒号标记。
        loose: 识别独占一行的分类。
        repeated: 识别独立重复出现的分类键。
        remove_invalid: 消费无效的方括号与圆括号标记。
        semantic: 接受语义 ID 而不是分类键。
        strip_references: 移除独立的内部图片引用。
        filter_all: 最后清除所有标记形式，不改变已选结果。

    Notes:
        尚未确认的行内代码会缓冲到闭合或回复结束。
        超长行或待确认的行内区间
        及其后续回复会原样保留而不再解释。这是聊天协议扫描器，
        不是完整的 CommonMark 渲染器，不涉及网络或文件系统输入输出。
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
        filter_all=False,
    ):
        self.categories = frozenset(categories)
        self.alternative = alternative
        self.loose = loose
        self.repeated = repeated
        self.remove_invalid = remove_invalid
        self.semantic = semantic
        self.strip_references = strip_references
        self.filter_all = filter_all
        self.tokens: list[MemeToken] = []
        self._line = ""
        self._emitted = 0
        self._offset = 0
        self._context = ContextScanner()
        self._pending_lines: list[str] = []
        self._pending_size = 0
        self._pending_context: ContextScanner | None = None
        self._pending_emitted = 0
        self._inline_limit = False
        self.diagnostics: list[str] = []
        self._passthrough = False
        self._finished = False

    @classmethod
    def parse(cls, text: str, categories=(), **options) -> ParseResult:
        """使用同一个增量引擎解析完整回复。

        Args:
            text: 原始完整回复。
            categories: 本次请求可用的分类键。
            **options: 控制可识别标记形式的构造参数。

        Returns:
            不可变的可见文本、来源标记与资源诊断信息。
        """
        parser = cls(categories, **options)
        visible = parser.feed(text) + parser.finish()
        return ParseResult(visible, tuple(parser.tokens), tuple(parser.diagnostics))

    @property
    def selections(self) -> list[str]:
        """按来源顺序返回已校验且去重的选择结果。"""
        return list(dict.fromkeys(token.value for token in self.tokens if token.valid))

    def feed(self, text: str) -> str:
        """消费一个文本增量，并返回当前可以安全发送的文本。

        Args:
            text: 新收到的文本，而不是累计回复。

        Returns:
            无需更多输入即可确定含义的可见文本。

        Raises:
            RuntimeError: 在 finish 之后调用。
        """
        if self._finished:
            raise RuntimeError("Cannot feed a finished meme parser")
        output = []
        for part in text.splitlines(keepends=True):
            # 只有 LF 才能结束协议行；其他分隔符按原文保留。
            complete = part.endswith("\n")
            if self._passthrough:
                output.append(part)
                self._offset += len(part)
                continue
            self._line += part
            pending_limit = (
                self._pending_size > 0
                and self._pending_size + len(self._line) > self.MAX_LINE
            )
            if pending_limit and self.filter_all:
                # 放弃推测行内代码后，仍继续执行最终标签过滤。
                self._inline_limit = True
                self.diagnostics.append("inline_code_limit_exceeded")
                output.append(self._flush_pending())
            if (
                pending_limit or len(self._line) > self.MAX_LINE
            ) and not self.filter_all:
                output.append(
                    "".join(self._pending_lines)[self._pending_emitted :]
                    + self._line[self._emitted :]
                )
                self._offset += self._pending_size + len(self._line)
                self._pending_lines.clear()
                self._pending_size = 0
                self._pending_context = None
                self._pending_emitted = 0
                self._line = ""
                self._emitted = 0
                self._passthrough = True
                self.diagnostics.append(
                    "inline_code_limit_exceeded"
                    if pending_limit
                    else "line_limit_exceeded"
                )
                continue
            if complete:
                output.append(self._consume_line())
            elif not self._context.active:
                # 任何语法出现之前，已完成的普通词不能再修改。
                # 保留缩进和可能独立出现的回退标记。
                syntax = re.search(r"[&`~\[\]()<>:\\\n]", self._line)
                prefix = self._line[: syntax.start()] if syntax else self._line
                if self.semantic or self.filter_all:
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
        """Flush the final line, including standalone unterminated semantic IDs.

        Returns:
            Remaining visible text, or an empty string on repeated calls.
        """
        if self._finished:
            return ""
        self._finished = True
        if self._line:
            if not self._pending_lines:
                self._pending_context = copy.deepcopy(self._context)
                self._pending_emitted = self._emitted
            self._pending_lines.append(self._line)
            self._line = ""
            self._emitted = 0
        return self._flush_pending(final=not self._inline_limit)

    def _flush_pending(self, *, final: bool = False) -> str:
        """代码保护状态可安全确定后，重新处理延后的各行。

        Args:
            final: 将后续没有等长匹配序列的反引号序列视为字面文本。

        Returns:
            已解析且尚未输出的文本。
        """
        if not self._pending_lines:
            return ""
        lines = self._pending_lines
        emitted = self._pending_emitted
        assert self._pending_context is not None
        self._context = self._pending_context
        self._pending_lines = []
        self._pending_size = 0
        self._pending_context = None
        self._pending_emitted = 0
        literal_ticks = set()
        if final:
            text = "".join(lines)
            runs = list(ContextScanner._ticks.finditer(text))
            last_ticks = {len(match[0]): match.start() for match in runs}
            literal_ticks.update(last_ticks.values())
            for match in runs:
                # 在代码外，转义仅消费连续反引号中的第一个。
                # 代码内的闭合符仍须匹配完整的原始反引号序列。
                if (
                    len(match[0]) > 1
                    and is_escaped(text, match.start())
                    and last_ticks.get(len(match[0]) - 1, -1) < match.start() + 1
                ):
                    literal_ticks.add(match.start() + 1)
        trailing_line, trailing_emitted = self._line, self._emitted
        output = []
        offset = 0
        positions = iter(sorted(literal_ticks))
        position = next(positions, None)
        for line in lines:
            line_ticks = set()
            while position is not None and position < offset + len(line):
                line_ticks.add(position - offset)
                position = next(positions, None)
            self._line, self._emitted = line, emitted
            if self._passthrough:
                output.append(line[emitted:])
                self._offset += len(line)
            else:
                output.append(self._consume_line(literal_ticks=line_ticks))
            offset += len(line)
            emitted = 0
        self._line, self._emitted = trailing_line, trailing_emitted
        return "".join(output)

    def _consume_line(self, *, literal_ticks: set[int] | None = None) -> str:
        """解析一行有长度限制的文本，并更新跨行保护状态。

        Args:
            literal_ticks: 重新处理已解析区块时已知的字面反引号位置。
                设为 None 时，允许延后处理尚未闭合的行内代码。

        Returns:
            编辑已识别标记后，该行尚未输出的部分。
        """
        line = self._line
        checkpoint = (
            copy.deepcopy(self._context)
            if literal_ticks is None and not self._pending_lines and "`" in line
            else None
        )
        continued_context = self._context.active
        protected = self._context.scan(line, literal_ticks=literal_ticks)
        if (
            literal_ticks is None
            and not self._inline_limit
            and (self._pending_lines or self._context.ticks)
        ):
            if not self._pending_lines:
                self._pending_context = checkpoint
                self._pending_emitted = self._emitted
            self._pending_lines.append(line)
            self._pending_size += len(line)
            self._line = ""
            self._emitted = 0
            return self._flush_pending() if not self._context.ticks else ""
        if self._context.overflow and not self.filter_all:
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

        # Recover only a whole-line semantic ID after its full extent is known.
        # Keep this outside _markers so final filtering retains its existing scope.
        if self.semantic and not edits and not protected:
            match = re.fullmatch(
                r"[ \t]*(&&(?i:meme:[0-9a-f]{12,64}))[ \t]*(?:\r?\n)?", line
            )
            if match:
                start, end = match.span(1)
                raw = match[1]
                edits.append((start, end))
                self.tokens.append(
                    MemeToken(
                        self._offset + start,
                        self._offset + end,
                        raw,
                        "unterminated",
                        raw[2:].lower(),
                        True,
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
        visible = "".join(parts)
        # 在常规选择之后执行清理，避免抑制有效图片。
        if self.filter_all:
            visible = self._markers.sub("", visible)
        return visible
