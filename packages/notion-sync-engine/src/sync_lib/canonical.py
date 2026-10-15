import hashlib
import unicodedata
from pathlib import PurePosixPath


def canonical(text: str) -> str:
    """规范化文本：统一换行符，去掉末尾空行"""
    # CRLF → LF
    text = text.replace("\r\n", "\n")
    # 单独的 CR → LF
    text = text.replace("\r", "\n")
    # 去掉末尾连续换行
    return text.rstrip("\n")


def compute_hash(text: str) -> str:
    """计算规范化文本的 SHA-256"""
    return hashlib.sha256(canonical(text).encode("utf-8")).hexdigest()


def detect_format(raw: bytes) -> dict:
    """探测文件格式"""
    # BOM
    bom = raw.startswith(b"\xef\xbb\xbf")
    if bom:
        raw = raw[3:]

    # 解码
    text = raw.decode("utf-8-sig")

    # EOL 统计
    crlf_count = raw.count(b"\r\n")
    lf_count = raw.count(b"\n") - crlf_count  # 减去 CRLF 中的 LF
    eol = "crlf" if crlf_count > lf_count else "lf"

    # 末尾换行
    final_newline = text.endswith("\n")

    return {
        "encoding": "utf-8",
        "bom": bom,
        "eol": eol,
        "final_newline": final_newline,
    }


def restore_format(text: str, fmt: dict) -> bytes:
    """按格式还原字节"""
    # EOL 还原
    if fmt["eol"] == "crlf":
        text = text.replace("\n", "\r\n")

    # 末尾换行
    if fmt["final_newline"] and not text.endswith(("\n", "\r\n")):
        text += "\r\n" if fmt["eol"] == "crlf" else "\n"

    # 编码
    raw = text.encode("utf-8")

    # BOM
    if fmt["bom"]:
        raw = b"\xef\xbb\xbf" + raw

    return raw


def split_rich_text(text: str, max_units: int = 2000) -> list[str]:
    """按 UTF-16 码元切分，不切碎代理对"""
    if not text:
        return [""]

    chunks = []
    current = []
    current_units = 0

    for char in text:
        # 计算当前字符的 UTF-16 码元数
        char_units = 2 if ord(char) > 0xFFFF else 1

        # 如果加入这个字符会超限，先保存当前 chunk
        if current_units + char_units > max_units:
            chunks.append("".join(current))
            current = [char]
            current_units = char_units
        else:
            current.append(char)
            current_units += char_units

    # 保存最后一个 chunk
    if current:
        chunks.append("".join(current))

    return chunks


def normalize_path(path: str) -> str:
    """把路径统一为 NFC 规范化的 POSIX 形式。

    - 反斜杠分隔符统一为正斜杠（Windows → POSIX）
    - NFC 规范化，避免 NFC/NFD 等价文件名映射到不同 state key
    - 合并重复分隔符、去除 `.` 段（PurePosixPath 语义）
    """
    path = path.replace("\\", "/")
    path = unicodedata.normalize("NFC", path)
    return str(PurePosixPath(path))


def validate_path(path: str) -> bool:
    """校验路径安全性"""
    # 统一为 NFC 规范化的 POSIX 形式（拒绝反斜杠穿越等绕过手法）
    path = normalize_path(path)

    # 拒绝绝对路径
    if path.startswith("/") or (len(path) >= 2 and path[1] == ":"):
        return False

    # 拒绝目录穿越
    if ".." in path.split("/"):
        return False

    # 检查每个路径段
    for part in path.split("/"):
        # 拒绝 Windows 保留名
        reserved = {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4",
                   "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2",
                   "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"}
        name_without_ext = part.split(".")[0].upper()
        if name_without_ext in reserved:
            return False

        # 拒绝结尾是空格或点（含扩展名前的主名）
        if part.endswith((" ", ".")) or part.split(".")[0].endswith((" ", ".")):
            return False

    return True
