# -*- coding: utf-8 -*-
"""pacer.py —— 节奏：决定字幕每次显示几个字、停多久、什么时候清空。

完整移植自 Tour guide(B1) 的 plugin/subtitle/proc_old.py —— 那版是最全的：

  agent 说话（本工具就是这样）
     · 每帧放 2 字（约 50 字/秒），比直接把文本甩上去慢，更像在讲
     · **只在句末标点处停**：显示完 → 停留（字数 × 125ms）→ 清空
     · 分号也算停顿点；逗号、顿号**不停**（否则碎得像喘不过气）
     · **满屏也先按字数停够**，再清空继续（不是立刻清）
     · 换行 = 停顿 + 清空
     · `<special>...</special>` 里的内容照常显示，但里面的标点**不触发停顿**
       （否则 "plugin/displayproc.py" 会被 "." 切开），标签本身不显示

  语音模式（本工具默认开启）
     · 节奏不由计时器驱动，而是「把当前这屏念完」→ 清空 → 显示下一屏 → 再念
     · speaker 卡住时有超时看门狗，避免字幕永远停在「正在念」
"""

import sys
import time

# ── 节奏参数（想调快慢改这里）────────────────────────────────────────────
TICK_MS = 40                  # 轮询间隔（毫秒）
AGENT_CHARS_PER_TICK = 3      # 每帧放几个字（配 TICK_MS=40 → 约 75 字/秒）

# 停留毫秒数 = 当前这屏的字数 × CHAR_MS
#   125ms/字 = 8 字/秒，基本贴着 Netflix 成人档上限（9 字/秒）
CHAR_MS = 80

BLANK_SEC = 0.0               # 句末标点清空后的额外空隙

# 触发停顿的标点：
#   分号 → 停一下、清空、立刻继续
#   句末 → 停一下、清空、留一点空隙再继续
COMMA_LIKE = "；;"
PERIOD_LIKE = "。.！!？?"

# `<special>` 标记：包住文件名、代码块等非正常对话内容。
# 标签本身不输出，范围内的标点不触发停顿。
# 两种写法都接受：<special>x<special> / <special>x</special>
SPECIAL_TAG = "<special>"
SPECIAL_END = "</special>"
_SPECIAL_TAGS = (SPECIAL_END, SPECIAL_TAG)


def log(message: str) -> None:
    sys.stderr.write("[pacer] %s\n" % message)
    sys.stderr.flush()


