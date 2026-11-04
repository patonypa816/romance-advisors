"""Strict M1 inputs; identifiers are never silently trimmed or truncated."""
from uuid import UUID


class ValidationError(ValueError):
    pass


def validate_registration(data: object) -> dict:
    fields = {"command_id", "provider", "identity_scope", "subject_id", "display_name"}
    if not isinstance(data, dict) or set(data) != fields:
        raise ValidationError("请求字段不完整或含不支持的字段。")
    for key, maximum in (("command_id", 36), ("provider", 32), ("identity_scope", 64),
                         ("subject_id", 128), ("display_name", 80)):
        value = data[key]
        if not isinstance(value, str) or not 1 <= len(value) <= maximum:
            raise ValidationError(f"{key} 长度必须为 1–{maximum} 字符。")
        if key != "display_name" and value != value.strip():
            raise ValidationError(f"{key} 不可含首尾空白。")
        if "\x00" in value:
            raise ValidationError(f"{key} 不可含空字符。")
    if not data["display_name"].strip():
        raise ValidationError("昵称不可全为空白。")
    if data["provider"] != "simulated" or data["identity_scope"] != "local-test":
        raise ValidationError("M1 仅接受 simulated / local-test 隔离测试身份。")
    try:
        command = str(UUID(data["command_id"]))
    except (ValueError, AttributeError):
        raise ValidationError("command_id 必须是 UUID。") from None
    return {**data, "command_id": command}
