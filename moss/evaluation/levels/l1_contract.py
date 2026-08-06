"""L1 合同回归入口；scripted 输出只证明 harness 合同。"""


def run_contract_smoke(**kwargs):
    # 延迟导入避免 evaluator 的兼容入口与本模块形成导入环。
    from ..evaluator import run_fixed_benchmark

    return run_fixed_benchmark(eval_level="L1", suite="contract-smoke", **kwargs)
