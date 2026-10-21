# whale host —— 字幕 + 语音 + 目录探测

三个 Python 工具文件，登记进 `dsh-python-tools` 插件后就能被 agent 直接调用。

```
talk.py       入口：注册工具、拉起常驻显示进程
speech.py     语音合成与播放（本机离线音色）
subtitle.py   单行字幕悬浮窗（PyQt6）
pacer.py      字幕节奏（每帧几个字、句末停多久）
probe.py      目录探测：树形结构 + 文件/文件夹大小
```

## 工具

| 工具 | 作用 |
|---|---|
| `talk-say` | 传一段文字 → 屏幕出字幕 + 语音同步朗读，说完自动清空 |
| `talk-stop` | 立刻停止并清空 |
| `talk-status` | 看常驻进程是否在跑 |
| `probe-tree` | 给一个目录，返回树形地图：每个文件带大小，每个文件夹带其下所有文件的总大小 |

**不发 `talk-say` 就不出声。**

## probe-tree 用法

```python
probe_tree(path="C:\\some\\dir", max_depth=3, big_mb=5.0, auto_scan=True)
```

| 参数 | 说明 |
|---|---|
| `path` | 要探测的目录，默认当前目录 |
| `max_depth` | 展开到第几层，默认 3，上限 12 |
| `big_mb` | 超过多少 MB 标记「大文件」，默认 5 |
| `auto_scan` | 额外返回整个目录下最大的 10 个文件 |

返回里有 `tree`（可直接读的文本树）、`total_human`、`file_count`，以及 `biggest_files`。

**大小永远是全量递归算的**，不受 `max_depth` 影响——即使某个目录没展开，它的总大小也是准的。

对 agent 的意义：先拿到地图，判断哪些文件值得读、哪些是几十 MB 的大块头该避开，避免把上下文烧在大文件上。

## 语音

固定一种，不提供选择：

| 项 | 值 |
|---|---|
| 引擎 | Windows OneCore（`winsdk`），完全离线 |
| 音色 | `Microsoft Huihui` |
| 语速 | 0.95（软件重采样实现，因为老 OneCore 音色忽略 SSML 的 rate） |

## 字幕节奏

| 规则 | 行为 |
|---|---|
| 出字速度 | 每帧 2 字，约 50 字/秒 |
| 句末停顿 | `。.！!？?` → 停「字数 × 125ms」后清空 |
| 分号 | `；;` → 停一下、立刻继续 |
| 逗号、顿号 | **不停**，句子中间一路读下去 |
| 满屏 | 先停够时间再清空继续 |
| `<special>...</special>` | 标签不显示，里面的标点不触发停顿（适合文件名、代码） |

有语音时，节奏由「这一屏念完」驱动，不是定时器；带超时看门狗防止卡死。

## 安装

```bash
pip install -r requirements.txt
```

**注意 PyQt6 和 sounddevice**：这两个在当前机器上没有装进系统 Python，
`subtitle.py` / `speech.py` 会去 `C:\Users\computer\Desktop\B2\Tour guide\library` 借用。
如果那个目录不在了，就需要真的装一遍（见 requirements.txt）。

## 登记

```bash
# 让 agent 跑
pytools install C:\Users\computer\Desktop\whale host\talk.py
pytools install C:\Users\computer\Desktop\whale host\probe.py

# 或在 pytools 面板里输入完整路径，点「注册」
```

登记后工具立刻可用，不用重启。

## 自己写工具的话

三件必须的：

1. 顶部 `from pytools_client import register, main`
2. 每个工具用 `@register(...)` 装饰
3. 文件末尾 `if __name__ == "__main__": raise SystemExit(main())`

少了第 3 条，插件读不到你的工具，而且**不会报错**。
