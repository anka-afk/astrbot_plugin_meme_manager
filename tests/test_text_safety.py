import subprocess
import sys
import unittest
from pathlib import Path

from backend.text_safety import (
    find_unprotected_word_spans,
    strip_internal_image_ref_lines,
)


class StripInternalImageRefLinesTests(unittest.TestCase):
    def test_removes_original_observed_reference(self):
        text = (
            "导师，公开教程没有写这个开关。\n"
            "[Image Ref 1] "
            "file:///AstrBot/data/plugin_data/meme_manager/packs/"
            "legacy-migrated/memes/confused/1739434694_1.jpg"
        )

        self.assertEqual(
            strip_internal_image_ref_lines(text),
            "导师，公开教程没有写这个开关。\n",
        )

    def test_removes_latest_bare_and_numbered_references(self):
        text = (
            "来啦来啦，导师发话莉莉哪敢怠慢喵！\n"
            " file:///AstrBot/data/plugin_data/meme_manager/packs/"
            "legacy-migrated/memes/shy/02B099.jpg\n"
            "[Image Ref 1] file:///AstrBot/data/plugin_data/"
            "meme_manager/packs/legacy-migrated/memes/1739434736_2.jpg\n"
            "[Image Ref 2] file:///AstrBot/data/plugin_data/"
            "meme_manager/packs/legacy-migrated/memes/1739434736_2.jpg"
        )

        self.assertEqual(
            strip_internal_image_ref_lines(text),
            "来啦来啦，导师发话莉莉哪敢怠慢喵！\n",
        )

    def test_preserves_crlf_while_removing_middle_reference(self):
        text = (
            "前文\r\n"
            "[Image Ref 1] file:///AstrBot/data/memes/confused/a.jpg\r\n"
            "后文\r\n"
        )

        self.assertEqual(
            strip_internal_image_ref_lines(text),
            "前文\r\n后文\r\n",
        )

    def test_removes_supported_marked_reference_forms(self):
        references = (
            "file:////AstrBot/data/a.png",
            "FILE:/AstrBot/data/a.GIF",
            "file://localhost/AstrBot/data/a.webp",
            "file:///C:/AstrBot/data/a%20b.jpeg",
            "file://C:/AstrBot/data/a.jpg",
            "file://server/share/AstrBot/data/a.jpg",
            "https://example.com/assets/a.png?size=large",
            "/AstrBot/data/a.jpg",
            r"C:\AstrBot\data\a.jpeg",
            r"\\server\share\AstrBot\data\a.webp",
            "data:image/png;base64,AA==",
            "base64://AA==",
        )

        for index, reference in enumerate(references, start=1):
            with self.subTest(reference=reference):
                text = f"[Image Ref {index}] {reference}"
                self.assertEqual(strip_internal_image_ref_lines(text), "")

    def test_removes_multiple_reference_lines(self):
        text = (
            "[Image Ref 1] file:///AstrBot/data/a.jpg\n"
            "保留正文\n"
            "[Image Ref 2]: <https://example.com/b.webp>\n"
        )

        self.assertEqual(strip_internal_image_ref_lines(text), "保留正文\n")

    def test_preserves_user_visible_or_malformed_text(self):
        examples = (
            "请打开 file:///AstrBot/data/confused/a.jpg",
            "调试输出：[Image Ref 1] file:///AstrBot/data/a.jpg",
            "> [Image Ref 1] file:///AstrBot/data/a.jpg",
            "    [Image Ref 1] file:///AstrBot/data/a.jpg",
            "\t[Image Ref 1] file:///AstrBot/data/a.jpg",
            "    file:///AstrBot/data/a.jpg",
            "\tfile:///AstrBot/data/a.jpg",
            "file:///tmp/example.jpg",
            "<file:///tmp/example.jpg>",
            "![Image Ref 1](file:///AstrBot/data/a.jpg)",
            "[Image Ref abc] file:///AstrBot/data/a.jpg",
            "[Image Ref 0] file:///AstrBot/data/a.jpg",
            "[Image Ref 1] file:///AstrBot/data/readme.txt",
            "[Image Ref 1] file:relative/a.jpg",
            "[Image Ref 1] file:///AstrBot/data/a.jpg 后面还有说明",
            "[Image Ref 1] http://[invalid/a.jpg",
            "file://[invalid/a.jpg",
            "https://example.com/a.jpg",
        )

        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(strip_internal_image_ref_lines(text), text)

    def test_preserves_reference_example_inside_fenced_code(self):
        text = (
            "```text\n"
            "[Image Ref 1] file:///AstrBot/data/a.jpg\n"
            "file:///AstrBot/data/a.jpg\n"
            "```\n"
            "正文\n"
        )

        self.assertEqual(strip_internal_image_ref_lines(text), text)

    def test_fence_with_info_is_not_treated_as_a_closing_fence(self):
        text = (
            "```text\n```not-a-close\n[Image Ref 1] file:///AstrBot/data/a.jpg\n```\n"
        )

        self.assertEqual(strip_internal_image_ref_lines(text), text)

    def test_import_has_no_astrbot_dependency(self):
        repo_root = Path(__file__).resolve().parents[1]
        code = """
import importlib.abc
import sys

class DenyAstrBot(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "astrbot" or fullname.startswith("astrbot."):
            raise RuntimeError(f"unexpected AstrBot import: {fullname}")
        return None

sys.meta_path.insert(0, DenyAstrBot())
from backend.text_safety import strip_internal_image_ref_lines
assert strip_internal_image_ref_lines(
    "[Image Ref 1] file:///AstrBot/data/a.jpg"
) == ""
"""
        subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root,
            check=True,
        )


