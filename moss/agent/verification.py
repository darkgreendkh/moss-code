"""判断一次工具调用算不算"跑过验证"。

为什么单独一个模块：收尾前自检和评测里的 unverified_edit_rate 用的必须是
同一套判定。散成两份口径，指标就会和运行时行为对不上。
"""

# 直接就是验证动作的命令前缀。判定看的是 argv[0] 附近，不是"命令里出现过这个词"——
# `echo "run pytest later"` 不该被当成跑过测试。
VERIFICATION_COMMANDS = (
    "pytest",
    "python -m pytest",
    "python3 -m pytest",
    "python -m unittest",
    "npm test",
    "pnpm test",
    "yarn test",
    "cargo test",
    "go test",
    "make test",
    "ruff",
    "mypy",
    "tsc",
    "eslint",
)


def is_verification_command(name, args, metadata=None):
    if name != "run_shell":
        return False
    # shell 风险分级里已经把测试类命令单独标出来了，优先复用它。
    if str((metadata or {}).get("shell_risk_class", "")) == "test":
        return True
    command = str((args or {}).get("command", "")).strip().lower()
    if not command:
        return False
    # 只看第一条命令：`ls && pytest` 里 pytest 仍然跑了，所以按 && / ; / | 拆开逐段判。
    for segment in _segments(command):
        if segment.startswith(VERIFICATION_COMMANDS):
            return True
    return False


def _segments(command):
    parts = [command]
    for separator in ("&&", ";", "||", "|"):
        expanded = []
        for part in parts:
            expanded.extend(part.split(separator))
        parts = expanded
    return [part.strip() for part in parts if part.strip()]
