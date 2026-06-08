from __future__ import annotations

import unittest

from job_bot.language_utils import detect_language, localized_text, normalize_match_text


class LanguageUtilsTests(unittest.TestCase):
    def test_detect_language_prefers_russian_for_cyrillic_text(self) -> None:
        text = (
            "\u041f\u0440\u043e\u0434\u0443\u043a\u0442\u043e\u0432\u044b\u0439 "
            "\u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440, "
            "\u0443\u0434\u0430\u043b\u0435\u043d\u043d\u0430\u044f "
            "\u0440\u0430\u0431\u043e\u0442\u0430"
        )
        self.assertEqual(detect_language(text=text), "ru")

    def test_normalize_match_text_keeps_cyrillic_tokens(self) -> None:
        text = (
            "\u041f\u0440\u043e\u0434\u0443\u043a\u0442\u043e\u0432\u044b\u0439 "
            "\u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440 / "
            "\u0443\u0434\u0430\u043b\u0435\u043d\u043d\u0430\u044f "
            "\u0440\u0430\u0431\u043e\u0442\u0430"
        )
        self.assertEqual(
            normalize_match_text(text),
            (
                "\u043f\u0440\u043e\u0434\u0443\u043a\u0442\u043e\u0432\u044b\u0439 "
                "\u0434\u0438\u0437\u0430\u0439\u043d\u0435\u0440 "
                "\u0443\u0434\u0430\u043b\u0435\u043d\u043d\u0430\u044f "
                "\u0440\u0430\u0431\u043e\u0442\u0430"
            ),
        )

    def test_localized_text_returns_russian_copy(self) -> None:
        self.assertEqual(
            localized_text("ru", en="New Job Alert", ru="\u041d\u043e\u0432\u0430\u044f \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u044f"),
            "\u041d\u043e\u0432\u0430\u044f \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u044f",
        )


if __name__ == "__main__":
    unittest.main()
