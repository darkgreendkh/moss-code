"""代码执行式工具编排（spec-09 §9.3）。

模型写一段受限 Python，一次性编排多个工具调用（"读这 5 个文件里所有含 X 的
函数并汇总"），替代 5 轮往返。

**这是整份 spec 里风险最高的一项。** 三条硬约束：

1. **默认关闭**，`--enable-code-mode` 显式开启，且**沙箱必须可用**
   （`sandbox.detect()` 给出的不是 `none`）。策略层挡不住 `__builtins__` 逃逸，
   只有 OS 级隔离能兜底。没有沙箱就不给这个工具。
2. **AST 白名单**，不是黑名单。黑名单永远漏一个：`__class__.__bases__`、
   `().__class__.__mro__`、`getattr(x, "__" + "globals__")`……逐个封堵是输定了的
   游戏。所以这里反过来：只允许一份明确列出的节点类型和名字，其余一律拒绝。
3. **每次工具 API 调用仍然逐条走 `ToolExecutor`**（审批、脱敏、trace 一个不少）。
   脚本只是把多次调用打包，不是绕过护栏的旁路。

脚本拿不到 `import`、`open`、`__builtins__`，只能用注入的
`fs.read(path)` / `search(pattern, path)` / `ls(path)` / `emit(value)`。
"""

from __future__ import annotations

import ast
import sys
import threading
import time

# 硬超时。脚本只是编排工具调用，正常几秒就该结束；跑更久说明它在算什么东西，
# 那不是这个工具的用途。
DEFAULT_TIMEOUT_S = 10
# 单次编排允许的工具调用次数。没有上限的话一个 `for` 循环就能把仓库读穿。
MAX_TOOL_CALLS = 40
MAX_SCRIPT_CHARS = 8000

# 允许出现的 AST 节点。白名单——名单外的一律拒绝，包括将来 Python 新增的节点。
ALLOWED_NODES = frozenset(
    {
        ast.Module, ast.Expr, ast.Assign, ast.AugAssign, ast.AnnAssign,
        ast.For, ast.While, ast.If, ast.Break, ast.Continue, ast.Pass,
        ast.Compare, ast.BoolOp, ast.UnaryOp, ast.BinOp, ast.IfExp,
        ast.Call, ast.Name, ast.Load, ast.Store, ast.Constant,
        ast.List, ast.Tuple, ast.Dict, ast.Set, ast.Slice, ast.Subscript,
        ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
        ast.comprehension, ast.keyword, ast.Starred,
        ast.And, ast.Or, ast.Not, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
        ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
        ast.USub, ast.UAdd,
        ast.JoinedStr, ast.FormattedValue,
    }
)

# 允许调用的内建。挑的都是纯数据处理，没有一个能拿到解释器状态。
# 注意没有 `getattr` / `type` / `vars` / `dir` / `eval` / `exec` / `open` / `__import__`。
SAFE_BUILTINS = {
    "len": len, "range": range, "enumerate": enumerate, "sorted": sorted,
    "sum": sum, "min": min, "max": max, "abs": abs, "round": round,
    "str": str, "int": int, "float": float, "bool": bool,
    "list": list, "dict": dict, "set": set, "tuple": tuple,
    "any": any, "all": all, "zip": zip, "reversed": reversed,
}

# 脚本里可以点属性访问的方法名。**只有这些**——`Attribute` 是逃逸的主入口
# （`x.__class__`、`x.__globals__`），所以按名字白名单，而不是按"不是 dunder"。
ALLOWED_ATTRIBUTES = frozenset(
    {
        # 注入的 API
        "read", "search", "ls",
        # 字符串/容器上少数确实需要的方法
        "split", "splitlines", "strip", "lstrip", "rstrip", "lower", "upper",
        "startswith", "endswith", "join", "replace", "count", "find",
        "append", "extend", "get", "items", "keys", "values", "sort",
    }
)


# 脚本能引用的自由名字：注入的 API + 安全内建。别的一律拒绝。
# 只按节点类型做白名单是不够的——`eval(...)` / `getattr(...)` / `globals()`
# 在 AST 上就是一个再普通不过的 `Call(Name)`，靠"运行时 NameError"兜底
# 意味着这段脚本已经跑起来了，而它前面可能已经做了别的事。
INJECTED_NAMES = frozenset({"fs", "search", "ls", "emit"})


class CodeModeError(ValueError):
    """脚本没通过校验，或者跑的时候越界了。"""


class CodeModeDisabled(CodeModeError):
    """code mode 没开，或者沙箱不可用。"""


def sandbox_ready(sandbox_plan):
    """沙箱到底能不能用。`none` 就是不能用。

    这是硬前置：AST 白名单是第一道，OS 隔离是第二道。只有第一道的话，
    一个我们没想到的逃逸路径就是完整的任意代码执行。
    """
    return bool(sandbox_plan is not None and str(getattr(sandbox_plan, "mode", "none")) != "none")


