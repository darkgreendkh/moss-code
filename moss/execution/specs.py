"""Tool schemas and stable prompt examples."""

from dataclasses import dataclass

@dataclass(frozen=True)
class ToolRunOutput:
    content: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None


@dataclass(frozen=True)
class ToolField:
    type: str
    required: bool = True
    default: object = None
    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fields: dict[str, ToolField]
    risky: bool
    description: str
    # 能力标签（spec-03 §4.2）。未声明 = 空集；risky 且未声明会被 policy 直接拒绝
    # （fail-closed），这样新工具忘了声明会立刻在测试里炸，而不是默默放行。
    capabilities: frozenset = frozenset()
    # 路径作用域：workspace（默认）/ run_dir / memory_dir。
    path_scope: str = "workspace"

BASE_TOOL_SPECS = {
    "list_files": ToolSpec(
        name="list_files",
        fields={"path": ToolField("str", required=False, default=".")},
        risky=False,
        capabilities=frozenset({"fs_read"}),
        description="List files in the workspace.",
    ),
    "read_file": ToolSpec(
        name="read_file",
        fields={
            "path": ToolField("str"),
            "start": ToolField("int", required=False, default=1, minimum=1),
            "end": ToolField("int", required=False, default=800, minimum=1),
        },
        risky=False,
        capabilities=frozenset({"fs_read"}),
        description="Read a UTF-8 file by line range.",
    ),
    "write_file": ToolSpec(
        name="write_file",
        fields={"path": ToolField("str"), "content": ToolField("str")},
        risky=True,
        capabilities=frozenset({"fs_write"}),
        description="Write a text file.",
    ),
    "edit_file": ToolSpec(
        name="edit_file",
        fields={"path": ToolField("str"), "old_text": ToolField("str"), "new_text": ToolField("str")},
        risky=True,
        capabilities=frozenset({"fs_read", "fs_write"}),
        description="Replace one exact text block in a file.",
    ),
    "search_text": ToolSpec(
        name="search_text",
        fields={"pattern": ToolField("str"), "path": ToolField("str", required=False, default=".")},
        risky=False,
        capabilities=frozenset({"fs_read"}),
        description="Search the workspace with rg or a simple fallback.",
    ),
    "update_plan": ToolSpec(
        name="update_plan",
        fields={"steps": ToolField("list")},
        risky=False,
        capabilities=frozenset(),
        description="Replace the current plan. Each step: {id, title, status}.",
    ),
    "run_shell": ToolSpec(
        name="run_shell",
        fields={
            "command": ToolField("str"),
            "timeout": ToolField("int", required=False, default=60, minimum=1, maximum=600),
        },
        risky=True,
        # shell 能干的事没有上界，所以四个能力全给——策略层据此决定拦不拦。
        capabilities=frozenset({"fs_read", "fs_write", "exec", "network"}),
        description="Run a shell command in the repo root.",
    ),
    "memory_write": ToolSpec(
        name="memory_write",
        fields={
            "scope": ToolField("str"),
            "topic": ToolField("str"),
            "text": ToolField("str"),
            "tags": ToolField("list", required=False, default=[]),
        },
        risky=False,
        capabilities=frozenset({"memory_write"}),
        description="Write a session or project memory after safety checks.",
    ),
    "memory_update": ToolSpec(
        name="memory_update",
        fields={"id": ToolField("str"), "text": ToolField("str")},
        risky=False,
        capabilities=frozenset({"memory_write"}),
        description="Replace an active durable memory with a new version.",
    ),
    "memory_delete": ToolSpec(
        name="memory_delete",
        fields={"id": ToolField("str")},
        risky=False,
        capabilities=frozenset({"memory_write"}),
        description="Forget an active durable memory by appending a tombstone.",
    ),
    "read_artifact": ToolSpec(
        name="read_artifact",
        fields={
            "path": ToolField("str"),
            "start": ToolField("int", required=False, default=1, minimum=1),
            "end": ToolField("int", required=False, default=200, minimum=1),
        },
        risky=False,
        capabilities=frozenset({"fs_read"}),
        # 作用域是当前 run 目录，不是工作区：卸载的输出是本次运行的证据，
        # 让模型拿这个工具去读仓库文件等于多开一个绕过 read_file 的口子。
        path_scope="run_dir",
        description="Read a byte-for-byte offloaded tool output from this run by line range.",
    ),
    "memory_search": ToolSpec(
        name="memory_search",
        fields={
            "query": ToolField("str"),
            "limit": ToolField("int", required=False, default=5, minimum=1, maximum=20),
        },
        risky=False,
        capabilities=frozenset(),
        description="Search relevant working, episodic, and durable memory.",
    ),
}

