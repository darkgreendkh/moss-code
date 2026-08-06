"""Skill 发现、解析与渐进披露（spec-09 §9.4）。

Skill 是放在 `<root>/.moss/skills/*.md` 里的 markdown 文件。**三级渐进披露**：

1. `description` 常驻 prefix——模型得知道这项能力存在；
2. 正文只在模型 `use_skill` 点名时注入；
3. `resources` 里的附件不注入，模型自己 `read_file` 取。

为什么分三级：全文常驻会让 20 个 skill 吃掉几千 token 的稳定前缀，而其中
19 个和当前任务无关。反过来只给个名字，模型又不知道该不该用它。

frontmatter：

```yaml
---
name: run-benchmarks
description: 跑基准并生成报告
allowed-tools: [run_shell, read_file]   # 用这个 skill 期间的能力临时覆盖
scope: ["benchmarks/**"]                # 在这些路径下自动提示
resources: ["scripts/bench.sh"]         # 附件，按需 read_file
source: https://example.com/skill.md    # 第三方来源；有它就走供应链校验
---
```

这里刻意不引入 YAML 依赖，沿用本项目手写解析 markdown 的风格
（见 features/memory.py 里的 DurableMemoryStore）。
"""

import hashlib
import json
from pathlib import Path

from .. import atomic_io
from ..context.token_budget import clip, estimate_tokens

SKILLS_SUBDIR = ".moss/skills"

DESCRIPTION_LIMIT = 300
BODY_LIMIT = 4000
# 进 prefix 的 Skills 段总预算。20 个 skill 也必须装得下（spec-09 §9.4 验收）：
# 稳定前缀是每一轮都要发的，让它随 skill 数量线性膨胀等于按 skill 数收税。
SKILLS_PREFIX_BUDGET_TOKENS = 400
# 单条 skill 在 prefix 里的描述字符上下限。太短就成了纯列名，模型没法判断该不该用。
MIN_PREFIX_DESCRIPTION_CHARS = 40
MAX_PREFIX_DESCRIPTION_CHARS = 200

TRUST_FILE = ".moss/cache/skill_trust.json"


def _one_line(text, limit):
    """单行硬截断。

    不能用 `clip`：它会插一条 `...[truncated N chars]` **换行**注记，
    而 prefix 里的 skill 是"一行一个"——多出来的那行会让列表结构错位，
    也会让 token 预算白算一遍。
    """
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + "…"


def _parse_list_value(value):
    """解析 `[a, b]` 或 `a, b` 两种写法。手写解析器，不引 YAML。"""
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    items = []
    for item in text.split(","):
        item = item.strip().strip("'\"")
        if item:
            items.append(item)
    return tuple(items)


def _parse_frontmatter(lines):
    # 仅当文件第一条非空行是 '---' 时，才认为存在 frontmatter。
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines) or lines[index].strip() != "---":
        return {}, lines

    meta = {}
    cursor = index + 1
    while cursor < len(lines):
        line = lines[cursor]
        if line.strip() == "---":
            cursor += 1
            break
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip().lower()] = value.strip()
        cursor += 1
    return meta, lines[cursor:]