def _bound_names(tree):
    """脚本自己绑定的名字（赋值目标、for 变量、推导式变量）。

    没有函数/类定义节点在白名单里，所以不需要处理作用域嵌套——
    整段脚本就是一个平坦的命名空间。
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
    return names


def validate_script(source):
    """AST 白名单校验。任何一条不满足就抛 `CodeModeError`。

    白名单而不是黑名单：黑名单要枚举所有逃逸路径，而逃逸路径的集合
    随 Python 版本增长——这是一场输定了的游戏。
    """
    source = str(source)
    if not source.strip():
        raise CodeModeError("script must not be empty")
    if len(source) > MAX_SCRIPT_CHARS:
        raise CodeModeError(f"script is longer than {MAX_SCRIPT_CHARS} chars")
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise CodeModeError(f"script does not parse: {exc}") from exc

    bound = _bound_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr not in ALLOWED_ATTRIBUTES:
                raise CodeModeError(f"attribute access is not allowed: .{node.attr}")
            continue
        if type(node) not in ALLOWED_NODES:
            raise CodeModeError(f"{type(node).__name__} is not allowed in code mode")
        if isinstance(node, ast.Name):
            if node.id.startswith("_"):
                # 下划线开头的名字是逃逸链的起点（`__builtins__`、`_[1]`）。
                raise CodeModeError(f"names starting with '_' are not allowed: {node.id}")
            if isinstance(node.ctx, ast.Load) and node.id not in bound | INJECTED_NAMES | set(SAFE_BUILTINS):
                raise CodeModeError(f"name is not available in code mode: {node.id}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "__" in node.value:
            # 字符串里的 dunder 只有一个用途：拼出来喂给 getattr。
            # getattr 已经不在名单里了，但把这条一起堵上，成本是零。
            raise CodeModeError("dunder strings are not allowed in code mode")
    return tree


class _Api:
    """注入脚本命名空间的工具门面。每个方法都逐条走 ToolExecutor。"""

    def __init__(self, run_tool, budget):
        self._run_tool = run_tool
        self._budget = budget

    def _call(self, name, args):
        self._budget.spend(name)
        return self._run_tool(name, args)

    def read(self, path, start=1, end=800):
        return self._call("read_file", {"path": str(path), "start": int(start), "end": int(end)})

    def search(self, pattern, path="."):
        return self._call("search_text", {"pattern": str(pattern), "path": str(path)})

    def ls(self, path="."):
        return self._call("list_files", {"path": str(path)})


class _Budget:
    def __init__(self, limit=MAX_TOOL_CALLS):
        self.limit = int(limit)
        self.calls = []

    def spend(self, name):
        if len(self.calls) >= self.limit:
            raise CodeModeError(f"script exceeded {self.limit} tool calls")
        self.calls.append(str(name))


def run_script(source, run_tool, *, timeout=DEFAULT_TIMEOUT_S, max_tool_calls=MAX_TOOL_CALLS):
    """校验并执行一段编排脚本。返回 `(emitted, calls)`。

    `run_tool(name, args)` 必须是受护栏的入口（`Moss.run_tool`）——
    脚本的价值是省往返，不是省护栏。

    超时有两层：worker 线程内装一个逐行 trace，过了截止时间就在脚本自己的
    栈里抛出来（`while True: pass` 靠这层真正停下）；外层 `join` 再兜一次底，
    应对 trace 覆盖不到的情形（比如卡在某个 C 层调用里）。外层兜底触发时
    线程仍然活着——那种情况只能靠沙箱那一层，这也是沙箱是硬前置的原因之一。
    """
    tree = validate_script(source)
    budget = _Budget(max_tool_calls)
    emitted = []
    api = _Api(run_tool, budget)

    namespace = {
        # 没有 __builtins__ 就没有 import / open / eval。这是最要紧的一行。
        "__builtins__": dict(SAFE_BUILTINS),
        # 同一个 api 实例：调用次数预算必须是全局共享的，
        # 每个入口各持一份的话，`for` 循环里换个入口就能绕过上限。
        "fs": api,
        "search": api.search,
        "ls": api.ls,
        "emit": emitted.append,
    }

    # 编译**校验过的那棵树**，而不是重新 parse 一次源码：两次 parse 之间
    # 源码理论上可以不是同一份（调用方传的是可变对象也说不准），
    # 那正是 TOCTOU 的形状。
    compiled = compile(tree, filename="<code_mode>", mode="exec")
    failure = []
    deadline = time.monotonic() + float(timeout)

    def target():
        # 逐行 trace 检查截止时间。为什么需要它：`while True: pass` 过得了
        # AST 白名单（它不是逃逸，只是死循环），而 join(timeout) 只是"不再等"——
        # 那个线程会继续烧一个核直到进程退出。trace 让超时变成真正的终止。
        sys.settrace(_deadline_tracer(deadline))
        try:
            exec(compiled, namespace)  # noqa: S102 - 已过 AST 白名单 + 空 builtins + 沙箱前置
        except CodeModeError as exc:
            failure.append(exc)
        except Exception as exc:
            failure.append(CodeModeError(f"script raised {type(exc).__name__}: {exc}"))
        finally:
            sys.settrace(None)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    # 多给一点余量让 tracer 自己抛出来；它没抛就退回"不再等"的老语义。
    worker.join(float(timeout) + 1.0)
    if worker.is_alive():
        raise CodeModeError(f"script did not finish within {timeout}s")
    if failure:
        raise failure[0]
    return emitted, list(budget.calls)


def _deadline_tracer(deadline):
    def tracer(frame, event, arg):
        if time.monotonic() > deadline:
            raise CodeModeError("script exceeded its time budget")
        return tracer

    return tracer


def render_result(emitted, calls):
    lines = [f"orchestration ran {len(calls)} tool call(s): {', '.join(calls) or '(none)'}"]
    if not emitted:
        lines.append("(the script emitted nothing; call emit(...) with what you want to keep)")
    else:
        lines.extend(str(item) for item in emitted)
    return "\n".join(lines)
