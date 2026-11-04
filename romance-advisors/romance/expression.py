"""Small content contract; the avatar bridge still validates execution/timing."""
from __future__ import annotations

import unicodedata


REACTION_TTS_STYLE = "像熟人随口聊天，松弛自然，避免播音腔；只朗读台词，不念演绎要求。"
PARTS = {"head", "face", "left_arm", "right_arm", "both_arms", "torso"}


class ExpressionValidationError(ValueError):
    """Keep legacy error strings while exposing bounded, content-free reasons."""

    def __init__(self, outer, field, code, details=None):
        super().__init__(outer)
        self.field, self.code = field, code
        self.details = dict(details or {})

    def diagnostic(self):
        return {"field": self.field, "code": self.code, "details": dict(self.details)}


def _fail(field, code, **details):
    raise ExpressionValidationError("invalid_reaction_expression", field, code, details)


def _type(value):
    # Never put model text, arbitrary extra keys or a provider body in diagnostics.
    kind = type(value).__name__
    return kind if kind in {"str", "dict", "list", "bool", "int", "float", "NoneType"} else "other"


def _object(value, required, field):
    if not isinstance(value, dict):
        _fail(field or "result", "expected_object", actual_type=_type(value))
    for key in required:
        if key not in value:
            _fail(f"{field}.{key}" if field else key, "missing_field")
    # Unknown fields are ignored at the boundary; they never reach Studio.
    return value


def _text(value, limit, field):
    if not isinstance(value, str):
        _fail(field, "expected_string", actual_type=_type(value))
    value = value.strip()
    if not value:
        _fail(field, "empty_string")
    if len(value) > limit:
        _fail(field, "text_too_long", length=len(value), max_length=limit)
    return value


def _ge(value, field):
    value = _object(value, ("gaze", "expression"), field)
    return {key: _text(value[key], 40, f"{field}.{key}") for key in ("gaze", "expression")}


def _bridge_occurrence(text, quote, occurrence, field):
    """Translate the model's literal occurrence to Studio's existing numbering.

    Studio counts matches after removing punctuation. Keep that wire contract,
    but select the model's intended continuous original-text match first. This
    runs only on fresh model output, never on a stored/frozen Studio plan.
    """
    if any(c.isspace() or unicodedata.category(c)[0] in {"P", "Z"} for c in quote):
        _fail(f"{field}.quote", "anchor_contains_separator")
    if type(occurrence) is not int or occurrence < 1:
        _fail(f"{field}.occurrence", "expected_positive_integer", actual_type=_type(occurrence))
    literal = [i for i in range(len(text)) if text.startswith(quote, i)]
    if not literal:
        _fail(f"{field}.quote", "anchor_not_found")
    if occurrence > len(literal):
        _fail(f"{field}.occurrence", "anchor_occurrence_out_of_range", occurrence=occurrence, matches=len(literal))
    positions = [i for i, c in enumerate(text)
                 if not c.isspace() and unicodedata.category(c)[0] not in {"P", "Z"}]
    normalized = "".join(text[i] for i in positions)
    matches = [i for i in range(len(normalized)) if normalized.startswith(quote, i)]
    target = positions.index(literal[occurrence - 1])
    return matches.index(target) + 1


