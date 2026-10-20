# -*- coding: utf-8 -*-
"""talk.py —— 字幕 + 语音：我说话时，屏幕上出字幕、同时念出来。

对外只有三件事：

    talk-say     传入要说的文字 → 出字幕 + 出声（说完自动清空）
    talk-stop    立刻停止并清空
    talk-status  看现在是否在说话

不发 talk-say 就不出声。

为什么字幕要单独一个常驻进程
------------------------------
Qt 的事件循环必须独占主线程（否则报 QApplication was not created in the
main() thread，随后崩溃）。而 pytools 每次调用都是「起进程、跑一次、退出」，
没有稳定的主线程可以跑 GUI。

所以这里分两层：

    · **常驻显示进程**（--serve）：独占主线程跑 QApplication，
      通过一个 JSON 行文件不断接收「要说的 text」，负责字幕节奏与朗读。
    · **每次的 pytools 调用**：把 text 写进那个文件就返回，不等它读完。

这样「我说一句」和「屏幕上一字一字地出现」就解耦了：调用瞬间返回，
显示按节奏慢慢来。
"""

import json
import os
import subprocess
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

import pytools_client as pytools
from pytools_client import register, skill

pytools.describe(
    "字幕 + 语音：把要说的话显示成屏幕字幕并念出来（本机离线音色，固定不选）。",
    tags=["subtitle", "tts", "speech", "ui"],
)

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(os.path.expanduser("~"), ".dsh", "python-tools", "state", "talk")
INBOX_FILE = os.path.join(STATE_DIR, "inbox.jsonl")
PID_FILE = os.path.join(STATE_DIR, "server.json")
LOG_FILE = os.path.join(STATE_DIR, "server.log")


# ══════════════════════════════════════════════════════════════════════
#  小工具
# ══════════════════════════════════════════════════════════════════════

def _ensure_state_dir() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    _ensure_state_dir()
    tmp = "%s.%s.tmp" % (path, uuid.uuid4().hex[:8])
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(tmp, path)


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
            capture_output=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return str(pid).encode() in (out.stdout or b"")
    except Exception:
        return False


def _detached() -> Dict[str, Any]:
    if os.name == "nt":
        return {
            "creationflags": getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        }
    return {"start_new_session": True}


