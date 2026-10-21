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

# 声明：agent 停下时请通知我（插件会以 --lifecycle agent-idle 再跑一次）。
# 用户点“停止”后 agent 死了，没人能通知字幕 —— 所以需要插件转这一手。
pytools.follow_lifecycle()

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(os.path.expanduser("~"), ".dsh", "python-tools", "state", "talk")
INBOX_FILE = os.path.join(STATE_DIR, "inbox.jsonl")
PID_FILE = os.path.join(STATE_DIR, "server.json")
LOG_FILE = os.path.join(STATE_DIR, "server.log")
# 显示进程把自己的状态写在这里，让工具侧能读到「在念没、还剩几句」
STATE_FILE = os.path.join(STATE_DIR, "playback.json")


# ══════════════════════════════════════════════════════════════════════
#  小工具
# ══════════════════════════════════════════════════════════════════════

def _ensure_state_dir() -> None:
    # 走插件的通用能力：目录已存在就什么都不做
    pytools.state_dir("talk")


def _read_json(path: str) -> Dict[str, Any]:
    data = pytools.read_json(path, {})
    return data if isinstance(data, dict) else {}


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    pytools.write_json(path, payload)


def _pid_alive(pid: Any) -> bool:
    return pytools.pid_alive(pid)


def _detached() -> Dict[str, Any]:
    """保留兼容：旧代码可能还引用它。新代码直接用 pytools.spawn_background。"""
    if os.name == "nt":
        return {
            "creationflags": getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        }
    return {"start_new_session": True}


