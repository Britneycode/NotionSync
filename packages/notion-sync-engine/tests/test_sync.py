"""Tests for the Notion sync canonical module (text normalization, format
detection/restoration, rich_text splitting and path validation)."""

from sync_lib.canonical import (
    canonical,
    detect_format,
    restore_format,
    split_rich_text,
    validate_path,
)


def test_canonical_crlf_to_lf():
    assert canonical("line1\r\nline2") == "line1\nline2"


def test_canonical_cr_to_lf():
    assert canonical("line1\rline2") == "line1\nline2"


def test_canonical_mixed_eol():
    assert canonical("line1\r\nline2\rline3\n") == "line1\nline2\nline3"


def test_canonical_strip_trailing_newlines():
    assert canonical("line1\n\n\n") == "line1"


def test_canonical_preserve_other_whitespace():
    assert canonical("line1\t  \nline2") == "line1\t  \nline2"


def test_canonical_empty():
    assert canonical("") == ""


def test_detect_format_utf8_no_bom_crlf():
    raw = b"line1\r\nline2\r\n"
    fmt = detect_format(raw)
    assert fmt == {"encoding": "utf-8", "bom": False, "eol": "crlf", "final_newline": True}


def test_detect_format_utf8_bom_lf():
    raw = b"\xef\xbb\xbfline1\nline2"
    fmt = detect_format(raw)
    assert fmt == {"encoding": "utf-8", "bom": True, "eol": "lf", "final_newline": False}


def test_detect_format_mixed_eol():
    raw = b"line1\r\nline2\nline3\r\n"
    fmt = detect_format(raw)
    assert fmt["eol"] == "crlf"  # 多数决


def test_restore_format_crlf():
    text = "line1\nline2"
    fmt = {"encoding": "utf-8", "bom": False, "eol": "crlf", "final_newline": True}
    assert restore_format(text, fmt) == b"line1\r\nline2\r\n"


def test_restore_format_bom():
    text = "line1\nline2"
    fmt = {"encoding": "utf-8", "bom": True, "eol": "lf", "final_newline": False}
    assert restore_format(text, fmt) == b"\xef\xbb\xbfline1\nline2"


def test_split_rich_text_exact_2000():
    text = "a" * 2000
    chunks = split_rich_text(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_split_rich_text_2001():
    text = "a" * 2001
    chunks = split_rich_text(text)
    assert len(chunks) == 2
    assert len(chunks[0]) == 2000
    assert len(chunks[1]) == 1


def test_split_rich_text_emoji_boundary():
    # emoji 是代理对（2 个 UTF-16 码元）
    text = "a" * 1999 + "😀" + "b" * 100
    chunks = split_rich_text(text)
    assert len(chunks) == 2
    assert chunks[0].endswith("😀") or chunks[1].startswith("😀")  # 不能切碎
    assert "".join(chunks) == text


def test_split_rich_text_chinese():
    # 中文是 1 个 UTF-16 码元
    text = "汉" * 2001
    chunks = split_rich_text(text)
    assert len(chunks) == 2
    assert "".join(chunks) == text


def test_split_rich_text_empty():
    assert split_rich_text("") == [""]


def test_validate_path_normal():
    assert validate_path("src/main.py") is True


def test_validate_path_nested():
    assert validate_path("docs/reports/final.md") is True


def test_validate_path_reject_parent():
    assert validate_path("../etc/passwd") is False


def test_validate_path_reject_absolute():
    assert validate_path("/etc/passwd") is False
    assert validate_path("C:\\Windows\\System32") is False


def test_validate_path_reject_windows_reserved():
    assert validate_path("CON") is False
    assert validate_path("aux.txt") is False
    assert validate_path("COM1") is False
    assert validate_path("LPT9.md") is False


def test_validate_path_reject_trailing_space():
    assert validate_path("file .txt") is False
    assert validate_path("dir /file.txt") is False


def test_validate_path_reject_trailing_dot():
    assert validate_path("file.txt.") is False
    assert validate_path("dir./file.txt") is False