class FindUnprotectedWordSpansTests(unittest.TestCase):
    def test_skips_path_uri_markdown_domain_and_filename_tokens(self):
        examples = (
            "file:///AstrBot/data/memes/confused/a.jpg",
            r"C:\AstrBot\data\confused\a.jpg",
            "/AstrBot/data/memes/confused/a.jpg",
            "https://example.com/memes/confused/a.jpg",
            "![confused](https://example.com/a.jpg)",
            "[meme](assets/confused/a.jpg)",
            "`/tmp/confused/a.jpg`",
            "confused.example.com",
            "confused.jpg",
        )

        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(find_unprotected_word_spans(text, "confused"), [])

    def test_skips_fenced_and_indented_code(self):
        examples = (
            "```text\nconfused\n```\n",
            "~~~\nconfused\n~~~\n",
            "    confused\n",
            "\tconfused\n",
        )

        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(find_unprotected_word_spans(text, "confused"), [])

    def test_keeps_natural_prose_available_for_loose_matching(self):
        examples = (
            "这次真的 confused！",
            "选A/B，然后，confused",
            "见https://example.com/a，然后，confused",
        )

        for text in examples:
            with self.subTest(text=text):
                start = text.index("confused")
                self.assertEqual(
                    find_unprotected_word_spans(text, "confused"),
                    [(start, start + len("confused"))],
                )

    def test_returns_multiple_natural_matches_in_source_order(self):
        text = "confused，然后还是 confused"
        first = text.index("confused")
        second = text.rindex("confused")

        self.assertEqual(
            find_unprotected_word_spans(text, "confused"),
            [
                (first, first + len("confused")),
                (second, second + len("confused")),
            ],
        )

    def test_keeps_path_match_protected_after_natural_match(self):
        text = "confused file:///AstrBot/data/memes/confused/a.jpg"
        first = text.index("confused")

        self.assertEqual(
            find_unprotected_word_spans(text, "confused"),
            [(first, first + len("confused"))],
        )

    def test_handles_many_reference_tokens_without_exposing_matches(self):
        paths = [
            f"file:///AstrBot/data/memes/confused/{index}.jpg" for index in range(500)
        ]
        text = " ".join(paths)

        self.assertEqual(find_unprotected_word_spans(text, "confused"), [])


if __name__ == "__main__":
    unittest.main()