def _enqueue(text: str, item_id: str = "") -> Dict[str, Any]:
    _ensure_state_dir()
    item = {"id": item_id or uuid.uuid4().hex[:10], "text": text, "at": time.time()}
    with open(INBOX_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    return item


def _done_file(item_id: str) -> str:
    return os.path.join(STATE_DIR, "done-%s.json" % item_id)


def _mark_done(item_id: str, finished: bool, interrupted: bool) -> None:
    """显示进程念完一句就写一个标记文件，等待方靠它知道该返回了。"""
    if not item_id:
        return
    _write_json(_done_file(item_id), {
        "id": item_id,
        "finished": bool(finished),
        "interrupted": bool(interrupted),
        "at": time.time(),
    })


def _read_done(item_id: str) -> Optional[Dict[str, Any]]:
    if not item_id:
        return None
    info = _read_json(_done_file(item_id))
    return info or None


def _clear_done(item_id: str) -> None:
    try:
        os.remove(_done_file(item_id))
    except OSError:
        pass


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


def _playback() -> Dict[str, Any]:
    """读显示进程写下的播放状态。没在跑就是空闲。"""
    info = _read_json(STATE_FILE)
    busy = bool(info.get("busy"))
    return {
        "busy": busy,
        "queued": int(info.get("queued") or 0),
        "remainingSeconds": round(float(info.get("remaining") or 0.0), 1),
        "currentText": str(info.get("current") or ""),
    }


def _spawn_server() -> int:
    _ensure_state_dir()
    pid = pytools.spawn_background(
        [sys.executable or "python", os.path.abspath(__file__), "--serve"],
        cwd=HERE,
        log=LOG_FILE,
    )
    _write_json(PID_FILE, {"pid": int(pid), "startedAt": time.time()})
    return int(pid)


def _clear_pid() -> None:
    """进程自己退出前把 pid 文件删了，下次 talk_say 才知道要重新拉。"""
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


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

    def hard_stop() -> None:
        """彻底停住：清字幕、停语音、丢掉还没念的内容。

        Pacer.clear() 已经同时做了「清字幕」和「player.stop()」，
        直接用它就行，不需要两边各来一次。
        """
        try:
            pacer.clear()
        except Exception:
            # 万一 pacer 出了意外，至少把字幕和声音停掉
            try:
                player.stop()
            except Exception:
                pass
            try:
                window.clear()
            except Exception:
                pass

    # 右键菜单里的「停止并清空」接到这里
    window.on_stop = hard_stop

    window.show()
    window.move_to_bottom_center()

    # 当前正在念的那一句（用于念完/被打断时写完成标记）
    current = {"id": "", "startedAt": 0.0, "seenBusy": False, "idleSince": 0.0}

    def settle_current(finished: bool, interrupted: bool) -> None:
        """把「这一句结束了」告诉等待方。已结过的不重复写。"""
        item_id = current.get("id") or ""
        if not item_id:
            return
        _mark_done(item_id, finished, interrupted)
        current["id"] = ""
        current["startedAt"] = 0.0
        current["seenBusy"] = False
        current["idleSince"] = 0.0

    def pump() -> None:
        try:
            # 还在念：什么都不取，等这一句念完。
            # 这是「不打断」的关键：以前每来一句就立刻 start_turn，
            # 正在念的内容会被当场杀掉。
            speaking = False
            try:
                speaking = bool(player.busy())
            except Exception:
                speaking = False

            # 就算在忙也要看 inbox：用户/agent 可能在念到一半时要求停。
            # 以前这一步只在空闲时做，于是打断要等整句念完才生效。
            # 注意：只处理控制记录，普通文本原样留到下一轮（不在忙时吃）。
            pending_items = _drain_inbox()
            if pending_items:
                keep = []
                for item in pending_items:
                    cmd = str(item.get("cmd") or "")
                    if cmd == "stop":
                        settle_current(False, True)
                        hard_stop()
                        speaking = False
                        continue
                    if cmd == "quit":
                        settle_current(False, True)
                        hard_stop()
                        try:
                            _clear_pid()
                        except Exception:
                            pass
                        app.quit()
                        return
                    keep.append(item)
                # 把普通文本写回收件箱（只写一次，不会无限循环）
                if keep:
                    with open(INBOX_FILE, "a", encoding="utf-8") as handle:
                        for item in keep:
                            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

            # 上一句刚念完（从忙变闲）—— 写完成标记
            # 必须同时满足：
            #   ① 真的忙过（避免刚设上 id 就误判）
            #   ② 队列里没有待念的（避免字幕分屏间隙被当成整句念完）
            #   ③ 已离开“正在念”至少一小段时间（避免合成与播放衔接的瞬时空隙）
            if speaking:
                current["seenBusy"] = True
                current["idleSince"] = 0.0
            elif current.get("id") and current.get("seenBusy"):
                if not current.get("idleSince"):
                    current["idleSince"] = time.time()
                queued_now = 0
                if os.path.isfile(INBOX_FILE):
                    try:
                        with open(INBOX_FILE, "r", encoding="utf-8") as handle:
                            queued_now = sum(1 for line in handle if line.strip())
                    except OSError:
                        queued_now = 0
                quiet_enough = (time.time() - float(current["idleSince"])) >= 0.35
                if queued_now == 0 and quiet_enough:
                    settle_current(True, False)

            if not speaking:
                for item in _drain_inbox():
                    text = str(item.get("text") or "")
                    if not text.strip():
                        continue
                    current["id"] = str(item.get("id") or "")
                    current["startedAt"] = time.time()
                    current["seenBusy"] = False
                    current["idleSince"] = 0.0
                    pacer.start_turn()
                    pacer.feed(text)
                    pacer.end_turn()
                    # 一次只吃一句：剩下的留在队列里，念完这再取
                    break
        except Exception as exc:
            sys.stderr.write("[talk] inbox %s\n" % exc)
            sys.stderr.flush()

        # 把当前状态写出去，agent 靠它判断能不能发下一句
        try:
            busy_now = bool(player.busy())
            pending = 0
            if os.path.isfile(INBOX_FILE):
                try:
                    with open(INBOX_FILE, "r", encoding="utf-8") as handle:
                        pending = sum(1 for line in handle if line.strip())
                except OSError:
                    pending = 0
            _write_json(STATE_FILE, {
                "busy": busy_now,
                "queued": pending,
                "remaining": player.remaining() if busy_now else 0.0,
                "current": player.current_text() if busy_now else "",
                "at": time.time(),
            })
        except Exception:
            pass
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

@register(description="说话：把文字显示成屏幕字幕并念出来（本机离线音色）。wait=True 时阻塞到这句真正念完才返回。", timeout=600)
def talk_say(text: str, wait: bool = False) -> Dict[str, Any]:
    """显示并念出一段话。

    Args:
        text: 要说的话。句末标点会停顿，逗号不停顿；<special>...</special>
              包住的内容原样显示、其中的标点不触发停顿（适合文件名、代码）。
        wait: True = 阻塞到这句真正念完（或被中断）才返回。
              讲课时用它：一页对应一句旁白，讲完才翻页。
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
    # 投递后等一小会，让显示进程把新状态写出来（它是一个 40ms 的循环）
    time.sleep(0.25)
    playback = _playback()
    result: Dict[str, Any] = {
        "ok": True,
        "id": item["id"],
        "started": started,
        "accepted": True,
        # speaking：这一句已经在念了；queued：前面还有没念完的，它排在后面
        "state": "speaking" if playback["busy"] else "queued",
        "busy": playback["busy"],
        "queued": playback["queued"],
        "remainingSeconds": playback["remainingSeconds"],
        "chars": len(text),
    }

    if wait:
        # 等这一句真正念完。超过字数估算的时间就放弃（防止永久卡住）。
        budget = max(60.0, len(text) * 0.4 + 30.0)
        deadline = time.time() + budget
        while time.time() < deadline:
            done = _read_done(item["id"])
            if done is not None:
                result["finished"] = bool(done.get("finished"))
                result["interrupted"] = bool(done.get("interrupted"))
                result["elapsedSeconds"] = round(time.time() - item["at"], 1)
                _clear_done(item["id"])
                if result["interrupted"]:
                    result["hint"] = "被打断了，没有念完。"
                else:
                    result["hint"] = "念完了，可以翻下一页。"
                return result
            time.sleep(0.08)
        result["finished"] = False
        result["interrupted"] = False
        result["timeout"] = True
        result["elapsedSeconds"] = round(time.time() - item["at"], 1)
        result["hint"] = "等超时了（约 %.0f 秒），可能显示进程卡住或窗口不在。" % budget
        return result

    result["hint"] = (
        "已排队。现在音频正在忙，这句话会等前面念完再开始；"
        "想确认念完了再发下一句，用 wait=True。"
        if playback["busy"]
        else "已开始念。想等它念完再继续，用 wait=True。"
    )
    return result


@register(description="agent 主动打断字幕：立刻停止朗读、清空字幕，并让正在等的 talk-say(wait=True) 立即返回。", timeout=20)
def talk_interrupt(reason: str = "") -> Dict[str, Any]:
    """agent 自己打断。

    和 talk-stop 的区别：
      talk-stop      用户要求停（不管等待方）
      talk-interrupt agent 自己要打断（告诉等待方「被打断了」）

    Args:
        reason: 为什么打断，方便追溯。
    """
    _ensure_state_dir()
    before = _playback()
    with open(INBOX_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": uuid.uuid4().hex[:10], "cmd": "stop",
                                 "reason": str(reason or ""),
                                 "at": time.time()}, ensure_ascii=False) + "\n")
    # 等显示进程把完成标记写出来，这样调用方的 wait=True 能立刻返回
    time.sleep(0.3)
    return {
        "ok": True,
        "interrupted": True,
        "hadSpeech": bool(before.get("busy")),
        "queued": before.get("queued", 0),
        "reason": str(reason or ""),
        "hint": "已停止并清空；正在等待的 talk-say(wait=True) 会收到 interrupted=true。",
    }


@register(description="等字幕念完（队列空、不在念）才返回。", timeout=200)
def talk_wait(timeout: float = 120) -> Dict[str, Any]:
    """阻塞到字幕完全空闲。

    Args:
        timeout: 最多等多少秒，默认 120。
    """
    started = time.time()
    deadline = started + max(1.0, float(timeout))
    while time.time() < deadline:
        playback = _playback()
        if not playback["busy"] and int(playback["queued"]) == 0:
            return {
                "ok": True,
                "idle": True,
                "waitedSeconds": round(time.time() - started, 1),
            }
        time.sleep(0.1)
    return {
        "ok": True,
        "idle": False,
        "timeout": True,
        "waitedSeconds": round(time.time() - started, 1),
        "hint": "还在念，或者队列里还有句子。",
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


@register(description="关闭字幕窗口（连进程一起退）。下次 talk-say 会自动重新拉起。", timeout=20)
def talk_close() -> Dict[str, Any]:
    """关掉字幕窗口与常驻进程。"""
    _ensure_state_dir()
    info = _read_json(PID_FILE)
    pid = info.get("pid")
    if not _pid_alive(pid):
        _clear_pid()
        return {"ok": True, "closed": False, "reason": "本来就没在跑"}

    with open(INBOX_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": uuid.uuid4().hex[:10], "cmd": "quit",
                                 "at": time.time()}, ensure_ascii=False) + "\n")

    # 等它自己退出；超时就精确杀这一个 pid（不波及别的 python 进程）
    for _ in range(30):
        time.sleep(0.1)
        if not _pid_alive(pid):
            _clear_pid()
            return {"ok": True, "closed": True, "pid": pid}

    try:
        os.kill(int(pid), 9)
    except Exception:
        pass
    _clear_pid()
    return {"ok": True, "closed": True, "pid": pid, "forced": True}


@register(description="看字幕/语音进程是否在跑、当前状态（是否在念、还剩几句、当前这句还要多久）。", timeout=20)
def talk_status() -> Dict[str, Any]:
    """返回运行状态，包含音频是否正在播。"""
    info = _read_json(PID_FILE)
    running = _server_running()
    playback = _playback() if running else {
        "busy": False, "queued": 0, "remainingSeconds": 0.0, "currentText": "",
    }
    if not running:
        hint = "没在跑，也没有在念。下次 talk-say 会自动拉起。"
    elif playback["busy"]:
        hint = "正在念：%s秒后这句结束%s。现在发的话会排队，不会打断。" % (
            playback["remainingSeconds"],
            "，后面还排着 %d 句" % playback["queued"] if playback["queued"] else "",
        )
    else:
        hint = "空闲，可以发下一句。"
    return {
        "ok": True,
        "running": running,
        "pid": info.get("pid"),
        "startedAt": info.get("startedAt"),
        "busy": playback["busy"],
        "queued": playback["queued"],
        "remainingSeconds": playback["remainingSeconds"],
        "currentText": playback["currentText"],
        "hint": hint,
    }


skill(
    "talk-guide",
    description="怎么用字幕 + 语音：说什么、怎么说、什么时候不说",
    when_to_use="需要在屏幕上出字幕并念出来时",
    body=r"""# 说话

## 一条链路

    talk_say(text)                →  字幕逐字显示 + 语音同步朗读（排队，不打断）
    talk_say(text, wait=True)     →  同上，但**等到这句真的念完才返回**
    talk_wait()                   →  等队列完全空闲（不关心是哪一句）
    talk_status()                 →  在不在念、还剩几句、当前这句还要多久
    talk_interrupt(reason)        →  **agent 主动打断**，并让 wait=True 立刻返回
    talk_stop()                   →  用户要求停（不告诉等待方）
    talk_close()                  →  连窗口一起关

**不想说就不发** —— 不发 talk_say 就完全不出声。

## 讲课时必须用 wait=True

这是黑板/字幕对齐的**唯一可靠方式**。

```python
for page in pages:
    blackboard_draw(page.ops)                    # 先出画面
    talk_say(page.narration, wait=True)          # 再讲，讲完才翻页
```

`wait=True` 会阻塞到：

| 情况 | 返回 |
|---|---|
| 正常念完 | `finished: true, interrupted: false` |
| 被打断 | `finished: false, interrupted: true` |
| 等超时 | `finished: false, timeout: true` |

超时预算自动算：`max(60, 字数 × 0.4 + 30)` 秒。

**不要用轮询代替 wait=True**。轮询既浪费回合，又容易错过瞬间。

## 打断：三种情况用三个工具

| 场景 | 用哪个 | 后果 |
|---|---|---|
| 用户说“停下” | `talk_stop()` | 停止，但等待方不知道发生了什么 |
| **用户插话、要改内容** | `talk_interrupt(reason)` | 停止 + **等待方立刻返回 interrupted** |
| **你自己要换内容** | `talk_interrupt(reason)` | 同上 |

关键区别：`talk_interrupt` 会让正在 `wait=True` 的调用**立刻返回**，
不会让调用方傻等到超时。实测打断到返回延迟约 0.1 秒。

### 典型场景：用户中途插话

```python
# 你正在等第 3 页念完
talk_interrupt("用户插话了")
# → 等待方立刻收到 interrupted=true

# 处理用户的新问题
talk_say("好的，我们先看这个问题……", wait=True)
```

## 用户插话：立即停，不许收尾

**用户说了新东西，第一反应必须是停，不是“把这段讲完”。**

```python
talk_interrupt("用户换话题了")    # 立刻停，等待方也立刻返回
# 然后直接处理用户的新内容，不要说“上回说到……”
```

不要做的事：

- “先把这句念完”—— 用户已经不要了
- “先总结一下”—— 没人在乎
- 把剩下的内容继续排队

只有用户说“等下再讲”时才需要记进度继续。

## 不要打断音频：先看状态再发话

`talk_say` **默认不打断**。如果现在有句子在念，你新发的话会**排队**，等前一句念完再开始。
不会出现“话说一半被腰斩”。

但这也意味着，发得太快会让声音滞后于你的思路。所以：

**发下一句之前，先看 `talk_status()` 的 `busy`。**

| `busy` | 含义 | 你应该 |
|---|---|---|
| `false` | 空闲 | 可以发下一句 |
| `true` | 正在念（`remainingSeconds` 秒后结束） | 等一下；或直接发，它会排队 |

`talk_say` 的返回也一样告诉你结果：

| `state` | 含义 |
|---|---|
| `speaking` | 你这句已经开始念了 |
| `queued` | 前面还有没念完的，你这句排着 |

`queued` 字段是队列里还有几句（不算正在念的那句）。

**连续讲多段时**，要么每段之间查一次 `talk_status` 等 `busy` 变 false，
要么一次把整段放进一个 `talk_say`（推荐，它自己会按标点断句）。

**只有用户明确说“停下”时**才用 `talk_stop`（或让用户右键字幕选“停止并清空”）。
不要用 `talk_stop` 来“清一下再发下一句”——那正是打断。

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
    # 生命周期通知：agent 每轮结束（含被用户中止）时，插件会跑
    #   python talk.py --lifecycle agent-idle
    # 这里直接把“停”指令写进收件箱，常驻显示进程读到就闭嘴。
    event = pytools.lifecycle_event(argv)
    if event:
        if event == "agent-idle":
            try:
                _ensure_state_dir()
                with open(INBOX_FILE, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"id": uuid.uuid4().hex[:10], "cmd": "stop",
                                             "reason": "agent-idle",
                                             "at": time.time()}, ensure_ascii=False) + "\n")
            except Exception:
                pass
        return 0
    if argv[:1] == ["--serve"]:
        return serve()
    return pytools.main()


if __name__ == "__main__":
    raise SystemExit(main())
