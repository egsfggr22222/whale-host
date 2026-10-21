# -*- coding: utf-8 -*-
"""calculator.py —— 一个最小的 Python 工具示例。

它演示三件事：
  1. 一个函数怎么变成 DSH 能调用的工具
  2. 参数怎么从类型注解自动生成
  3. docstring 怎么写才能被读懂
"""

from __future__ import annotations

from typing import Any, Dict

import pytools_client as pytools
from pytools_client import register, skill

pytools.describe(
    "计算器：四则运算 + 常用数学函数。用来演示 Python 工具的最小写法。",
    tags=["demo", "math", "calculator"],
)

skill(
    "calculator-guide",
    description="一个最小的 Python 工具示例：演示注册三步、参数推导、返回值。",
    when_to_use="想学怎么写一个新的 Python 工具文件时当作模板看",
    body=r"""# 计算器示例

这是一个**模板**，不是用来算数的工具（真要算，直接叫 calc 就行）。

## 三步

```python
from pytools_client import register, skill, main   # ① 导入

@register(description="四则运算")                    # ② 声明
 def calc(a: float, b: float, op: str = "add"):
    ...

if __name__ == "__main__":                        # ③ 入口
    raise SystemExit(main())
```

**少了第 ③ 步，插件读不到你的工具，而且不报错。** 这是最容易踩的坑。

## 参数是怎么来的

不需要手写 schema。插件会从两处推导：

1. **类型注解**：`a: float` → 数字类型；`op: str = "add"` → 字符串 + 默认值
2. **docstring 的 `Args:` 段**：变成参数的文字说明

```python
def calc(a: float, b: float, op: str = "add"):
    '''对两个数做一次运算。

    Args:
        a: 第一个数。
        b: 第二个数。
        op: 运算符，add / sub / mul / div 之一。
    '''
```

## 返回值

直接 `return` 一个 dict，它会原样回到 agent 手里。建议带上 `ok` 字段：

```python
return {"ok": True, "result": 56088}
return {"ok": False, "error": "除数不能是 0"}
```

## 重型库怎么放

```python
# 不好：每次读声明都要等它加载，容易超时
import torch

# 好：写进函数里，声明秒出
def predict(...):
    import torch
    ...
```
""",
)


@register(description="四则运算：加减乘除")
def calc(a: float, b: float, op: str = "add") -> Dict[str, Any]:
    """对两个数做一次运算。

    Args:
        a: 第一个数。
        b: 第二个数。
        op: 运算符，add / sub / mul / div 之一。
    """
    ops = {
        "add": ("+", lambda x, y: x + y),
        "sub": ("-", lambda x, y: x - y),
        "mul": ("*", lambda x, y: x * y),
        "div": ("/", lambda x, y: x / y if y != 0 else None),
    }
    if op not in ops:
        return {"ok": False, "error": "不认识的运算符：%s（可用 add / sub / mul / div）" % op}

    symbol, fn = ops[op]
    result = fn(a, b)
    if result is None:
        return {"ok": False, "error": "除数不能是 0"}

    return {
        "ok": True,
        "expression": "%g %s %g" % (a, symbol, b),
        "result": result,
    }


@register(description="算平均数、最大、最小、总和")
def calc_stats(numbers: str) -> Dict[str, Any]:
    """对一串数字做统计。

    Args:
        numbers: 用逗号或空格分隔的数字，例如 "1,2,3,4"。
    """
    raw = numbers.replace(",", " ").replace("，", " ").split()
    try:
        values = [float(item) for item in raw if item.strip()]
    except ValueError:
        return {"ok": False, "error": "里面有不是数字的东西：%s" % numbers}

    if not values:
        return {"ok": False, "error": "没有给数字"}

    return {
        "ok": True,
        "count": len(values),
        "sum": sum(values),
        "average": sum(values) / len(values),
        "min": min(values),
        "max": max(values),
    }


if __name__ == "__main__":
    from pytools_client import main

    raise SystemExit(main())