class Pacer:
    """上方字幕行的节奏控制。

    缓冲区里存的是 (字符, 是否 special) —— 标签在解析时就被剥掉，
    只留下「这个字要不要按标点停顿」的信息。
    """

    def __init__(self, window, player=None):
        self.window = window
        self.player = player          # 有就是语音模式：节奏由朗读驱动
        self.buf = []                 # [(char, is_special), ...]
        self.raw = ""                 # 还没解析的原始文本（可能含半个标签）
        self.in_special = False
        self.state = "show"           # show | hold | blank | speaking
        self.hold_until = 0.0
        self.blank_until = 0.0
        self.after_hold = "show"
        self.end_pending = False
        self.speak_deadline = 0.0
        self.speak_timeouts = 0

    # ── 解析 <special> ────────────────────────────────────────────────
    def _parse(self) -> None:
        while self.raw:
            hit = None
            for tag in _SPECIAL_TAGS:
                if self.raw.startswith(tag):
                    hit = tag
                    break
            if hit is not None:
                # </special> 一律关闭；<special> 在「已经开着」时也当关闭
                if hit == SPECIAL_END or self.in_special:
                    self.in_special = False
                else:
                    self.in_special = True
                self.raw = self.raw[len(hit):]
                continue

            # 可能是半个标签（流式分块会切开）→ 留着等后续
            if self.raw.startswith("<") and any(t.startswith(self.raw) for t in _SPECIAL_TAGS):
                return

            ch = self.raw[0]
            self.raw = self.raw[1:]
            self.buf.append((ch, self.in_special))

    # ── 输入 ──────────────────────────────────────────────────────────
    def start_turn(self) -> None:
        self.buf = []
        self.raw = ""
        self.in_special = False
        self.state = "show"
        self.hold_until = 0.0
        self.blank_until = 0.0
        self.end_pending = False
        self.window.clear()

    def feed(self, text: str) -> None:
        if not text:
            return
        self.raw += text
        self._parse()

    def end_turn(self) -> None:
        self.end_pending = True

    def clear(self) -> None:
        self.buf = []
        self.raw = ""
        self.in_special = False
        self.state = "show"
        self.hold_until = 0.0
        self.blank_until = 0.0
        self.end_pending = False
        self.window.clear()
        if self.player is not None:
            try:
                self.player.stop()
            except Exception:
                pass

    # ── 每帧 ──────────────────────────────────────────────────────────
    def tick(self, now: float) -> None:
        # ⓪ 语音模式：正在念这一屏 —— 念完才清空、走下一段
        if self.state == "speaking":
            if not self._player_busy():
                self.state = "show"
                self.window.clear()
            elif now >= self.speak_deadline:
                self.speak_timeouts += 1
                try:
                    self.player.stop()
                except Exception:
                    pass
                log("朗读超时（第 %d 次），强制推进字幕" % self.speak_timeouts)
                self.state = "show"
                self.window.clear()
            return

        # ① 保持原样中
        if self.state == "hold":
            if now >= self.hold_until:
                self.window.clear()
                if self.after_hold == "blank":
                    self.state = "blank"
                    self.blank_until = now + BLANK_SEC
                else:
                    self.state = "show"
            return

        # ② 清空等待中（句号后）
        if self.state == "blank":
            if now >= self.blank_until:
                self.state = "show"
            return

        # ③ 出字
        if not self.buf:
            if self.end_pending:
                self.end_pending = False
                if self._voice_on() and (self.window.get_text() or "").strip():
                    self._start_speaking()
                    return
                self.clear()
            return

        for _ in range(AGENT_CHARS_PER_TICK):
            if not self.buf:
                break
            ch, special = self.buf.pop(0)

            # 换行 → 先按字数停够，再清空
            if ch in "\n\r":
                if self._voice_on() and (self.window.get_text() or "").strip():
                    self._start_speaking()
                    return
                if (self.window.get_text() or "").strip():
                    self.state = "hold"
                    self.hold_until = now + self._hold_for_current()
                    self.after_hold = "show"
                    break
                self.window.clear()
                continue

            # 满屏 → 也先停够时间再清空，然后从这个字接着写
            if not self.window.fits(self.window.get_text() + ch):
                self.buf.insert(0, (ch, special))
                if self._voice_on():
                    self._start_speaking()
                else:
                    self.state = "hold"
                    self.hold_until = now + self._hold_for_current()
                    self.after_hold = "show"
                break

            self.window.push_char(ch)

            # <special> 里的标点不触发停顿
            if special:
                continue

            # 该断句了（分号 / 句末标点）
            if ch in PERIOD_LIKE or ch in COMMA_LIKE:
                if self._voice_on():
                    self._start_speaking()
                else:
                    self.state = "hold"
                    self.hold_until = now + self._hold_for_current()
                    self.after_hold = ("blank" if ch in PERIOD_LIKE else "show")
                break

    # ── 语音模式辅助 ──────────────────────────────────────────────────
    def _hold_for_current(self) -> float:
        """按当前这屏的字数算停留时间。"""
        text = (self.window.get_text() or "").strip()
        return max(1, len(text)) * CHAR_MS / 1000.0

    def _voice_on(self) -> bool:
        return self.player is not None

    def _player_busy(self) -> bool:
        try:
            return bool(self.player.busy())
        except Exception:
            return False

    def _start_speaking(self) -> None:
        """把当前字幕显示的内容送去念；念完 tick 会自动清空走下一段。"""
        text = (self.window.get_text() or "").strip()
        if not text:
            self.window.clear()
            return
        try:
            self.player.say(text)
            self.state = "speaking"
            # 超时看门狗：中文 TTS 约 4~5 字/秒，取 0.35 秒/字 + 5 秒余量
            self.speak_deadline = time.time() + len(text) * 0.35 + 5.0
        except Exception as exc:
            log("朗读失败，退回定时模式：%s" % str(exc)[:80])
            self.state = "hold"
            self.hold_until = time.time() + self._hold_for_current()
            self.after_hold = "show"