def _enqueue(text: str) -> Dict[str, Any]:
    _ensure_state_dir()
    item = {"id": uuid.uuid4().hex[:10], "text": text, "at": time.time()}
    with open(INBOX_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    return item


def _drain_inbox() -> List[Dict[str, Any]]:
    """取走并清空收件箱（显示进程用）。"""
    if not os.path.isfile(INBOX_FILE):
        return []
    try:
        with open(INBOX_FILE, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        os.remove(INBOX_FILE)
    except OSError:
        return []
    items = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except ValueError:
            continue
    return items


def _server_running() -> bool:
    info = _read_json(PID_FILE)
    return _pid_alive(info.get("pid"))


def _spawn_server() -> int:
    _ensure_state_dir()
    log = open(LOG_FILE, "a", encoding="utf-8")
    child = subprocess.Popen(  # noqa: S603
        [sys.executable or "python", os.path.abspath(__file__), "--serve"],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        cwd=HERE, **_detached(),
    )
    _write_json(PID_FILE, {"pid": int(child.pid), "startedAt": time.time()})
    return int(child.pid)


# ══════════════════════════════════════════════════════════════════════
#  常驻显示进程：Qt 独占主线程
# ══════════════════════════════════════════════════════════════════════

def serve() -> int:
    # PyQt6 / winsdk / sounddevice 的路径都由 speech / subtitle 在 import 时挂好，
    # 所以必须先 import 它们，再 import PyQt6。
    sys.path.insert(0, HERE)
    import speech  # noqa: F401
    import subtitle  # noqa: F401

    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    from pacer import Pacer, TICK_MS
    from speech import Player
    from subtitle import SubtitleWindow

    app = QApplication.instance() or QApplication(sys.argv)
    window = SubtitleWindow()
    player = Player()
    pacer = Pacer(window, player)

    window.show()
    window.move_to_bottom_center()

    def pump() -> None:
        try:
            for item in _drain_inbox():
                text = str(item.get("text") or "")
                if not text.strip():
                    continue
                pacer.start_turn()
                pacer.feed(text)
                pacer.end_turn()
        except Exception as exc:
            sys.stderr.write("[talk] inbox %s\n" % exc)
            sys.stderr.flush()
        try:
            pacer.tick(time.time())
        except Exception as exc:
            sys.stderr.write("[talk] tick %s\n" % exc)
            sys.stderr.flush()

    timer = QTimer()
    timer.timeout.connect(pump)
    timer.start(TICK_MS)

    return int(app.exec())


# ══════════════════════════════════════════════════════════════════════
#  pytools 工具
# ══════════════════════════════════════════════════════════════════════

@register(description="说话：把文字显示成屏幕字幕并念出来（本机离线音色）。说完自动清空。", timeout=30)
def talk_say(text: str) -> Dict[str, Any]:
    """显示并念出一段话。

    Args:
        text: 要说的话。句末标点会停顿，逗号不停顿；<special>...</special>
              包住的内容原样显示、其中的标点不触发停顿（适合文件名、代码）。
    """
    text = str(text or "")
    if not text.strip():
        return {"ok": False, "error": "text 不能为空"}

    started = False
    if not _server_running():
        _spawn_server()
        started = True
        # 等显示进程把窗口建好，避免第一句丢
        deadline = time.time() + 12
        while time.time() < deadline:
            if _server_running():
                break
            time.sleep(0.2)
        time.sleep(1.2)

    item = _enqueue(text)
    return {
        "ok": True,
        "id": item["id"],
        "started": started,
        "chars": len(text),
        "hint": "字幕按节奏逐字显示，语音同步朗读；说完自动清空。",
    }


@register(description="立刻停止说话并清空字幕。", timeout=20)
def talk_stop() -> Dict[str, Any]:
    """停止当前朗读与字幕。"""
    _ensure_state_dir()
    # 用一条控制记录表示「停」
    with open(INBOX_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": uuid.uuid4().hex[:10], "cmd": "stop",
                                 "at": time.time()}, ensure_ascii=False) + "\n")
    return {"ok": True, "stopped": True}


@register(description="看字幕/语音进程是否在跑、当前状态。", timeout=20)
def talk_status() -> Dict[str, Any]:
    """返回运行状态。"""
    info = _read_json(PID_FILE)
    return {
        "ok": True,
        "running": _server_running(),
        "pid": info.get("pid"),
        "startedAt": info.get("startedAt"),
        "hint": "没在跑的话，下次 talk-say 会自动拉起。",
    }


skill(
    "说话",
    description="怎么用字幕 + 语音：说什么、怎么说、什么时候不说",
    when_to_use="需要在屏幕上出字幕并念出来时",
    body=r"""# 说话

## 一条链路

    talk_say(text)     →  字幕窗口逐字显示 + 语音同步朗读
    talk_stop()        →  立刻闭嘴、清空
    talk_status()      →  看进程活着没

**不想说就不发** —— 不发 talk_say 就完全不出声。

## 声音

固定一种：本机离线音色 `Microsoft Huihui`，语速 0.95。不提供选择。

## 字幕节奏（照搬原实现）

- 每帧 2 字，约 50 字/秒
- **只在句末停**：`。.！!？?` → 停「字数 × 125ms」后清空
- **分号也算停顿点**：`；;`
- **逗号、顿号不停顿** —— 句子中间一路读下去
- 满屏也先按字数停够再清空，不是立刻清
- 换行 = 停顿 + 清空

## `<special>` 标记

包住文件名、代码块等非正常对话内容：

    <special>plugin/displayproc.py<special>

- 标签本身**不显示**
- 范围内的标点**不触发停顿**（否则 `displayproc.py` 会被 `.` 切开）

## 写得好听一点

- 一句话别太长，句末用句号收尾，字幕会自然停顿
- 逗号可以放心用，不会切碎
- 不要靠换行排版，字幕是单行的
""",
)


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--serve"]:
        return serve()
    return pytools.main()


if __name__ == "__main__":
    raise SystemExit(main())
