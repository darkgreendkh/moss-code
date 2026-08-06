"""依赖 stdlib 的评测统计量；所有随机过程都可用 seed 复核。"""

import math
import random
from statistics import NormalDist


def wilson_interval(successes, n, confidence=0.95):
    successes = int(successes)
    n = int(n)
    confidence = float(confidence)
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= successes <= n:
        raise ValueError("successes must be between zero and n")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between zero and one")
    z = NormalDist().inv_cdf(0.5 + confidence / 2.0)
    proportion = successes / n
    denominator = 1.0 + (z * z / n)
    center = (proportion + z * z / (2.0 * n)) / denominator
    margin = (
        z
        * math.sqrt(proportion * (1.0 - proportion) / n + z * z / (4.0 * n * n))
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def _validate_combination_inputs(n, c, k):
    n, c, k = int(n), int(c), int(k)
    if n < 0 or not 0 <= c <= n or not 1 <= k <= n:
        raise ValueError("require n >= 1, 0 <= c <= n, and 1 <= k <= n")
    return n, c, k


def pass_hat_k(n, c, k):
    """在 n 次采样中恰有 c 次成功时，随机取 k 次全部成功的组合估计。"""
    n, c, k = _validate_combination_inputs(n, c, k)
    return 0.0 if c < k else math.comb(c, k) / math.comb(n, k)


def success_at_k(n, c, k):
    """在 n 次采样中恰有 c 次成功时，随机取 k 次至少一次成功的组合估计。"""
    n, c, k = _validate_combination_inputs(n, c, k)
    failures = n - c
    return 1.0 if failures < k else 1.0 - math.comb(failures, k) / math.comb(n, k)


def _percentile(values, probability):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("cannot compute an interval from no samples")
    position = (len(ordered) - 1) * float(probability)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _cluster_value(row, key):
    if isinstance(key, (tuple, list)):
        return tuple(row[item] for item in key)
    return row[key]


def _hierarchical_sample(rows, keys, rng):
    if not keys:
        return list(rows)
    groups = {}
    key = keys[0]
    for row in rows:
        groups.setdefault(row[key], []).append(row)
    labels = list(groups)
    sampled = []
    for label in rng.choices(labels, k=len(labels)):
        sampled.extend(_hierarchical_sample(groups[label], keys[1:], rng))
    return sampled


def cluster_bootstrap(rows, statistic, *, cluster_key, iters=5000, seed=0):
    rows = list(rows)
    iters = int(iters)
    if not rows or iters <= 0:
        raise ValueError("rows and iters must be non-empty and positive")
    keys = tuple(cluster_key) if isinstance(cluster_key, (tuple, list)) else (cluster_key,)
    rng = random.Random(seed)
    samples = [float(statistic(_hierarchical_sample(rows, keys, rng))) for _ in range(iters)]
    return {
        "estimate": float(statistic(rows)),
        "ci_low": _percentile(samples, 0.025),
        "ci_high": _percentile(samples, 0.975),
        "confidence": 0.95,
        "n": len(rows),
        "iters": iters,
    }


def paired_bootstrap(
    rows_a,
    rows_b,
    *,
    pair_key,
    iters=5000,
    seed=0,
    value_key="passed",
    statistic=None,
):
    rows_a = list(rows_a)
    rows_b = list(rows_b)
    by_a = {_cluster_value(row, pair_key): row for row in rows_a}
    by_b = {_cluster_value(row, pair_key): row for row in rows_b}
    pair_ids = sorted(set(by_a) & set(by_b), key=repr)
    if not pair_ids or int(iters) <= 0:
        raise ValueError("paired bootstrap requires shared pairs and positive iters")

    def delta(ids):
        left = [by_a[pair_id] for pair_id in ids]
        right = [by_b[pair_id] for pair_id in ids]
        if statistic is not None:
            return float(statistic(left, right))
        left_mean = sum(float(row[value_key]) for row in left) / len(left)
        right_mean = sum(float(row[value_key]) for row in right) / len(right)
        return right_mean - left_mean

    rng = random.Random(seed)
    samples = [delta(rng.choices(pair_ids, k=len(pair_ids))) for _ in range(int(iters))]
    return {
        "estimate": delta(pair_ids),
        "ci_low": _percentile(samples, 0.025),
        "ci_high": _percentile(samples, 0.975),
        "confidence": 0.95,
        "n": len(pair_ids),
        "iters": int(iters),
    }


def rule_of_three(n):
    n = int(n)
    if n <= 0:
        raise ValueError("n must be positive")
    return 3.0 / n
