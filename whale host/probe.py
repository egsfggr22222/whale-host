# -*- coding: utf-8 -*-
"""probe.py —— 目录探测：先给一张「地图」，再决定读哪个文件。

为什么需要它
------------
直接列目录、逐个读文件，很容易一头撞进几十 MB 的日志或模型文件，
白白烧掉上下文。这个工具只做一件事：**把一个目录的结构和体量摸清楚**，
返回树形清单，每个文件带大小，每个文件夹带「它下面所有文件的总大小」。

有了地图再挑文件读，就不会误读大块头。

输出里带三个信号，方便快速判断：
  · 每个条目的体积
  · 大文件标记（超过 big_mb 的单独标出来）
  · 被折叠的目录（深度超过 max_depth 的，只给汇总，不展开）
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import pytools_client as pytools
from pytools_client import register

pytools.describe(
    "目录探测：返回树形结构 + 每个文件/文件夹的大小，用来精确定位有价值的文件、避开大文件。",
    tags=["filesystem", "probe", "tree", "size"],
)

# 扫描预算：防止在超大目录上把时间耗光
MAX_SCAN_ENTRIES = 50_000
MAX_LIST_ENTRIES = 500


def _human(size: int) -> str:
    """把字节数变成好读的单位。"""
    if size < 1024:
        return "%dB" % size
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024.0
        if value < 1024 or unit == "TB":
            return "%.1f%s" % (value, unit)
    return "%.1fTB" % value


def _scan(path: str, budget: List[int]) -> Tuple[int, int, List[Tuple[str, int]]]:
    """递归算一个目录的总大小、文件数、以及其中的大文件。

    budget 是一个单元素列表（可变），用来跨递归共享扫描配额。
    返回 (总字节, 文件数, [(相对路径, 字节), ...]) —— 这里只收体积，不收结构。
    """
    total = 0
    files = 0
    biggest: List[Tuple[str, int]] = []
    try:
        entries = list(os.scandir(path))
    except OSError:
        return 0, 0, []

    for entry in entries:
        if budget[0] <= 0:
            break
        budget[0] -= 1
        try:
            if entry.is_dir(follow_symlinks=False):
                sub_total, sub_files, sub_big = _scan(entry.path, budget)
                total += sub_total
                files += sub_files
                biggest.extend(sub_big)
            elif entry.is_file(follow_symlinks=False):
                size = entry.stat(follow_symlinks=False).st_size
                total += size
                files += 1
                biggest.append((entry.name, size))
        except OSError:
            continue
    return total, files, biggest


def _build(path: str, depth: int, max_depth: int, budget: List[int],
           counter: List[int], big_bytes: int) -> Tuple[int, int, List[Dict[str, Any]]]:
    """建树。返回 (总字节, 文件数, 子项列表)。"""
    total = 0
    files = 0
    items: List[Dict[str, Any]] = []

    try:
        entries = sorted(os.scandir(path), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
    except OSError:
        return 0, 0, []

    for entry in entries:
        if budget[0] <= 0:
            break
        budget[0] -= 1
        counter[0] += 1
        if counter[0] > MAX_LIST_ENTRIES:
            break

        try:
            if entry.is_dir(follow_symlinks=False):
                if depth < max_depth:
                    sub_total, sub_files, sub_items = _build(
                        entry.path, depth + 1, max_depth, budget, counter, big_bytes)
                else:
                    # 深度到顶：只给汇总，不展开
                    sub_total, sub_files, _ = _scan(entry.path, budget)
                    sub_items = []
                total += sub_total
                files += sub_files
                items.append({
                    "name": entry.name,
                    "type": "dir",
                    "size": sub_total,
                    "size_human": _human(sub_total),
                    "files": sub_files,
                    "children": sub_items,
                    "collapsed": depth >= max_depth,
                })
            elif entry.is_file(follow_symlinks=False):
                size = entry.stat(follow_symlinks=False).st_size
                total += size
                files += 1
                items.append({
                    "name": entry.name,
                    "type": "file",
                    "size": size,
                    "size_human": _human(size),
                    "big": size >= big_bytes,
                })
        except OSError:
            continue

    # 大的排前面，方便一眼看到体量分布
    items.sort(key=lambda item: (-item["size"], item["name"].lower()))
    return total, files, items


def _render(items: List[Dict[str, Any]], prefix: str = "", depth: int = 0) -> List[str]:
    """把树画成等宽文本。"""
    lines: List[str] = []
    for index, item in enumerate(items):
        last = index == len(items) - 1
        branch = "└─ " if last else "├─ "
        name = item["name"] + ("/" if item["type"] == "dir" else "")
        marks = []
        if item.get("collapsed"):
            marks.append("未展开")
        if item.get("big"):
            marks.append("大文件")
        tail = ("  ← " + " · ".join(marks)) if marks else ""
        lines.append("%s%s%s  %s%s" % (prefix, branch, name, item["size_human"], tail))
        if item.get("children"):
            lines.extend(_render(item["children"], prefix + ("   " if last else "│  "), depth + 1))
    return lines


@register(description="探测目录：返回树形结构，每个文件带大小，每个文件夹带其下所有文件的总大小。", timeout=120)
def probe_tree(path: str = ".", max_depth: int = 3, big_mb: float = 5.0,
               auto_scan: bool = True) -> Dict[str, Any]:
    """给一个目录拍一张「体量地图」。

    Args:
        path: 要探测的目录（相对路径按当前工作目录解析）。
        max_depth: 展开到第几层，默认 3。更深的只给汇总，不展开。
        big_mb: 超过多少 MB 标记为大文件，默认 5。
        auto_scan: 是否额外返回「整个目录下最大的几个文件」，默认开启。
    """
    target = os.path.abspath(os.path.expanduser(str(path or ".")))
    if not os.path.isdir(target):
        return {"ok": False, "error": "不是目录或不存在：%s" % target}

    depth = max(1, min(12, int(max_depth)))
    big_bytes = int(max(0.0, float(big_mb)) * 1024 * 1024)
    budget = [MAX_SCAN_ENTRIES]
    counter = [0]

    total, files, items = _build(target, 1, depth, budget, counter, big_bytes)
    lines = _render(items)

    result: Dict[str, Any] = {
        "ok": True,
        "path": target,
        "total_size": total,
        "total_human": _human(total),
        "file_count": files,
        "listed": counter[0],
        "truncated": budget[0] <= 0,
        "tree": "\n".join(lines) if lines else "(空目录)",
        "entries": items,
    }

    if auto_scan:
        scan_budget = [MAX_SCAN_ENTRIES]
        _, _, biggest = _scan(target, scan_budget)
        biggest.sort(key=lambda pair: -pair[1])
        result["biggest_files"] = [
            {"name": name, "size": size, "size_human": _human(size)}
            for name, size in biggest[:10]
        ]

    result["hint"] = (
        "先看 total_human 和 biggest_files 判断值不值得深入；"
        "要读某个文件前，确认它的体积，避开大文件。"
    )
    return result


if __name__ == "__main__":
    from pytools_client import main

    raise SystemExit(main())
