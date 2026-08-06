"""能力与路径作用域策略。

为什么存在：权限原来只有 `risky: bool`——改 `src/x.py` 和改
`.github/workflows/ci.yml` 是同一档。前者是日常工作，后者能改掉 CI 本身，
等于把项目的所有校验一起关掉。布尔量表达不了这个差别。

fail-closed 是刻意的：工具没声明能力又标了 risky，直接拒绝并告诉作者
该声明什么。忘记声明会立刻在测试里炸，而不是默默按"无所谓"放行。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

CAPABILITIES = frozenset({"fs_read", "fs_write", "exec", "network", "spawn", "memory_write"})

# 默认禁止写入的路径。这些文件改动之后，"再跑一遍验证"这件事本身就不可信了：
# .git 是历史，.github 是 CI，.env 是密钥，.moss 是 agent 自己的状态目录。
DEFAULT_DENY = {
    "fs_write": (".git/**", ".git", ".github/**", ".github", ".env", ".env.*", ".moss/**", ".moss"),
}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool = False
    reason: str = ""
    effects: tuple = ()

    def __bool__(self):
        return self.allowed


@dataclass(frozen=True)
class Policy:
    allow: dict = field(default_factory=dict)
    deny: dict = field(default_factory=dict)
    read_only: bool = False

    @classmethod
    def build(cls, *, allow=None, deny=None, read_only=False, use_defaults=True):
        merged_deny = {key: tuple(value) for key, value in (DEFAULT_DENY if use_defaults else {}).items()}
        for capability, patterns in (deny or {}).items():
            merged_deny[capability] = merged_deny.get(capability, ()) + tuple(patterns)
        merged_allow = {key: tuple(value) for key, value in (allow or {}).items()}
        return cls(allow=merged_allow, deny=merged_deny, read_only=bool(read_only))

    def _denied_path(self, capability, rel_path):
        for pattern in self.deny.get(capability, ()):
            if fnmatch.fnmatch(rel_path, pattern):
                return pattern
            # `.git/**` 不匹配 `.git` 自身，但目录本身也该挡住。
            if pattern.endswith("/**") and rel_path == pattern[:-3]:
                return pattern
        return ""

    def _outside_allowlist(self, capability, rel_path):
        patterns = self.allow.get(capability)
        if not patterns:
            return ""
        if any(fnmatch.fnmatch(rel_path, pattern) for pattern in patterns):
            return ""
        return ",".join(patterns)

    def decide(self, spec, args, resolved_paths=()):
        """判定一次工具调用是否放行。

        resolved_paths 是**已经锚定到仓库内**的相对路径列表；路径逃逸这类
        安全判定仍然由 Moss.path() 负责，这里只管"允不允许碰这些路径"。
        """
        capabilities = frozenset(getattr(spec, "capabilities", frozenset()) or frozenset())
        risky = bool(getattr(spec, "risky", False))
        name = getattr(spec, "name", "?")

        unknown = capabilities - CAPABILITIES
        if unknown:
            return PolicyDecision(
                allowed=False,
                reason=f"tool {name} declares unknown capabilities: {', '.join(sorted(unknown))}",
            )
        if risky and not capabilities:
            # fail-closed：宁可让新工具在测试里立刻炸，也不要默默放行。
            return PolicyDecision(
                allowed=False,
                reason=(
                    f"tool {name} is marked risky but declares no capabilities; "
                    f"add capabilities from {sorted(CAPABILITIES)} to its ToolSpec"
                ),
            )
        if self.read_only and capabilities - {"fs_read"}:
            return PolicyDecision(
                allowed=False,
                reason=f"tool {name} needs {sorted(capabilities - {'fs_read'})} but this run is read-only",
            )

        effects = []
        for capability in sorted(capabilities):
            if capability in {"fs_read", "memory_write"}:
                continue
            for rel_path in resolved_paths:
                pattern = self._denied_path(capability, rel_path)
                if pattern:
                    return PolicyDecision(
                        allowed=False,
                        reason=f"{capability} to {rel_path} is denied by policy ({pattern})",
                    )
                allowlist = self._outside_allowlist(capability, rel_path)
                if allowlist:
                    return PolicyDecision(
                        allowed=False,
                        reason=f"{capability} to {rel_path} is outside the allowed scope ({allowlist})",
                    )
            effects.append(f"{capability}: {', '.join(resolved_paths) or '(no path)'}")

        return PolicyDecision(
            allowed=True,
            requires_approval=risky,
            reason="",
            effects=tuple(effects),
        )


def parse_capability_rules(values):
    """解析 `--allow fs_write=src/**,tests/**` / `--deny network` 这类参数。

    不带 `=` 的形式表示整个能力，用 `**` 覆盖所有路径。
    """
    rules = {}
    for value in values or ():
        text = str(value).strip()
        if not text:
            continue
        capability, _, patterns = text.partition("=")
        capability = capability.strip()
        globs = tuple(item.strip() for item in patterns.split(",") if item.strip()) or ("**",)
        rules.setdefault(capability, ())
        rules[capability] = rules[capability] + globs
    return rules
