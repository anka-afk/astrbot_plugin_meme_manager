"""Immutable parser output with original-source coordinates."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MemeToken:
    """A consumed marker with character offsets into the original response."""

    start: int
    end: int
    raw: str
    kind: str
    value: str
    valid: bool


@dataclass(frozen=True)
class ParseResult:
    """Visible text, consumed tokens, and explicit resource-limit diagnostics."""

    text: str
    tokens: tuple[MemeToken, ...]
    diagnostics: tuple[str, ...]

    @property
    def selections(self) -> tuple[str, ...]:
        """Return valid selections in source order, without duplicates."""
        return tuple(dict.fromkeys(token.value for token in self.tokens if token.valid))