def build_plan(text, instruction, expression, *, ssml=None):
    """Freeze a fresh model segment without importing Studio's runtime."""
    text, instruction = _text(text, 120, "text"), _text(instruction, 160, "instruction")
    # Studio owns XML/tag and spoken/display consistency validation. Preserve
    # exact markup here so normalization cannot change its speech controls.
    markup = {}
    if ssml is not None:
        if not isinstance(ssml, str):
            _fail("ssml", "expected_string", actual_type=_type(ssml))
        if not ssml.strip():
            _fail("ssml", "empty_string")
        try:
            size = len(ssml.encode("utf-8"))
        except UnicodeError:
            _fail("ssml", "invalid_utf8")
        if size > 8192:
            _fail("ssml", "ssml_too_large", utf8_bytes=size, max_bytes=8192)
        markup["ssml"] = ssml
    expression = _object(expression, ("emotion", "actions", "recovery"), "expression")
    emotion = _text(expression["emotion"], 24, "expression.emotion")
    actions = expression["actions"]
    if not isinstance(actions, list):
        _fail("expression.actions", "expected_array", actual_type=_type(actions))
    if len(actions) > 1:
        _fail("expression.actions", "too_many_items", count=len(actions), max_items=1)
    checked = []
    for index, group in enumerate(actions):
        field = f"expression.actions[{index}]"
        group = _object(group, ("anchor", "motions", "gaze", "expression"), field)
        anchor = _object(group["anchor"], ("quote", "occurrence"), f"{field}.anchor")
        quote = _text(anchor["quote"], 120, f"{field}.anchor.quote")
        occurrence = _bridge_occurrence(text, quote, anchor["occurrence"], f"{field}.anchor")
        motions = group["motions"]
        if not isinstance(motions, list):
            _fail(f"{field}.motions", "expected_array", actual_type=_type(motions))
        if not 1 <= len(motions) <= 2:
            _fail(f"{field}.motions", "item_count_out_of_range", count=len(motions), min_items=1, max_items=2)
        clean_motions, occupied = [], set()
        for number, motion in enumerate(motions):
            mfield = f"{field}.motions[{number}]"
            motion = _object(motion, ("part", "description"), mfield)
            part = motion["part"]
            if not isinstance(part, str):
                _fail(f"{mfield}.part", "expected_string", actual_type=_type(part))
            part = part.strip()
            if part not in PARTS:
                _fail(f"{mfield}.part", "unsupported_part")
            resources = {"left_arm", "right_arm"} if part == "both_arms" else {part}
            if occupied & resources:
                _fail(f"{mfield}.part", "resource_conflict")
            occupied.update(resources)
            clean_motions.append({"part": part, "description": _text(motion["description"], 70, f"{mfield}.description")})
        ge = _ge(group, field)
        # Per-field bounds already cap this structure. Studio uses the same
        # bounds and compiles all words; there is no second arbitrary total cap.
        checked.append({"anchor": {"quote": quote, "occurrence": occurrence}, "motions": clean_motions, **ge})
    recovery = expression["recovery"]
    if checked:
        recovery = _ge(recovery, "expression.recovery")
    elif recovery is not None:
        _fail("expression.recovery", "recovery_without_action")
    return {"emotion": emotion, "reading": None,
            "segments": [{"text": text, "instruction": instruction, **markup,
                          "actions": checked, "recovery": recovery}]}


def parse_ambient_result(result, allowed_comment_ids):
    """Read known fields only; silence and speech keep their existing meaning."""
    try:
        result = _object(result, ("speak",), "")
        if type(result["speak"]) is not bool:
            _fail("speak", "expected_boolean", actual_type=_type(result["speak"]))
        if result["speak"] is False:
            return None
        _object(result, ("text", "instruction", "expression", "intent", "comment_ids"), "")
        text = _text(result["text"], 60, "text")
        cue = _text(result["instruction"], 100, "instruction")
        intent = _text(result["intent"], 24, "intent")
        ids = result["comment_ids"]
        if not isinstance(ids, list):
            _fail("comment_ids", "expected_array", actual_type=_type(ids))
        checked_ids = []
        for index, comment_id in enumerate(ids):
            field = f"comment_ids[{index}]"
            if not isinstance(comment_id, str):
                _fail(field, "expected_string", actual_type=_type(comment_id))
            comment_id = comment_id.strip()
            if not comment_id:
                _fail(field, "empty_string")
            if comment_id not in allowed_comment_ids:
                _fail(field, "unknown_comment_id")
            if comment_id in checked_ids:
                _fail(field, "duplicate_comment_id")
            checked_ids.append(comment_id)
        return {"plan": build_plan(text, REACTION_TTS_STYLE + cue, result["expression"], ssml=result.get("ssml")),
                "intent": intent, "comment_ids": checked_ids}
    except ExpressionValidationError as error:
        raise ExpressionValidationError("invalid_ambient_result", error.field, error.code, error.details) from None
