"""Skill 发现与解析。

Skill 是放在 `<root>/.moss/skills/*.md` 里的 markdown 文件：frontmatter 给出
`name` 和 `description`（让模型在 prompt 里看到这项能力），正文是真正的指令，
只有当模型用 `use_skill` 工具点名某个 skill 时，才把正文注入上下文。

这里刻意不引入 YAML 依赖，沿用本项目手写解析 markdown 的风格
（见 features/memory.py 里的 DurableMemoryStore）。
"""

from pathlib import Path

from .workspace import clip

SKILLS_SUBDIR = ".moss/skills"

DESCRIPTION_LIMIT = 300
BODY_LIMIT = 4000


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
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body_lines = _parse_frontmatter(text.splitlines())
    name = str(meta.get("name", "")).strip() or path.stem
    description = clip(str(meta.get("description", "")).strip(), DESCRIPTION_LIMIT)
    body = clip("\n".join(body_lines).strip(), BODY_LIMIT)
    return {
        "name": name,
        "description": description,
        "body": body,
        "path": path.as_posix(),
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
