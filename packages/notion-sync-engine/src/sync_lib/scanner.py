# sync_lib/scanner.py
from pathlib import Path
from dataclasses import dataclass, field
import pathspec
from .canonical import detect_format, compute_hash, validate_path, canonical


@dataclass
class LocalSnapshot:
    files: dict = field(default_factory=dict)  # path → {hash, format}
    skipped: list = field(default_factory=list)  # [{path, reason, ...}]

    def __contains__(self, path):
        return path in self.files

    def __getitem__(self, path):
        return self.files[path]

    def keys(self):
        return self.files.keys()


def scan_local(root: Path, config: dict) -> LocalSnapshot:
    """扫描本地文件"""
    root = Path(root)
    snapshot = LocalSnapshot()

    # 编译 pathspec 规则
    include_spec = pathspec.PathSpec.from_lines("gitwildmatch", config["include"])
    exclude_spec = pathspec.PathSpec.from_lines("gitwildmatch", config["exclude"])

    # 内置排除
    builtin_exclude = pathspec.PathSpec.from_lines("gitwildmatch", [
        ".sync-*",
        "*.sync-tmp",
        "__pycache__/",
    ])

    # 遍历文件
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue

        # 相对路径（POSIX 格式）
        rel_path = file_path.relative_to(root).as_posix()

        # 检查内置排除
        if builtin_exclude.match_file(rel_path):
            continue

        # 检查用户排除
        if exclude_spec.match_file(rel_path):
            continue

        # 检查 include
        if not include_spec.match_file(rel_path):
            continue

        # 路径安全校验
        if not validate_path(rel_path):
            snapshot.skipped.append({"path": rel_path, "reason": "invalid_path"})
            continue

        # 读取文件
        try:
            raw = file_path.read_bytes()
        except Exception as e:
            snapshot.skipped.append({"path": rel_path, "reason": f"read_error: {e}"})
            continue

        # 检查大小
        text = raw.decode("utf-8-sig", errors="ignore")
        canonical_text = canonical(text)
        char_count = len(canonical_text.encode("utf-16-le")) // 2  # UTF-16 码元数

        if char_count > config.get("max_chars", 150000):
            snapshot.skipped.append({
                "path": rel_path,
                "reason": "max_chars_exceeded",
                "chars": char_count,
            })
            continue

        # 计算 hash 和格式
        file_hash = compute_hash(text)
        fmt = detect_format(raw)

        snapshot.files[rel_path] = {
            "hash": file_hash,
            "format": fmt,
            "size_kb": len(raw) / 1024,
        }

    return snapshot
