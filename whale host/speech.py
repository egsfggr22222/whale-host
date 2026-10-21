# -*- coding: utf-8 -*-
"""speech.py —— 说话：把一段文字变成声音，并播放出来。

移植自 Tour guide 的 plugin/tts.py，**只保留本机离线后端**：

    后端   winrt（Windows OneCore 引擎，完全离线、零下载）
    音色   Microsoft Kangkang（中文男声，固定，不提供选择）
    语速   0.95（原代码默认值；老 OneCore 音色忽略 SSML 的 rate，
           所以用 _respeed 在软件层重采样实现）

对外只有三件事：make() 合成、play() 播放、以及一个能被字幕轮询的 busy()。
"""

import io
import os
import sys
import threading
import time
import wave

# ── 依赖路径：PyQt6 与本项目自带的库都在 Tour guide 里 ──────────────────────
_BASE = os.path.dirname(os.path.abspath(__file__))
_TOUR = r"C:\Users\computer\Desktop\B2\Tour guide"
_LIB = os.path.join(_TOUR, "library")
for _sub in ("sounddevice", "soundfile", "winsdk", ""):
    _p = os.path.join(_LIB, _sub) if _sub else _LIB
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402

# ── 固定参数（不暴露给调用方）────────────────────────────────────────────
VOICE_NAME = "Microsoft Kangkang"
VOICE_SPEED = 1.10
PLAY_RATE = 24000
# 播放看门狗：音频时长 + 这个余量还没播完就强行收尾，防止字幕永远卡在「正在念」
PLAY_GUARD_SEC = 3.0


def _ssml(name: str, text: str, speed: float) -> str:
    """构造 SSML。

    ⚠ 踩过的坑：pitch="0st" 会抛 WinError -2146233033，必须写成带符号的 "+0st"。
    """
    rate = "%.2f" % max(0.5, min(2.0, speed))
    safe = (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xml:lang="zh-CN"><prosody rate="%s" pitch="+0st">'
        '<voice name="%s">%s</voice></prosody></speak>' % (rate, name, safe)
    )


def _respeed(a: "np.ndarray", sr: int, speed: float):
    """软件改语速（重采样）。

    ⚠ 本机老 OneCore 音色（辉辉/Kangkang/Yaoyao）**完全忽略 SSML 的 rate/pitch**，
      实测 rate=1.0 和 0.95 出来的音频一模一样，所以只能在软件层做。
      重采样会同时改变音调：慢 → 更低沉，正好是播报腔想要的效果。
    """
    if abs(speed - 1.0) < 0.01 or len(a) == 0:
        return a, sr
    n = max(1, int(len(a) / float(speed)))
    out = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a.astype(np.float64))
    return out.astype(np.float32), sr


def synthesize(text: str, speed: float = VOICE_SPEED):
    """合成一段文字。返回 (float32 单声道样本, 采样率)。"""
    import asyncio

    from winsdk.windows.media.speechsynthesis import SpeechSynthesizer
    from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions

    async def go():
        synth = SpeechSynthesizer()
        stream = await synth.synthesize_ssml_to_stream_async(_ssml(VOICE_NAME, text, speed))
        size = stream.size
        if size == 0:
            raise RuntimeError("合成返回空音频")
        buf = Buffer(size)
        await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
        reader = DataReader.from_buffer(buf)
        data = bytearray(size)
        reader.read_bytes(data)
        return bytes(data)

    raw = asyncio.run(go())
    with wave.open(io.BytesIO(raw)) as handle:
        sr, channels = handle.getframerate(), handle.getnchannels()
        samples = np.frombuffer(handle.readframes(handle.getnframes()),
                                dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return _respeed(samples, sr, speed)


class Player:
    """一次念一句：say() 异步合成并播放，busy() 告诉字幕「念完没有」。

    字幕进程靠 busy() 决定什么时候翻下一页 —— 这就是「语音和字幕同速」。
    """

    def __init__(self) -> None:
        self._busy = threading.Event()
        self._stop_flag = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._last_text = ""
        self._deadline = 0.0
        self.said = 0
        self.error = ""

    def say(self, text: str, force: bool = False) -> bool:
        """开念一句。

        默认**不打断**：正在念的时候再调，直接拒收并返回 False。
        要不要插话是调用方的决定，不是这里的默认行为。
        只有 force=True（用户明确要求停/换）才真的把正在念的干掉。

        返回 True 表示这一句已经开始念了。
        """
        text = (text or "").strip()
        if not text:
            return False
        with self._lock:
            if self._busy.is_set() and not force:
                return False
            self.stop()
            self._stop_flag.clear()
            self._busy.set()
            self._last_text = text
            self._thread = threading.Thread(target=self._run, args=(text,), daemon=True)
            self._thread.start()
            return True

    def _run(self, text: str) -> None:
        try:
            samples, sr = synthesize(text)
            if self._stop_flag.is_set():
                return
            import sounddevice as sd

            # 重采样到统一播放率，避免不同设备采样率不一致导致变调
            if sr != PLAY_RATE and len(samples) > 0:
                n = max(1, int(len(samples) * PLAY_RATE / float(sr)))
                samples = np.interp(np.linspace(0, len(samples) - 1, n),
                                    np.arange(len(samples)), samples).astype(np.float32)
                sr = PLAY_RATE
            duration = len(samples) / float(sr) if sr else 0.0
            deadline = time.time() + duration + PLAY_GUARD_SEC
            self._deadline = deadline
            sd.play(samples, sr, blocking=False)
            while time.time() < deadline:
                if self._stop_flag.is_set():
                    try:
                        sd.stop()
                    except Exception:
                        pass
                    return
                try:
                    if not sd.get_stream().active:
                        break
                except Exception:
                    break
                time.sleep(0.03)
            try:
                sd.stop()
            except Exception:
                pass
            self.said += 1
            self.error = ""
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)
            sys.stderr.write("[speech] %s\n" % self.error)
            sys.stderr.flush()
        finally:
            self._busy.clear()

    def busy(self) -> bool:
        return self._busy.is_set()

    def remaining(self) -> float:
        """当前这句还剩多少秒（估）；没在念就是 0。"""
        if not self._busy.is_set():
            return 0.0
        return max(0.0, self._deadline - time.time())

    def current_text(self) -> str:
        return self._last_text if self._busy.is_set() else ""

    def stop(self) -> None:
        """立刻闭嘴。"""
        self._stop_flag.set()
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:
            pass
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.5)
        self._busy.clear()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    text = sys.argv[1] if len(sys.argv) > 1 else "你好，我是 DeepSeek。"
    samples, sr = synthesize(text)
    print("合成完成：%.2f 秒 @ %d Hz" % (len(samples) / sr, sr))
    import sounddevice as sd

    sd.play(samples, sr, blocking=True)
    print("播放完成")
