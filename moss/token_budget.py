"""Token 计量与按预算截断。

为什么存在：
字符数不是模型真正的约束——上下文窗口和费用都按 token 计。而且 CJK
（中文、日文、韩文、全角符号）在主流 BPE 分词里的 token 密度远高于
英文：英文经验值约 4 字符/token，中文接近 1 字符/token。用字符数当预算，
会系统性低估中文内容的真实开销。

这个模块提供两样东西：
- `estimate_tokens`：一个便宜、零依赖、对 CJK 友好的 token 估算；
- `clip_to_budget`：按“某种计量的预算”而不是死板的字符数截断文本，
  并且可以选择保留开头 / 结尾 / 两端。
"""

from __future__ import annotations

# CJK 及全角区段：这些字符在主流分词里通常 1 字符 >= 1 token。
_CJK_RANGES = (
    (0x3000, 0x303F),   # CJK 标点
    (0x3040, 0x30FF),   # 平假名 / 片假名
    (0x3400, 0x4DBF),   # CJK 扩展 A
    (0x4E00, 0x9FFF),   # CJK 统一表意文字
    (0xA000, 0xA4CF),   # 彝文音节等
    (0xAC00, 0xD7AF),   # 谚文音节
    (0xF900, 0xFAFF),   # CJK 兼容表意文字
    (0xFF00, 0xFFEF),   # 全角与半角变体
)

# 英文/拉丁：经验值约 4 字符 ≈ 1 token。
_LATIN_CHARS_PER_TOKEN = 4


def _is_cjk(ch):
    code = ord(ch)
    for low, high in _CJK_RANGES:
        if low <= code <= high:
            return True
    return False


def estimate_tokens(text):
    """估算文本的 token 数。

    刻意保持简单、零依赖：CJK/全角字符按 1 token 计，其余字符按约
    4 字符/token 计。它不追求和某个具体分词器逐 token 对齐，只求比
    “字符数”更接近真实成本——尤其在中英混排时不再系统性低估中文。
    非空文本至少记 1 token。
    """
    text = str(text)
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        else:
            other += 1
    latin_tokens = (other + _LATIN_CHARS_PER_TOKEN - 1) // _LATIN_CHARS_PER_TOKEN
    return max(1, cjk + latin_tokens)


def clip_to_budget(text, limit, measure=len, keep="head", marker="..."):
    """把 text 截断到 `measure(result) <= limit`，可选择保留哪一端。

    keep="head"   → 保留开头（读代码时 import/签名在前，信息在头部）。
    keep="tail"   → 保留结尾（报错/stack trace 的关键信息通常在末尾）。
    keep="middle" → 两端都留、砍中间（shell 输出的 exit_code 在顶部、
                    stderr 在底部，两端都值得保留）。

    `measure` 是计量函数：传 `len` 就是按字符预算，传 `estimate_tokens`
    就是按 token 预算。因为 `measure(candidate)` 对“保留的字符数”单调
    不减，这里用二分找出能塞进预算的最大截断。
    """
    text = str(text)
    if limit <= 0:
        return ""
    if measure(text) <= limit:
        return text
    if measure(marker) >= limit:
        # 连截断标记本身都放不下，就退回无标记的硬截断。
        marker = ""
    if keep not in ("head", "tail", "middle"):
        keep = "head"

    def candidate(keep_chars):
        if keep_chars <= 0:
            return marker
        if keep == "tail":
            return marker + text[-keep_chars:]
        if keep == "middle":
            left = keep_chars // 2
            right = keep_chars - left
            tail = text[-right:] if right else ""
            return text[:left] + marker + tail
        return text[:keep_chars] + marker

    lo, hi = 0, len(text)
    best = candidate(0)
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = candidate(mid)
        if measure(cand) <= limit:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    return best
