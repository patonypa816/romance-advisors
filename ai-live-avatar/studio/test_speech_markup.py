"""Offline SSML validation: XML never owns the visible script or text budget."""

import unittest

from speech_markup import MAX_SSML_BYTES, SAY_AS_TYPES, SpeechMarkupError, parse_speech_markup


class SpeechMarkupTests(unittest.TestCase):
    def test_plain_text_keeps_every_character_and_has_no_xml_budget(self):
        text = " 你好。\n"
        parsed = parse_speech_markup(text)
        self.assertEqual(parsed, {"display_text": text, "tts_input": text,
                                  "spoken_text": text, "rewritten_ranges": [], "ssml_version": 1,
                                  "tts_parameters": {}})
        self.assertEqual(parse_speech_markup("字" * 121)["display_text"], "字" * 121)

    def test_pause_and_parallel_speaks_preserve_literal_body(self):
        xml = '\n<speak rate="0.5" pitch="2.0" volume="0">你好<break time="50ms"/>。</speak>\n<speak rate="0.5" pitch="2" volume="0">再见<break/>。</speak>\n'
        parsed = parse_speech_markup("你好。再见。", xml)
        self.assertEqual(parsed["tts_input"], xml)
        self.assertEqual(parsed["spoken_text"], "你好。再见。")
        self.assertEqual(parsed["rewritten_ranges"], [])
        self.assertEqual(parsed["tts_parameters"], {"rate": 0.5, "pitch": 2.0, "volume": 0})

    def test_speak_parameters_use_effective_defaults_and_only_promote_explicit_values(self):
        for xml, expected in (
            ('<speak>你好。</speak>', {}),
            ('<speak rate=".88" pitch="1.03" volume="50">你好。</speak>',
             {"rate": .88, "pitch": 1.03, "volume": 50}),
            ('<speak rate="1">你</speak><speak pitch="1.0" volume="50">好。</speak>',
             {"rate": 1.0, "pitch": 1.0, "volume": 50}),
            ('<speak rate="1">你</speak><speak>好。</speak>', {"rate": 1.0}),
        ):
            with self.subTest(xml=xml):
                self.assertEqual(parse_speech_markup("你好。", xml)["tts_parameters"], expected)
        for xml in (
            '<speak rate=".88">你</speak><speak>好。</speak>',
            '<speak pitch="1.03">你</speak><speak pitch="1.04">好。</speak>',
            '<speak volume="0">你</speak><speak volume="100">好。</speak>',
        ):
            with self.subTest(xml=xml), self.assertRaisesRegex(
                    SpeechMarkupError, "^mixed_speak_parameters_unsupported$"):
                parse_speech_markup("你好。", xml)

    def test_sub_ranges_use_original_unicode_character_offsets(self):
        xml = '<speak>😀<sub alias="世界卫生组织">WHO</sub>和<sub alias="网络标准">W3C</sub>。</speak>'
        parsed = parse_speech_markup("😀WHO和W3C。", xml)
        self.assertEqual(parsed["spoken_text"], "😀世界卫生组织和网络标准。")
        self.assertEqual(parsed["rewritten_ranges"], [{"start": 1, "end": 4}, {"start": 5, "end": 8}])

    def test_predefined_and_numeric_entities_are_literal_text(self):
        parsed = parse_speech_markup('A&B<5"\'', '<speak>A&amp;B&lt;5&quot;&apos;</speak>')
        self.assertEqual(parsed["spoken_text"], 'A&B<5"\'')
        parsed = parse_speech_markup("你好", "<speak>&#20320;&#x597d;</speak>")
        self.assertEqual(parsed["display_text"], "你好")

    def test_phonemes_keep_spoken_intent_and_mark_the_original_range(self):
        for body in ('<phoneme alphabet="py" ph="chong2 qing4">重庆</phoneme>',
                     '<phoneme alphabet="cmu" ph="S AY N">sin</phoneme>'):
            text = "重庆" if "py" in body else "sin"
            parsed = parse_speech_markup(text, "<speak>" + body + "</speak>")
            self.assertEqual(parsed["spoken_text"], text)
            self.assertEqual(parsed["rewritten_ranges"], [{"start": 0, "end": len(text)}])

    def test_all_official_say_as_types_are_explicitly_unknown_spoken_text(self):
        self.assertEqual(len(SAY_AS_TYPES), 12)
        for kind in SAY_AS_TYPES:
            parsed = parse_speech_markup("编号123。", f'<speak>编号<say-as interpret-as="{kind}">123</say-as>。</speak>')
            self.assertIsNone(parsed["spoken_text"])
            self.assertEqual(parsed["rewritten_ranges"], [{"start": 2, "end": 5}])

    def test_invalid_markup_rejected_without_echoing_input(self):
        cases = [
            ("你好", "<speak>您好</speak>"),
            ("你好", "<speak> 你好 </speak>"),
            ("你好", "你好<speak/>"),
            ("你好", "<speak>你好</speak>外文"),
            ("你好", "<speak><speak>你好</speak></speak>"),
            ("你好", '<speak voice="other">你好</speak>'),
            ("你好", '<speak><prosody rate="1">你好</prosody></speak>'),
            ("你好", '<speak><soundEvent src="https://x"/>你好</speak>'),
            ("你好", '<speak xmlns="x">你好</speak>'),
            ("你好", '<speak xmlns="">你好</speak>'),
            ("你好", '<speak xmlns:unused="x">你好</speak>'),
            ("你好", '<speak>你好<break time="0s"/></speak>'),
            ("你好", '<speak>你好<break time="1.5s"/></speak>'),
            ("你好", '<speak>你好<break time="49ms"/></speak>'),
            ("你好", '<speak>你好<break time="10001ms"/></speak>'),
            ("你好", '<speak>你好<break time="11s"/></speak>'),
            ("你好", '<speak>你好<break strength="weak"/></speak>'),
            ("你好", '<speak>你好<break> </break></speak>'),
            ("你好", '<speak><sub alias="">你好</sub></speak>'),
            ("你好", '<speak><sub alias="[开心]你好">你好</sub></speak>'),
            ("你好", '<speak><sub alias="您好" extra="x">你好</sub></speak>'),
            ("你好", '<speak><sub alias="您"><break/>你好</sub></speak>'),
            ("你好", '<speak><phoneme alphabet="ipa" ph="ni">你好</phoneme></speak>'),
            ("你好", '<speak><phoneme alphabet="py" ph="ni3">你好</phoneme></speak>'),
            ("你好", '<speak><phoneme alphabet="py" ph="ni0 hao3">你好</phoneme></speak>'),
            ("你好", '<speak><say-as interpret-as="unknown">你好</say-as></speak>'),
            ("你好", '<!DOCTYPE speak [<!ENTITY x SYSTEM "file:///private">]><speak>你好</speak>'),
            ("你好", '<?xml version="1.0"?><speak>你好</speak>'),
            ("你好", '<speak><!-- comment -->你好</speak>'),
            ("你好", '<speak><![CDATA[你好]]></speak>'),
            ("你好", '<speak>&unknown;</speak>'),
            ("[开心]你好", '<speak>&#91;开心&#93;你好</speak>'),
        ]
        for text, xml in cases:
            with self.subTest(xml=xml), self.assertRaises(SpeechMarkupError) as raised:
                parse_speech_markup(text, xml)
            self.assertNotIn(xml, str(raised.exception))

    def test_attribute_numeric_boundaries_and_syntax(self):
        for attribute, value in (("rate", "0.49"), ("pitch", "2.01"), ("rate", "nan"),
                                 ("rate", "1e0"), ("volume", "100.0"), ("volume", "101"),
                                 ("volume", "-1"), ("volume", "9" * 5000)):
            with self.subTest(attribute=attribute, value=value[:20]), self.assertRaises(SpeechMarkupError):
                parse_speech_markup("你好", f'<speak {attribute}="{value}">你好</speak>')
        for time in ("10000ms", "10s", "1s"):
            self.assertEqual(parse_speech_markup("你好", f'<speak>你好<break time="{time}"/></speak>')["spoken_text"], "你好")

    def test_xml_byte_limit_is_independent_of_display_limit(self):
        body = '<speak><sub alias="' + "a" * MAX_SSML_BYTES + '">字</sub></speak>'
        with self.assertRaisesRegex(SpeechMarkupError, "ssml_too_large"):
            parse_speech_markup("字", body)
        self.assertEqual(parse_speech_markup("字" * 120, '<speak rate="1.1">' + "字" * 120 + '</speak>')["display_text"], "字" * 120)


if __name__ == "__main__":
    unittest.main()
