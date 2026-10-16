# sync_lib/scanner.py
from dataclasses import dataclass, field
from pathlib import Path

import pathspec

from .canonical import canonical, compute_hash, detect_format, normalize_path, validate_path


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


def compile_scan_rules(config: dict):
    """编译同步范围规则：内置排除 / 用户排除 / include（按此顺序判定）。

    扫描器、计划器与桌面端监听器都要按同一套规则判「这个路径归不归同步管」，
    所以规则在这里只写一份（审查报告 B4）。
    """
    # 内置排除。`.sync.*` 单独列出：`.sync-*` 匹配不到 .sync.lock / .sync.lock.json
    # （审查报告 B4），include 放宽到 `**` 时它们会被扫描，触发自激同步。
    builtin_exclude = pathspec.PathSpec.from_lines("gitwildmatch", [
        ".sync-*",
        ".sync.*",
        "*.sync-tmp",
        "__pycache__/",
    ])
    return (
        builtin_exclude,
        pathspec.PathSpec.from_lines("gitwildmatch", config.get("exclude", [])),
        pathspec.PathSpec.from_lines("gitwildmatch", config.get("include", [])),
    )


def in_scope(rel_path: str, rules) -> bool:
    """rel_path 是否落在同步范围内（内置排除、用户排除、include 三关都过）。"""
    builtin_exclude, exclude_spec, include_spec = rules
    return not (
        builtin_exclude.match_file(rel_path)
        or exclude_spec.match_file(rel_path)
        or not include_spec.match_file(rel_path)
    )


def scan_local(root: Path, config: dict) -> LocalSnapshot:
    """扫描本地文件"""
    root = Path(root)
    snapshot = LocalSnapshot()

    builtin_exclude, exclude_spec, include_spec = compile_scan_rules(config)

    # 遍历文件
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue

        # 相对路径（POSIX 格式，NFC 规范化）
        rel_path = normalize_path(file_path.relative_to(root).as_posix())

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

        # 检查大小。块级拆分（remote.split_into_blocks）已能把一个文件的正文装进
        # 一个 page 内的多个 code block，所以这里只挡「远超拆分能容纳」的极端文件，
        # 不再用旧的 15 万（单块余量）当上限。
        text = raw.decode("utf-8-sig", errors="ignore")
        canonical_text = canonical(text)
        char_count = len(canonical_text.encode("utf-16-le")) // 2  # UTF-16 码元数

        if char_count > config.get("max_chars", 1_000_000):
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
