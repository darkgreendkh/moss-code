"""工作区快照工具。

这个模块负责在 agent 按需读文件之前，先给它一份便宜的“仓库第一印象”。
这份快照刻意保持小而稳定：主要包含 Git 事实和少量白名单项目文档。
"""

import hashlib
import json
import subprocess
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import project_config_section
from .token_budget import clip

# 这些文件最可能直接影响 agent 的行动方式。
# 我们不会预加载整个仓库，只会先给模型一小份“导航包”。
DOC_NAMES = (
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "README.md",
    "README.rst",
    "README.txt",
    "pyproject.toml",
    "package.json",
    "Makefile",
    "justfile",
)
# 单份文档进 prompt 的预算（字符）。超了就退化成结构摘要 + 取回指针，
# 而不是硬切断——被砍掉一半的 README 比一份目录更容易把模型带偏。
DOC_PREVIEW_BUDGET = 1200
# 结构摘要里保留多少行正文开头。
DOC_PREVIEW_HEAD_LINES = 12
# 就近文档：文件类工具碰到某个目录时，会去祖先目录找这些文件。
NEAREST_DOC_NAMES = ("AGENTS.md", "CLAUDE.md")
IGNORED_PATH_NAMES = {".git", ".moss", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv"}

# 指纹口径的版本号。改了指纹的计算方式就必须改它——旧 checkpoint 里存的是老口径的
# 指纹，不带版本号的话会被误判成“工作区变了”（workspace-mismatch），
# 而真实原因是 schema 变了。见 checkpoint.evaluate_resume_state。
WORKSPACE_FINGERPRINT_VERSION = "ws-v2"

# status 段进 prompt 时最多列多少条路径。计数摘要总是完整的，
# 所以列表被截断也不会让模型误判改动规模。
STATUS_ENTRY_LIMIT = 20

# git 事实的默认缓存 TTL（秒）。一轮 agent 循环里 prefix 可能被刷新多次，
# 没必要每次都 fork 两个 git 进程；但 TTL 必须足够短，
# 否则模型看到的 status 会落后于它自己刚做的改动。
GIT_FACTS_TTL_S = 0.5

# 增量快照里的“墓碑”值：git 说这个路径变了，但它已经不在磁盘上。
MISSING_ENTRY = ("missing", 0)


@dataclass(frozen=True)
class SnapshotResult:
    """一次快照的完整结果。

    `entries` 是给 diff 用的 path -> (mtime_ns, size)；`untracked` 只在 git 策略下
    有值，用来把“本来就没被跟踪的新文件”和“原本干净、刚被改脏的文件”区分开——
    增量快照的 key 集合只覆盖变更集，光看 diff 两者都表现为“新出现的 key”。
    """

    entries: dict = field(default_factory=dict)
    strategy: str = "walk"
    # None 表示“这次快照没有 git 信息”——此时 diff 只能按 key 差异判断，
    # 新出现的 key 一律算 created。空集合和 None 语义不同，别合并。
    untracked: frozenset = None


def _lstat_entry(path):
    """对单个路径取快照值。

    符号链接记 ("symlink", target 的 hash) 而不是被跳过：原来的实现直接 continue，
    于是“把某个文件换成指向仓库外的软链”这种改动在快照里完全不可见。
    """
    stat = path.lstat()
    if hasattr(stat, "st_mode") and Path(path).is_symlink():
        try:
            target = str(path.readlink())
        except OSError:
            target = ""
        return ("symlink", hashlib.sha256(target.encode("utf-8")).hexdigest()[:16])
    return (stat.st_mtime_ns, stat.st_size)


def _walk_snapshot(root, ignore):
    snapshot = {}
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (OSError, PermissionError):
            continue
        for entry in entries:
            try:
                key = entry.relative_to(root).as_posix()
            except ValueError:
                continue
            if entry.name in IGNORED_PATH_NAMES:
                continue
            try:
                is_symlink = entry.is_symlink()
                # 不跟随符号链接目录：符号链接指回祖先目录会导致遍历死循环，
                # 让 risky 工具直接卡死——对 agent 来说是最糟糕的体验。
                # 但链接本身要记一笔，否则换掉链接目标就成了隐身改动。
                is_dir = entry.is_dir() and not is_symlink
                if ignore is not None and ignore.match(key, is_dir=is_dir):
                    continue
                if is_symlink:
                    snapshot[key] = _lstat_entry(entry)
                    continue
                if is_dir:
                    stack.append(entry)
                    continue
                if not entry.is_file():
                    continue
                snapshot[key] = _lstat_entry(entry)
            except (OSError, ValueError):
                continue
    return snapshot


def _git_changed_paths(root):
    """拿 git 的变更集。返回 (paths, untracked)；git 不可用时返回 None。"""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except Exception:
        return None
    paths = []
    untracked = set()
    fields = [item for item in result.stdout.split("\0") if item]
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code.startswith("R") or code.startswith("C"):
            # 重命名/复制的原路径单独占一个 \0 字段。
            if index < len(fields):
                paths.append(fields[index])
                index += 1
        paths.append(path)
        if code.startswith("??"):
            untracked.add(path)
    return paths, untracked


def capture_snapshot(root, *, ignore=None, strategy="auto", git_changed=None, detailed=False):
    """给工作区拍一张快照，记录每个文件的 (mtime_ns, size)。

    变更检测只需要“文件有没有动过”，不需要内容 hash。
    用 stat 的 (mtime_ns, size) 元组代替 sha256，避免对每个文件读整份内容——
    在几百个文件的仓库里这一步能把 risky 工具的开销从秒级降到毫秒级。

    strategy:
      - "git"：只对 git 报告的变更路径 lstat，开销从 O(全仓) 降到 O(变更集)；
      - "walk"：全量遍历，作为没有 git 时的退路；
      - "auto"：能用 git 就用 git，否则 walk。
    `git_changed` 让调用方补充一批必须覆盖的路径（比如上一张快照里的 key），
    否则前后两张快照的 key 集合对不齐，删除会被漏掉。
    """
    root = Path(root)
    resolved_strategy = strategy
    snapshot = None
    untracked = None
    if strategy in {"git", "auto"}:
        changed = _git_changed_paths(root) if (root / ".git").exists() else None
        if changed is not None:
            paths, untracked_paths = changed
            untracked = frozenset(untracked_paths)
            resolved_strategy = "git"
            snapshot = {}
            for rel in sorted(set(paths) | set(git_changed or ())):
                rel = rel.rstrip("/")
                if not rel:
                    continue
                if ignore is not None and ignore.match(rel):
                    continue
                candidate = root / rel
                try:
                    snapshot[rel] = _lstat_entry(candidate)
                except OSError:
                    # 路径已经没了。增量快照下不能简单地“不记录”：删除之前这个文件
                    # 是干净的，压根没进过上一张快照，两边都缺 key 就等于漏报。
                    # 记一个显式的墓碑，让 diff 能认出这是删除。
                    snapshot[rel] = MISSING_ENTRY
        elif strategy == "git":
            # 显式要求 git 却拿不到变更集：退回 walk，并在结果里如实标注，
            # 否则评测口径会把“没扫到”误读成“没改动”。
            resolved_strategy = "walk_only"
    if snapshot is None:
        snapshot = _walk_snapshot(root, ignore)
        if resolved_strategy != "walk_only":
            resolved_strategy = "walk"
    if detailed:
        return SnapshotResult(entries=snapshot, strategy=resolved_strategy, untracked=untracked)
    return snapshot


def diff_snapshots(before, after, *, untracked=None):
    """对比两份快照，返回 (changed_paths, summaries)，summaries 形如 created:/deleted:/modified:path。

    `untracked` 是 after 时刻 git 认为未跟踪的路径集合。增量快照下，一个原本干净的
    已跟踪文件被改脏后才第一次出现在快照里，光看 key 差异会被误判成 created；
    有了这个集合就能把它正确标成 modified。
    """
    changed_paths = []
    summaries = []

    def gone(value):
        return value is None or value == MISSING_ENTRY

    for path in sorted(set(before) | set(after)):
        old = before.get(path)
        new = after.get(path)
        # 注意不能把“两边都 gone”一律跳过：增量快照里一个干净文件被删除时，
        # before 压根没有这个 key（old is None），after 是墓碑，这正是要报的删除。
        # 真正的重复墓碑会被 old == new 提前挡掉。
        if old == new:
            continue
        changed_paths.append(path)
        if gone(new):
            summaries.append(f"deleted:{path}")
        elif gone(old):
            created = untracked is None or path in untracked
            summaries.append(f"{'created' if created else 'modified'}:{path}")
        else:
            summaries.append(f"modified:{path}")
    return changed_paths, summaries


@dataclass(frozen=True)
class DocRef:
    """一份项目文档在 prompt 里的表示。

    为什么 preview 和 digest 分开：preview 是被预算裁剪过的展示文本，
    digest 是对**全文**算的。指纹只认 digest——否则改动第 1200 字符之后的内容
    不会让缓存失效，模型会拿着过期的文档继续干活。
    """

    path: str
    preview: str
    digest: str
    total_lines: int
    truncated: bool


@dataclass(frozen=True)
class GitFacts:
    """一次 git 采集的结果。

    为什么存在：原本每轮要跑 4 个 git 子进程（rev-parse/branch/symbolic-ref/status/log），
    在大仓库上是 prompt 构建耗时的大头。这里把采集收敛成 status+log 两次调用，
    仓库身份（repo_root / default_branch）单独按 cwd 缓存，进程内只解析一次。
    """

    branch: str = "-"
    default_branch: str = "main"
    status_entries: tuple = ()
    counts: dict = field(default_factory=dict)
    recent_commits: tuple = ()
    collected_at: float = 0.0
    cache_key: str = ""


# 进程级缓存。key 是 str(cwd)。
_GIT_FACTS_CACHE = {}
_REPO_IDENTITY_CACHE = {}


def _run_git(args, cwd, fallback=""):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip() or fallback
    except Exception:
        return fallback


def _git_dir_cache_key(cwd):
    """用 .git/index 与 .git/HEAD 的 mtime 当缓存键。

    它们覆盖了“暂存区变了”和“HEAD 变了”两类事件；工作树里的未跟踪文件改动
    不会体现在这里，所以缓存复用还必须叠加一个很短的 TTL。
    """
    parts = []
    base = Path(cwd)
    for candidate in (base, *base.parents):
        git_dir = candidate / ".git"
        if not git_dir.exists():
            continue
        # worktree / submodule 下 .git 是文件而不是目录，此时只能拿它自己的 mtime。
        targets = (git_dir / "index", git_dir / "HEAD") if git_dir.is_dir() else (git_dir,)
        for target in targets:
            try:
                parts.append(str(target.stat().st_mtime_ns))
            except OSError:
                parts.append("-")
        break
    return "|".join(parts)


def _repo_identity(cwd):
    """解析 (repo_root, default_branch)，进程内按 cwd 缓存。

    这两个值在一次运行里不会变，却各要一个子进程，所以从每轮采集里摘出来。
    """
    key = str(cwd)
    cached = _REPO_IDENTITY_CACHE.get(key)
    if cached is not None:
        return cached
    repo_root = Path(_run_git(["rev-parse", "--show-toplevel"], cwd, str(cwd))).resolve()
    default_branch = _run_git(
        ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd, "origin/main"
    ) or "origin/main"
    if default_branch.startswith("origin/"):
        default_branch = default_branch[len("origin/") :]
    identity = (repo_root, default_branch)
    _REPO_IDENTITY_CACHE[key] = identity
    return identity


def invalidate_git_facts_cache():
    """强制下一次采集重新跑 git。risky 工具执行后调用，避免 TTL 内看到过期 status。"""
    _GIT_FACTS_CACHE.clear()


def parse_status_counts(entries):
    """把 `git status --short` 的行解析成 {modified, added, deleted, untracked} 计数。"""
    counts = {"modified": 0, "added": 0, "deleted": 0, "untracked": 0}
    for entry in entries:
        code = entry[:2]
        if not code.strip():
            continue
        if code.startswith("??"):
            counts["untracked"] += 1
        elif "D" in code:
            counts["deleted"] += 1
        elif "A" in code:
            counts["added"] += 1
        else:
            counts["modified"] += 1
    return counts


def collect_git_facts(cwd, *, ttl_s=GIT_FACTS_TTL_S, cache=None):
    """采集 git 事实：稳态下只跑 2 个子进程（status 与 log）。

    缓存命中条件是“TTL 未过期 **且** .git 指纹未变”——两者缺一不可：
    只看指纹会漏掉未跟踪文件的新增（不写 .git/index），只看 TTL 会在 commit 后
    还给出旧数据。
    """
    cache = _GIT_FACTS_CACHE if cache is None else cache
    key = str(cwd)
    cache_key = _git_dir_cache_key(cwd)
    cached = cache.get(key)
    if (
        cached is not None
        and cached.cache_key == cache_key
        and (time.monotonic() - cached.collected_at) < ttl_s
    ):
        return cached

    _, default_branch = _repo_identity(cwd)
    raw_status = _run_git(["status", "--short", "--branch", "--untracked-files=normal"], cwd)
    lines = [line for line in raw_status.splitlines() if line.strip()]
    branch = "-"
    entries = []
    for line in lines:
        if line.startswith("## "):
            # 形如 `## main...origin/main [ahead 1]`，只取本地分支名。
            head = line[3:].split("...", 1)[0].strip()
            branch = head.split(" ", 1)[0] or "-"
            if branch.startswith("HEAD (no branch)") or branch == "HEAD":
                branch = "-"
            continue
        entries.append(line)
    commits = [line for line in _run_git(["log", "--oneline", "-5"], cwd).splitlines() if line]

    facts = GitFacts(
        branch=branch,
        default_branch=default_branch,
        status_entries=tuple(entries),
        counts=parse_status_counts(entries),
        recent_commits=tuple(commits),
        collected_at=time.monotonic(),
        # 记的是采集**之后**的指纹：`git status` 自己会刷新 .git/index 的 stat 缓存，
        # 用采集前的指纹存进去，下一次比对必然不相等，缓存等于永远不命中。
        cache_key=_git_dir_cache_key(cwd),
    )
    cache[key] = facts
    return facts


def summarize_doc(text, path, budget=DOC_PREVIEW_BUDGET, head_lines=DOC_PREVIEW_HEAD_LINES):
    """超预算的文档退化成“标题树 + 开头几行 + 取回指针”。

    为什么不直接硬切：一份被砍在半句话上的 README 会让模型误以为自己看全了；
    而“有哪些章节 + 怎么取回全文”既省 token，又明确告诉它这里还有东西。
    """
    lines = text.splitlines()
    if len(text) <= budget:
        return text, False
    headings = [line.strip() for line in lines if line.startswith("#") and line.count("#") <= 3]
    pointer = f"（全文 {len(lines)} 行，用 read_file {path} 取回）"
    parts = []
    if headings:
        parts.append("\n".join(headings[:20]))
    parts.append("\n".join(lines[:head_lines]))
    body = "\n".join(part for part in parts if part.strip())
    # 取回指针必须活下来：它是模型知道"这里还有东西、怎么拿"的唯一线索，
    # 所以先给它留出位置，再切正文，而不是整段一起切。
    return f"{clip(body, max(0, budget - len(pointer) - 1))}\n{pointer}", True


def _doc_ref(path, rel_key, budget=DOC_PREVIEW_BUDGET):
    text = path.read_text(encoding="utf-8", errors="replace")
    preview, truncated = summarize_doc(text, rel_key, budget)
    return DocRef(
        path=rel_key,
        preview=preview,
        digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        total_lines=len(text.splitlines()),
        truncated=truncated,
    )


def _configured_doc_names(repo_root):
    names = project_config_section(repo_root, "repo_context").get("doc_names")
    if isinstance(names, (list, tuple)) and names:
        return tuple(str(name) for name in names)
    return None


def discover_docs(repo_root, cwd, extra_names=(), doc_names=None):
    """分层发现项目文档：repo_root 与 cwd 各扫一遍白名单。

    名单可以被 .moss/config.json 的 repo_context.doc_names 整体覆盖——
    不同技术栈的“导航包”差别很大，硬编码一份名单只能服务 Python 仓库。
    """
    repo_root = Path(repo_root)
    cwd = Path(cwd)
    names = tuple(doc_names) if doc_names else DOC_NAMES
    names = names + tuple(extra_names)
    refs = {}
    for base in (repo_root, cwd):
        for name in names:
            path = base / name
            if not path.is_file():
                continue
            try:
                key = str(path.relative_to(repo_root))
            except ValueError:
                key = str(path)
            if key in refs:
                continue
            try:
                refs[key] = _doc_ref(path, key)
            except OSError:
                continue
    return refs


def find_nearest_instruction_docs(repo_root, target, names=NEAREST_DOC_NAMES):
    """从 target 所在目录往上找就近的指令文档，由近到远返回相对路径。

    为什么按“由近到远”返回：同名规则冲突时最近的目录优先——
    子目录里的 AGENTS.md 就是为了覆盖仓库根那份而存在的。
    """
    repo_root = Path(repo_root).resolve()
    target = Path(target)
    directory = target if target.is_dir() else target.parent
    found = []
    try:
        directory = directory.resolve()
        directory.relative_to(repo_root)
    except (OSError, ValueError):
        return found
    for candidate in (directory, *directory.parents):
        for name in names:
            doc = candidate / name
            if doc.is_file():
                found.append(doc.relative_to(repo_root).as_posix())
        if candidate == repo_root:
            break
    return found


class WorkspaceContext:
    def __init__(
        self,
        cwd,
        repo_root,
        branch,
        default_branch,
        status,
        recent_commits,
        project_docs,
        invocation_cwd=None,
        status_entries=None,
        status_counts=None,
        doc_refs=None,
    ):
        # invocation_cwd 是用户真正敲命令的目录，repo_root 是仓库根。
        # 两者必须分开存：第二轮刷新 prefix 时如果拿 repo_root 当 cwd，
        # 在子目录启动的会话会莫名其妙地“跳”到仓库根，指纹也跟着抖。
        self.invocation_cwd = str(invocation_cwd or cwd)
        self.cwd = str(cwd)
        self.repo_root = str(repo_root)
        self.branch = branch
        self.default_branch = default_branch
        self.status = status
        self.recent_commits = recent_commits
        self.project_docs = project_docs
        # 仓库地图渲染后的文本。挂在 workspace 段（prefix 尾部）而不是稳定头：
        # 它与任务无关但会随仓库结构变化，放进 stable_hash 覆盖的段落会白白打掉
        # prompt 缓存。默认空字符串 —— MOSS_REPO_MAP=off 时行为与加地图前完全一致。
        self.repo_map_text = ""
        self.status_entries = tuple(
            status_entries
            if status_entries is not None
            else [line for line in str(status).splitlines() if line.strip() and line != "clean"]
        )
        self.status_counts = dict(
            status_counts if status_counts is not None else parse_status_counts(self.status_entries)
        )
        # 直接构造（测试/评测）时没有 DocRef，就退化成对 preview 取 digest：
        # 指纹依然确定，只是失去了“全文变化必被发现”的保证。
        self.doc_refs = dict(doc_refs) if doc_refs is not None else {
            path: DocRef(
                path=path,
                preview=snippet,
                digest=hashlib.sha256(str(snippet).encode("utf-8")).hexdigest(),
                total_lines=len(str(snippet).splitlines()),
                truncated=False,
            )
            for path, snippet in (project_docs or {}).items()
        }

    @classmethod
    def build(cls, cwd, repo_root_override=None, *, git_facts=None, doc_names=None):
        cwd = Path(cwd).resolve()
        repo_root = (
            Path(repo_root_override).resolve()
            if repo_root_override is not None
            else _repo_identity(cwd)[0]
        )
        facts = git_facts if git_facts is not None else collect_git_facts(cwd)
        # 同时扫描 repo_root 和 cwd，这样在子目录启动时也能看到本地文档。
        doc_refs = discover_docs(repo_root, cwd, doc_names=doc_names or _configured_doc_names(repo_root))

        return cls(
            cwd=str(cwd),
            invocation_cwd=str(cwd),
            repo_root=str(repo_root),
            branch=facts.branch or "-",
            default_branch=facts.default_branch,
            status="\n".join(facts.status_entries) or "clean",
            status_entries=facts.status_entries,
            status_counts=facts.counts,
            recent_commits=list(facts.recent_commits),
            project_docs={key: ref.preview for key, ref in doc_refs.items()},
            doc_refs=doc_refs,
        )

    def status_text(self):
        """结构化的 status 段：先给规模，再给有限的样本路径。

        为什么不直接贴 `git status --short`：大改动下它能刷掉几十行，
        而模型真正需要的是“动了多少、动了哪些类别”，路径清单给前 N 条就够，
        剩下的它可以自己调 run_shell 看。
        """
        counts = self.status_counts or {}
        if not self.status_entries:
            return "clean"
        summary = " · ".join(
            f"{name}: {counts.get(name, 0)}"
            for name in ("modified", "added", "deleted", "untracked")
            if counts.get(name)
        )
        entries = sorted(self.status_entries)[:STATUS_ENTRY_LIMIT]
        lines = [summary] if summary else []
        lines.extend(entries)
        remaining = len(self.status_entries) - len(entries)
        if remaining > 0:
            lines.append(f"… {remaining} more")
        return "\n".join(lines)

    def text(self):
        # 这段文本会被塞进 prompt prefix，作为相对稳定的基线上下文。
        commits = "\n".join(f"- {line}" for line in self.recent_commits) or "- none"
        docs = "\n".join(f"- {path}\n{snippet}" for path, snippet in self.project_docs.items()) or "- none"
        repo_map = f"\n\n{self.repo_map_text}" if self.repo_map_text else ""
        return textwrap.dedent(
            f"""\
            Workspace:
            - cwd: {self.cwd}
            - repo_root: {self.repo_root}
            - branch: {self.branch}
            - default_branch: {self.default_branch}
            - status:
            {self.status_text()}
            - recent_commits:
            {commits}
            - project_docs:
            {docs}
            """
        ).strip() + repo_map

    def fingerprint(self):
        # 这个指纹用来判断仓库状态是否发生了足够大的变化，
        # 从而决定是否需要重建缓存中的 prompt prefix。
        # 注意：文档取的是**全文** digest 而不是被裁剪的 preview，
        # 否则第 1200 字符之后的改动永远不会让缓存失效。
        payload = {
            "cwd": self.invocation_cwd,
            "repo_root": self.repo_root,
            "branch": self.branch,
            "default_branch": self.default_branch,
            "status_counts": dict(self.status_counts),
            "status_entries": list(self.status_entries),
            "recent_commits": list(self.recent_commits),
            "project_docs": {path: ref.digest for path, ref in sorted(self.doc_refs.items())},
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
        return f"{WORKSPACE_FINGERPRINT_VERSION}:{digest}"
