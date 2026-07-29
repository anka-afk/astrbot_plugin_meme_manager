"""用于响应清理和情绪匹配的纯文本安全工具。"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")
_IMAGE_REF_LINE_RE = re.compile(
    r"^ {0,3}\[image[ \t]+ref[ \t]+[1-9]\d*\]"
    r"(?:[ \t]*:[ \t]*|[ \t]+)(?P<reference>.*\S)[ \t]*$",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")
_DATA_IMAGE_RE = re.compile(
    r"^data:image/(?:jpe?g|png|gif|webp);base64,[a-z0-9+/=_-]+$",
    re.IGNORECASE,
)
_BASE64_IMAGE_RE = re.compile(r"^base64://[a-z0-9+/=_-]+$", re.IGNORECASE)
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[a-z]:[\\/]", re.IGNORECASE)
_UNC_PATH_RE = re.compile(r"^(?:\\\\|//)[^\\/]+[\\/][^\\/]+")
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]\r\n]*\]\([^)\r\n]*\)")
_INLINE_CODE_RE = re.compile(r"(?P<fence>`+)[^\r\n]*?(?P=fence)")
_URI_RE = re.compile(
    r"(?:file|https?|data|base64):[^\s,，。！？；：、]+",
    re.IGNORECASE,
)
_REFERENCE_TOKEN_RE = re.compile(r"[^\s,;!?，。！？；：、]+")
_REFERENCE_SCHEME_RE = re.compile(r"(?:file|https?|data|base64):", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}",
    re.IGNORECASE,
)


def _unwrap_reference(value: str) -> str:
    reference = value.strip()
    if reference.startswith("<") and reference.endswith(">"):
        return reference[1:-1].strip()
    return reference


def _has_supported_image_suffix(value: str) -> bool:
    return unquote(value).lower().endswith(_IMAGE_SUFFIXES)


def _is_supported_image_reference(value: str) -> bool:
    reference = _unwrap_reference(value)
    if not reference:
        return False

    if _DATA_IMAGE_RE.fullmatch(reference) or _BASE64_IMAGE_RE.fullmatch(reference):
        return True

    try:
        parsed = urlsplit(reference)
    except ValueError:
        return False
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https"}:
        return bool(parsed.netloc) and _has_supported_image_suffix(parsed.path)
    if scheme == "file":
        file_target = reference[len(parsed.scheme) + 1 :]
        is_absolute = bool(parsed.netloc) or file_target.startswith("/")
        return is_absolute and _has_supported_image_suffix(parsed.path)

    decoded_reference = unquote(reference)
    is_absolute_path = (
        decoded_reference.startswith("/")
        or bool(_WINDOWS_ABSOLUTE_PATH_RE.match(decoded_reference))
        or bool(_UNC_PATH_RE.match(decoded_reference))
    )
    return is_absolute_path and _has_supported_image_suffix(decoded_reference)


def _is_plugin_owned_file_image_reference(line_body: str) -> bool:
    leading_spaces = len(line_body) - len(line_body.lstrip(" "))
    if leading_spaces > 3 or line_body[leading_spaces:].startswith("\t"):
        return False

    reference = _unwrap_reference(line_body)
    try:
        parsed = urlsplit(reference)
    except ValueError:
        return False
    if parsed.scheme.lower() != "file" or not _is_supported_image_reference(reference):
        return False

    path_parts = [
        part.casefold()
        for part in unquote(parsed.path).replace("\\", "/").split("/")
        if part
    ]
    return any(
        path_parts[index : index + 2] == ["plugin_data", "meme_manager"]
        for index in range(len(path_parts) - 1)
    )


def strip_internal_image_ref_lines(text: str) -> str:
    """移除机器生成的独立图片引用行。

    带编号标记的行可以使用任意受支持的图片引用。没有标记的行仅在引用
    本插件 ``plugin_data/meme_manager`` 目录下的本地 ``file:`` 图片 URI 时
    才会被移除。Markdown 代码保持不变。
    """

    if not text:
        return text

    kept_lines: list[str] = []
    active_fence: tuple[str, int] | None = None

    for line in text.splitlines(keepends=True):
        line_body = line.rstrip("\r\n")
        fence_match = _FENCE_RE.match(line_body)

        if active_fence is not None:
            kept_lines.append(line)
            if fence_match:
                fence = fence_match.group("fence")
                is_closing_fence = not line_body[fence_match.end() :].strip()
                if (
                    fence[0] == active_fence[0]
                    and len(fence) >= active_fence[1]
                    and is_closing_fence
                ):
                    active_fence = None
            continue

        if fence_match:
            fence = fence_match.group("fence")
            active_fence = (fence[0], len(fence))
            kept_lines.append(line)
            continue

        marker_match = _IMAGE_REF_LINE_RE.fullmatch(line_body)
        is_marked_reference = marker_match and _is_supported_image_reference(
            marker_match.group("reference")
        )
        if is_marked_reference or _is_plugin_owned_file_image_reference(line_body):
            continue

        kept_lines.append(line)

    return "".join(kept_lines)


def _token_is_reference(token: str) -> bool:
    if "/" in token or "\\" in token:
        return True
    if _REFERENCE_SCHEME_RE.search(token) or _DOMAIN_RE.search(token):
        return True

    filename_token = token.strip("\"'`<>()[]{}，。！？；：、,.!?;:")
    return _has_supported_image_suffix(filename_token)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []

    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def _protected_reference_spans(text: str) -> list[tuple[int, int]]:
    spans = [
        match.span()
        for pattern in (_MARKDOWN_LINK_RE, _INLINE_CODE_RE, _URI_RE)
        for match in pattern.finditer(text)
    ]

    active_fence: tuple[str, int] | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        line_body = line.rstrip("\r\n")
        fence_match = _FENCE_RE.match(line_body)
        leading_spaces = len(line_body) - len(line_body.lstrip(" "))
        content_after_indent = line_body[leading_spaces:]
        is_indented_code = leading_spaces >= 4 or content_after_indent.startswith("\t")

        if active_fence is not None:
            spans.append((offset, offset + len(line)))
            if fence_match:
                fence = fence_match.group("fence")
                is_closing_fence = not line_body[fence_match.end() :].strip()
                if (
                    fence[0] == active_fence[0]
                    and len(fence) >= active_fence[1]
                    and is_closing_fence
                ):
                    active_fence = None
        elif fence_match:
            fence = fence_match.group("fence")
            active_fence = (fence[0], len(fence))
            spans.append((offset, offset + len(line)))
        elif is_indented_code:
            spans.append((offset, offset + len(line)))

        offset += len(line)

    spans.extend(
        match.span()
        for match in _REFERENCE_TOKEN_RE.finditer(text)
        if _token_is_reference(match.group(0))
    )
    return _merge_spans(spans)


def find_unprotected_word_spans(text: str, word: str) -> list[tuple[int, int]]:
    """查找可安全用于旧版宽松情绪匹配的完整单词位置。"""

    if not text or not word:
        return []

    protected_spans = _protected_reference_spans(text)
    protected_index = 0
    matches: list[tuple[int, int]] = []
    pattern = re.compile(r"\b(" + re.escape(word) + r")\b")

    for match in pattern.finditer(text):
        start, end = match.span(1)
        while (
            protected_index < len(protected_spans)
            and protected_spans[protected_index][1] <= start
        ):
            protected_index += 1
        if protected_index < len(protected_spans):
            protected_start, protected_end = protected_spans[protected_index]
            if protected_start <= start and end <= protected_end:
                continue
        matches.append((start, end))

    return matches
