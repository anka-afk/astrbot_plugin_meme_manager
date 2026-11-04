"""不可变的解析结果，包含原始来源坐标。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MemeToken:
    """已消费的标记，附带其在原始回复中的字符偏移。"""

    start: int
    end: int
    raw: str
    kind: str
    value: str
    valid: bool


@dataclass(frozen=True)
class ParseResult:
    """可见文本、已消费标记及明确的资源限制诊断信息。"""

    text: str
    tokens: tuple[MemeToken, ...]
    diagnostics: tuple[str, ...]

    @property
    def selections(self) -> tuple[str, ...]:
        """按来源顺序返回有效且去重的选择结果。"""
        return tuple(dict.fromkeys(token.value for token in self.tokens if token.valid))