DELEGATE_TOOL_SPEC = ToolSpec(
    name="delegate",
    fields={
        # task 与 tasks 二选一，所以两个都不是 schema 级必填；
        # "至少给一个"由 validate_tool 判，报错信息才说得清。
        "task": ToolField("str", required=False, default=""),
        "max_steps": ToolField("int", required=False, default=3, minimum=1),
        # 并行 fan-out：给多条问题时子 agent 并发跑，结果按提交顺序聚合。
        "tasks": ToolField("list", required=False, default=[]),
        # 父 agent 显式指定的相关文件。不给就用 repo map 的起点锚。
        "focus": ToolField("list", required=False, default=[]),
    },
    risky=False,
    capabilities=frozenset({"fs_read", "spawn"}),
    description="Ask bounded read-only child agents to investigate; returns findings with evidence anchors.",
)

RUN_ORCHESTRATION_TOOL_SPEC = ToolSpec(
    name="run_orchestration",
    fields={"script": ToolField("str")},
    # risky 是刻意的：脚本会代替模型发起一串工具调用，而审批摘要里
    # 只看得到脚本本身。它值得被问一次。
    risky=True,
    capabilities=frozenset({"fs_read", "exec"}),
    description=(
        "Run a restricted Python script that batches several read-only tool calls "
        "(fs.read/search/ls/emit). Requires --enable-code-mode and a working sandbox."
    ),
)

DESCRIBE_TOOL_SPEC = ToolSpec(
    name="describe_tool",
    fields={"name": ToolField("str")},
    risky=False,
    capabilities=frozenset(),
    description="Show one tool's full argument schema (used when the tool list is in catalog mode).",
)

USE_SKILL_TOOL_SPEC = ToolSpec(
    name="use_skill",
    fields={"name": ToolField("str")},
    risky=False,
    capabilities=frozenset({"fs_read"}),
    description="Load a skill's instructions by name before doing the work it describes.",
)

TOOL_EXAMPLES = {
    "list_files": '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
    "read_file": '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":400}}</tool>',
    "search_text": '<tool>{"name":"search_text","args":{"pattern":"binary_search","path":"."}}</tool>',
    "run_shell": '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
    "write_file": '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
    "edit_file": '<tool name="edit_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
    "delegate": '<tool>{"name":"delegate","args":{"task":"where is retry handled?","focus":["moss/agent/loop.py"],"max_steps":3}}</tool>',
    "use_skill": '<tool>{"name":"use_skill","args":{"name":"some-skill"}}</tool>',
    "describe_tool": '<tool>{"name":"describe_tool","args":{"name":"search_text"}}</tool>',
    "run_orchestration": (
        '<tool name="run_orchestration"><script>for path in ["a.py", "b.py"]:\n'
        '    text = fs.read(path)\n'
        '    if "TODO" in text:\n'
        '        emit(path)\n</script></tool>'
    ),
    "update_plan": '<tool>{"name":"update_plan","args":{"steps":[{"id":"1","title":"read the parser","status":"in_progress"},{"id":"2","title":"add a test","status":"pending"}]}}</tool>',
    "memory_write": '<tool>{"name":"memory_write","args":{"scope":"project","topic":"key-decisions","text":"Use SQLite","tags":["database"]}}</tool>',
    "memory_update": '<tool>{"name":"memory_update","args":{"id":"mem_123456789abc","text":"Use SQLite WAL"}}</tool>',
    "memory_delete": '<tool>{"name":"memory_delete","args":{"id":"mem_123456789abc"}}</tool>',
    "memory_search": '<tool>{"name":"memory_search","args":{"query":"database choice","limit":5}}</tool>',
    "read_artifact": '<tool>{"name":"read_artifact","args":{"path":"artifacts/007-run_shell-a1b2c3d4e5f6.txt","start":1,"end":200}}</tool>',
}


def legal_tool_names():
    return set(BASE_TOOL_SPECS) | {"delegate", "use_skill", "describe_tool", "run_orchestration"}


def tool_example(name):
    return TOOL_EXAMPLES.get(name, "")


def tool_spec(name):
    """按名字取 ToolSpec。策略层要用它读能力标签。"""
    return _tool_spec(name)


def _tool_spec(name):
    if name in BASE_TOOL_SPECS:
        return BASE_TOOL_SPECS[name]
    if name == "delegate":
        return DELEGATE_TOOL_SPEC
    if name == "use_skill":
        return USE_SKILL_TOOL_SPEC
    if name == "describe_tool":
        return DESCRIBE_TOOL_SPEC
    if name == "run_orchestration":
        return RUN_ORCHESTRATION_TOOL_SPEC
    return None