def parse_skill_file(path):
    path = Path(path)
    raw = path.read_text(encoding="utf-8", errors="replace")
    meta, body_lines = _parse_frontmatter(raw.splitlines())
    name = str(meta.get("name", "")).strip() or path.stem
    description = _one_line(meta.get("description", ""), DESCRIPTION_LIMIT)
    body = clip("\n".join(body_lines).strip(), BODY_LIMIT)
    return {
        "name": name,
        "description": description,
        "body": body,
        "path": path.as_posix(),
        # 空元组表示"不覆盖能力"，和"覆盖成空集"是两回事。
        "allowed_tools": _parse_list_value(meta.get("allowed-tools", "")),
        "scope": _parse_list_value(meta.get("scope", "")),
        "resources": _parse_list_value(meta.get("resources", "")),
        "source": str(meta.get("source", "")).strip(),
        # 供应链指纹算的是**整份文件**，包括 frontmatter：
        # 只对正文取指纹的话，改 allowed-tools 提权就不会触发确认。
        "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }


def build_skill_registry(root):
    # skill 不是动态注册的，而是从磁盘显式发现的；这样模型看到的能力集合
    # 始终对应 .moss/skills/ 里真实存在的文件，可审计、可随仓库一起版本化。
    skills_dir = Path(root) / SKILLS_SUBDIR
    if not skills_dir.is_dir():
        return {}
    registry = {}
    for skill_path in sorted(skills_dir.glob("*.md"), key=lambda item: item.name):
        skill = parse_skill_file(skill_path)
        name = skill["name"]
        if not name or name in registry:
            # 同名 skill 以排序靠前的文件为准，避免随机覆盖。
            continue
        registry[name] = skill
    return dict(sorted(registry.items()))


def render_skill_lines(skills, budget_tokens=SKILLS_PREFIX_BUDGET_TOKENS, measure=estimate_tokens):
    """把 skill 列表渲染成 prefix 里的几行，总量卡在预算内。

    skill 多了就按比例缩短每条描述，而不是整段截断——整段截断会让排在后面的
    skill 直接从模型视野里消失，模型永远不知道有这么个能力。
    """
    skills = skills or {}
    if not skills:
        return []
    budget_chars = max(1, int(budget_tokens)) * 4
    per_skill = budget_chars // len(skills)
    allowance = max(MIN_PREFIX_DESCRIPTION_CHARS, min(MAX_PREFIX_DESCRIPTION_CHARS, per_skill))
    def render(width):
        return [
            f"- {name}: {_one_line(skill.get('description', ''), width)}".rstrip()
            for name, skill in skills.items()
        ]

    lines = render(allowance)
    while measure("\n".join(lines)) > budget_tokens and allowance > MIN_PREFIX_DESCRIPTION_CHARS:
        allowance = max(MIN_PREFIX_DESCRIPTION_CHARS, allowance // 2)
        lines = render(allowance)
    return lines


def matches_scope(skill, rel_paths):
    """skill 的 scope glob 是否命中这些路径中的任何一个。"""
    import fnmatch

    patterns = skill.get("scope") or ()
    if not patterns:
        return False
    for pattern in patterns:
        for rel_path in rel_paths:
            path = str(rel_path).replace("\\", "/").lstrip("./")
            if fnmatch.fnmatch(path, pattern):
                return True
            # `benchmarks/**` 该同时命中目录本身。
            if pattern.endswith("/**") and path == pattern[:-3]:
                return True
    return False


def scope_hints(skills, rel_paths):
    """命中 scope 的 skill 只给一行提示，不自动注入全文。

    自动注入等于把"渐进披露"这件事撤销掉：一进某个目录就吃掉几千 token，
    而模型可能根本不打算用它。
    """
    hits = [name for name, skill in (skills or {}).items() if matches_scope(skill, rel_paths)]
    if not hits:
        return ""
    return "Skills available for the paths in play: " + ", ".join(sorted(hits)) + " (load with use_skill)."


def validate_allowed_tools(skill, parent_tools, legal_tools):
    """`allowed-tools` 只能收紧或在父能力集内放开，不能越权。

    fail-closed：越权声明直接拒绝，而不是静默取交集。静默取交集的话，
    一份声称"我需要 run_shell"的第三方 skill 会在只读 run 里安静地降级运行，
    然后因为缺工具而做错事——报错比降级更有用。
    """
    declared = tuple(skill.get("allowed_tools") or ())
    if not declared:
        return None
    unknown = sorted(set(declared) - set(legal_tools))
    if unknown:
        raise ValueError(f"skill {skill['name']} declares unknown tools: {', '.join(unknown)}")
    if parent_tools is not None:
        escalated = sorted(set(declared) - set(parent_tools))
        if escalated:
            raise ValueError(
                f"skill {skill['name']} declares tools outside this run's allowlist: {', '.join(escalated)}"
            )
    # use_skill 永远留着：拿不到它就换不了 skill，也退不出当前 skill。
    return frozenset(declared) | {"use_skill"}


class SkillTrustStore:
    """第三方 skill 的内容指纹台账。

    为什么需要：skill 正文是会被原样注入 prompt 的指令，`allowed-tools` 还能
    影响这一段时间的能力集合。一份从网上拿来的 skill 在本地被改掉一行，
    表现和它原来一模一样——除非有人记住了它原来的样子。
    """

    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def trusted_sha(self, name):
        return str(self._load().get(str(name), {}).get("sha256", ""))

    def trust(self, skill):
        payload = self._load()
        payload[skill["name"]] = {"sha256": skill["sha256"], "source": skill.get("source", "")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_io.write_atomic(self.path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return payload

    def needs_confirmation(self, skill):
        """要不要问一次。只有声明了 `source` 的第三方 skill 才走这条链路。

        本地手写的 skill 不问：那是用户自己写的文件，每改一行都要确认一次
        只会训练用户闭眼按 y，反而削弱了真正需要确认时的那一问。
        """
        if not skill.get("source"):
            return False
        return self.trusted_sha(skill["name"]) != skill["sha256"]
