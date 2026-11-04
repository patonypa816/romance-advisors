"""Validate the bounded SSML subset accepted by Studio, without service calls.

Provider contract: https://help.aliyun.com/zh/model-studio/ssml-latex-user-guide
The display script is authoritative; XML may annotate it, never replace it.
"""

from __future__ import annotations

import re
import io
import xml.etree.ElementTree as ET

SSML_VERSION = 1
MAX_SSML_BYTES = 8 * 1024
SAY_AS_TYPES = frozenset({
    "cardinal", "digits", "telephone", "name", "address", "id",
    "characters", "punctuation", "date", "time", "currency", "measure",
})
_BRACKETS = "[]［］"
_DECIMAL = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\Z")


class SpeechMarkupError(ValueError):
    """A fixed local error code, never an echo of untrusted XML."""


def _reject(reason: str) -> None:
    raise SpeechMarkupError(reason)


def _plain(value: str) -> bool:
    return not any(char in value for char in "<>" + _BRACKETS) and not any(
        ord(char) < 32 and char not in "\t\n\r" for char in value
    )


def parse_speech_markup(text: str, ssml: str | None = None) -> dict:
    """Return display, synthesis and known spoken text, or raise ValueError.

Ranges use Python character offsets into the original display script and have
an exclusive end. Say-as expansion depends on the provider and language, so its
spoken text is deliberately unknown until verified provider words are available.
No body-length policy lives here: the caller still owns its 30/120-char limit.
"""
    if not isinstance(text, str) or not text.strip():
        _reject("invalid_text")
    result = {"display_text": text, "tts_input": text, "spoken_text": text,
              "rewritten_ranges": [], "ssml_version": SSML_VERSION,
              "tts_parameters": {}}
    if ssml is None:
        if not _plain(text):
            _reject("invalid_plain_text")
        return result
    if not isinstance(ssml, str) or not ssml:
        _reject("invalid_ssml")
    try:
        size = len(ssml.encode("utf-8"))
    except UnicodeError:
        _reject("invalid_ssml")
    if size > MAX_SSML_BYTES:
        _reject("ssml_too_large")
    # Reject declarations, DTD/entity definitions, comments, CDATA and processing
    # instructions before parsing. Only XML's predefined/numeric entities remain.
    if "<!" in ssml or "<?" in ssml or any(char in ssml for char in _BRACKETS):
        _reject("unsupported_ssml_syntax")
    try:
        # Namespace declarations disappear from Element.attrib. Observe their
        # parser events explicitly so xmlns cannot bypass the attribute allowlist.
        parser = ET.iterparse(io.StringIO("<studio-ssml>" + ssml + "</studio-ssml>"),
                              events=("start-ns", "end"))
        for event, _ in parser:
            if event == "start-ns":
                _reject("unsupported_ssml_namespace")
        root = parser.root
    except (ET.ParseError, ValueError):
        _reject("invalid_ssml")
    if not len(root) or (root.text or "").strip():
        _reject("speak_required")
    literal: list[str] = []
    spoken: list[str] = []
    cursor = 0
    unknown_spoken = False
    common_parameters: dict | None = None

    def append(value: str) -> None:
        nonlocal cursor
        if any(char in value for char in _BRACKETS):
            _reject("unsupported_ssml_syntax")
        literal.append(value)
        spoken.append(value)
        cursor += len(value)

    for speak in root:
        if speak.tag != "speak" or (speak.tail or "").strip():
            _reject("speak_required")
        if set(speak.attrib) - {"rate", "pitch", "volume"}:
            _reject("unsupported_speak_attribute")
        effective_parameters = {"rate": 1.0, "pitch": 1.0, "volume": 50}
        for name, value in speak.attrib.items():
            if name == "volume":
                valid = re.fullmatch(r"[0-9]{1,3}", value) and 0 <= int(value) <= 100
            else:
                valid = _DECIMAL.fullmatch(value) and 0.5 <= float(value) <= 2
            if not valid:
                _reject("invalid_speak_attribute")
            effective_parameters[name] = int(value) if name == "volume" else float(value)
        # Qwen 3.1's HTTP path was observed to ignore speak-level controls while
        # honoring input-level controls. Promote only uniform segment settings;
        # applying one speak's values to a different speak would change intent.
        if common_parameters is not None and effective_parameters != common_parameters:
            _reject("mixed_speak_parameters_unsupported")
        common_parameters = effective_parameters
        result["tts_parameters"].update({name: effective_parameters[name] for name in speak.attrib})
        append(speak.text or "")
        for child in speak:
            content = child.text or ""
            if len(child):
                _reject("nested_ssml_tag")
            if child.tag == "break":
                if set(child.attrib) - {"time"} or content:
                    _reject("invalid_break")
                time = child.get("time")
                if time is not None:
                    match = re.fullmatch(r"([0-9]{1,5})(ms|s)", time)
                    if not match or not (50 <= int(match[1]) <= 10000 if match[2] == "ms"
                                         else 1 <= int(match[1]) <= 10):
                        _reject("invalid_break")
            elif child.tag in {"sub", "phoneme", "say-as"}:
                if not content.strip():
                    _reject("empty_ssml_content")
                start = cursor
                append(content)
                if child.tag == "sub":
                    alias = child.get("alias", "")
                    if set(child.attrib) != {"alias"} or not alias.strip() or not _plain(alias):
                        _reject("invalid_sub")
                    spoken[-1] = alias
                elif child.tag == "phoneme":
                    alphabet, ph = child.get("alphabet"), child.get("ph", "")
                    if (set(child.attrib) != {"alphabet", "ph"} or alphabet not in {"py", "cmu"}
                            or not ph.strip() or not _plain(ph)):
                        _reject("invalid_phoneme")
                    units = ph.split()
                    if alphabet == "py":
                        if (len(units) != len(content) or not all(
                                re.fullmatch(r"[A-Za-züÜvV:]+[1-5]", unit) for unit in units)):
                            _reject("invalid_phoneme")
                    elif not all(re.fullmatch(r"[A-Z]+[012]?", unit) for unit in units):
                        _reject("invalid_phoneme")
                else:
                    if (set(child.attrib) != {"interpret-as"}
                            or child.get("interpret-as") not in SAY_AS_TYPES):
                        _reject("invalid_say_as")
                    unknown_spoken = True
                if child.tag in {"sub", "say-as", "phoneme"}:
                    result["rewritten_ranges"].append({"start": start, "end": cursor})
            else:
                _reject("unsupported_ssml_tag")
            append(child.tail or "")
    if "".join(literal) != text:
        _reject("ssml_text_mismatch")
    result["tts_input"] = ssml
    result["spoken_text"] = None if unknown_spoken else "".join(spoken)
    return result
