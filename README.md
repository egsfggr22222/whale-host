# whale host — subtitles + speech + directory probing
[中文 README](https://github.com/egsfggr22222/whale-host/blob/main/whale%20host/README.md)

Three Python tool files that, once registered into the `dsh-python-tools` plugin, can be called directly by the agent.

```
talk.py       Entry point: registers tools, launches the resident display process
speech.py     Speech synthesis and playback (local offline voices)
subtitle.py   Single-line subtitle overlay (PyQt6)
pacer.py      Subtitle pacing (characters per frame, pause length at sentence ends)
probe.py      Directory probing: tree structure + file/folder sizes
```

## Tools

| Tool | Purpose |
|---|---|
| `talk-say` | Pass in a piece of text → subtitles appear on screen + speech reads it in sync, auto-clears when done |
| `talk-stop` | Stop immediately and clear |
| `talk-status` | Check whether the resident process is running |
| `probe-tree` | Given a directory, return a tree map: each file with its size, each folder with the total size of all files beneath it |

**No `talk-say`, no sound.**

## probe-tree usage

```python
probe_tree(path="C:\\some\\dir", max_depth=3, big_mb=5.0, auto_scan=True)
```

| Parameter | Description |
|---|---|
| `path` | The directory to probe; defaults to the current directory |
| `max_depth` | How many levels to expand; default 3, max 12 |
| `big_mb` | Mark files larger than this many MB as "big files"; default 5 |
| `auto_scan` | Also return the 10 largest files across the whole directory |

The return value contains `tree` (a directly readable text tree), `total_human`, `file_count`, and `biggest_files`.

**Sizes are always computed with a full recursive walk**, unaffected by `max_depth` — even if a directory isn't expanded, its total size is accurate.

What it means for the agent: get the map first, decide which files are worth reading and which are tens-of-MB monsters to avoid, and don't burn context on large files.

## Speech

One fixed voice, no choice offered:

| Item | Value |
|---|---|
| Engine | Windows OneCore (`winsdk`), fully offline |
| Voice | `Microsoft Huihui` |
| Rate | 0.95 (done via software resampling, because older OneCore voices ignore SSML's `rate`) |

## Subtitle pacing

| Rule | Behavior |
|---|---|
| Character speed | 2 characters per frame, about 50 characters/second |
| End-of-sentence pause | `。.！!？?` → pause for "character count × 125 ms", then clear |
| Semicolon | `；;` → brief pause, then continue immediately |
| Comma, enumeration comma | **No pause**, keep reading straight through the sentence |
| Screen full | Pause long enough first, then clear and continue |
| `<special>...</special>` | Tags are not displayed, and punctuation inside them doesn't trigger pauses (good for file names, code) |

With speech, pacing is driven by "this screen has been read aloud", not by a timer; a timeout watchdog prevents it from getting stuck.

## Installation

```bash
pip install -r requirements.txt
```

**Note on PyQt6 and sounddevice**: these two are not installed into the system Python on the current machine;
`subtitle.py` / `speech.py` borrow them from `C:\Users\computer\Desktop\B2\Tour guide\library`.
If that directory no longer exists, you'll need to actually install them (see requirements.txt).

## Registration

```bash
# Have the agent run
pytools install C:\Users\computer\Desktop\whale host\talk.py
pytools install C:\Users\computer\Desktop\whale host\probe.py

# Or enter the full path in the pytools panel and click "Register"
```

Tools are usable immediately after registration — no restart.

## If you write your own tools

Three mandatory things:

1. `from pytools_client import register, main` at the top
2. Decorate each tool with `@register(...)`
3. `if __name__ == "__main__": raise SystemExit(main())` at the end of the file

Without item 3, the plugin can't read your tools, and **it won't report an error**.
