"""run 租约（spec-07 §4.4）。

核心回归：**持有有效租约的 run 不能被第二个进程标成 interrupted**。
那是 spec-07 §1 里的 bug #2——并发下的静默数据损坏。
"""

import json
import os
from datetime import datetime, timedelta, timezone

from moss.runs.lease import (
    DEFAULT_TTL_S,
    TAKEOVER_INTERRUPTED,
    TAKEOVER_STALE,
    LeaseHeartbeat,
    RunLease,
    age_seconds,
)
from moss.runs.store import RunStore
from moss.agent.state import STATUS_FAILED, STATUS_RUNNING, TaskState


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _running(store, run_id):
    state = TaskState.create(run_id=run_id, task_id="t_" + run_id, user_request="x")
    store.start_run(state)
    return state


def test_active_lease_protects_a_concurrently_running_run(tmp_path):
    """另一个终端正在跑的 run，绝不能被这个进程判死并覆写成 failed。"""
    root = tmp_path / "runs"
    owner = RunStore(root)
    state = _running(owner, "run_live")

    # 第二个进程：同一个 runs 目录，独立的 RunStore 实例。
    newcomer = RunStore(root)
    taken = newcomer.mark_interrupted_runs()

    assert taken == []
    assert newcomer.load_task_state(state.run_id)["status"] == STATUS_RUNNING
    assert owner.lease.path(state.run_id).exists()


def test_dead_pid_lease_can_be_taken_over_as_interrupted(tmp_path):
    store = RunStore(tmp_path / "runs")
    state = _running(store, "run_dead")
    # 进程不在了。owner_pid 换成一个不属于本进程的号，并让探测判死。
    lease = json.loads(store.lease.path(state.run_id).read_text(encoding="utf-8"))
    lease["owner_pid"] = os.getpid() + 100000
    store.lease.path(state.run_id).write_text(json.dumps(lease), encoding="utf-8")
    store.lease._probe = lambda pid: False

    taken = store.mark_interrupted_runs()

    assert [item["run_id"] for item in taken] == ["run_dead"]
    assert taken[0]["takeover"] == TAKEOVER_INTERRUPTED
    assert store.load_task_state(state.run_id)["status"] == STATUS_FAILED
    # 接管之后租约必须清掉，否则下次启动还会看到一份过期证明。
    assert not store.lease.path(state.run_id).exists()


def test_hung_process_lease_expires_by_ttl(tmp_path):
    store = RunStore(tmp_path / "runs")
    state = _running(store, "run_hung")
    lease = json.loads(store.lease.path(state.run_id).read_text(encoding="utf-8"))
    lease["owner_pid"] = os.getpid() + 100000
    lease["heartbeat_at"] = _ago(DEFAULT_TTL_S * 3)
    store.lease.path(state.run_id).write_text(json.dumps(lease), encoding="utf-8")
    # 进程还在（探测判活），但心跳停了很久 —— hang 住，可以接管。
    store.lease._probe = lambda pid: True

    taken = store.mark_interrupted_runs()

    assert [item["takeover"] for item in taken] == [TAKEOVER_INTERRUPTED]


def test_fresh_heartbeat_from_another_pid_is_still_alive(tmp_path):
    store = RunStore(tmp_path / "runs")
    state = _running(store, "run_other_pid")
    lease = json.loads(store.lease.path(state.run_id).read_text(encoding="utf-8"))
    lease["owner_pid"] = os.getpid() + 100000
    lease["heartbeat_at"] = _ago(1)
    store.lease.path(state.run_id).write_text(json.dumps(lease), encoding="utf-8")
    store.lease._probe = lambda pid: True

    assert store.mark_interrupted_runs() == []


def test_run_without_lease_is_classified_stale_not_interrupted(tmp_path):
    """旧版本留下的 run 没有租约。可以接管，但不能计进"中断"统计。"""
    store = RunStore(tmp_path / "runs")
    state = _running(store, "run_legacy")
    store.lease.path(state.run_id).unlink()

    taken = store.mark_interrupted_runs()

    assert [item["takeover"] for item in taken] == [TAKEOVER_STALE]
    assert store.load_report(state.run_id)["takeover"] == TAKEOVER_STALE


def test_machine_reboot_kills_the_lease(tmp_path):
    store = RunStore(tmp_path / "runs")
    state = _running(store, "run_reboot")
    lease = json.loads(store.lease.path(state.run_id).read_text(encoding="utf-8"))
    lease["owner_pid"] = os.getpid() + 100000
    lease["boot_id"] = "old-boot"
    store.lease.path(state.run_id).write_text(json.dumps(lease), encoding="utf-8")
    store.lease._boot_id = "new-boot"
    # 探测说"活着"（PID 被复用了），但机器重启过，那个 PID 与原 run 无关。
    store.lease._probe = lambda pid: True

    assert store.lease.is_alive(store.lease.read(state.run_id)) is False


def test_remote_host_lease_only_uses_ttl(tmp_path):
    store = RunStore(tmp_path / "runs")
    state = _running(store, "run_remote")
    lease = json.loads(store.lease.path(state.run_id).read_text(encoding="utf-8"))
    lease["owner_pid"] = os.getpid() + 100000
    lease["host"] = "some-other-host"
    lease["heartbeat_at"] = _ago(5)
    store.lease.path(state.run_id).write_text(json.dumps(lease), encoding="utf-8")
    # 跨机器探不到进程：新鲜心跳判活。
    assert store.lease.is_alive(store.lease.read(state.run_id)) is True

    lease["heartbeat_at"] = _ago(DEFAULT_TTL_S * 2)
    store.lease.path(state.run_id).write_text(json.dumps(lease), encoding="utf-8")
    assert store.lease.is_alive(store.lease.read(state.run_id)) is False


def test_unparseable_lease_is_treated_as_missing(tmp_path):
    store = RunStore(tmp_path / "runs")
    state = _running(store, "run_corrupt")
    store.lease.path(state.run_id).write_text("{not json", encoding="utf-8")

    can_take_over, takeover = store.lease.classify(state.run_id)

    assert can_take_over is True
    assert takeover == TAKEOVER_STALE


def test_heartbeat_refreshes_timestamp_and_recreates_missing_lease(tmp_path):
    store = RunStore(tmp_path / "runs")
    state = _running(store, "run_hb")
    before = store.lease.read(state.run_id)["heartbeat_at"]

    store.lease.path(state.run_id).unlink()
    recreated = store.heartbeat(state.run_id)

    assert store.lease.path(state.run_id).exists()
    assert recreated["owner_pid"] == os.getpid()
    assert age_seconds(before) >= age_seconds(recreated["heartbeat_at"])


def test_release_is_idempotent_and_never_raises(tmp_path):
    store = RunStore(tmp_path / "runs")
    state = _running(store, "run_release")

    assert store.release_run(state.run_id) is True
    assert store.release_run(state.run_id) is False


def test_active_runs_lists_only_live_leases(tmp_path):
    store = RunStore(tmp_path / "runs")
    live = _running(store, "run_a")
    dead = _running(store, "run_b")
    store.lease.path(dead.run_id).unlink()

    assert store.active_runs() == [live.run_id]


def test_lease_heartbeat_thread_refreshes_periodically():
    ticks = []
    heartbeat = LeaseHeartbeat(lambda: ticks.append(1), interval_s=0.01)
    heartbeat.start()
    deadline = datetime.now(timezone.utc) + timedelta(seconds=2)
    while not ticks and datetime.now(timezone.utc) < deadline:
        pass
    heartbeat.stop()

    assert ticks


def test_lease_heartbeat_swallows_refresh_errors():
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("nope")

    heartbeat = LeaseHeartbeat(boom, interval_s=0.01)
    heartbeat.start()
    deadline = datetime.now(timezone.utc) + timedelta(seconds=2)
    while not calls and datetime.now(timezone.utc) < deadline:
        pass
    heartbeat.stop()

    assert calls


def _agent(tmp_path, outputs):
    from moss import FakeModelClient, Moss, SessionStore, WorkspaceContext

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Moss(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".moss" / "sessions"),
        approval_policy="auto",
    )


def test_agent_run_acquires_and_releases_the_lease(tmp_path):
    agent = _agent(tmp_path, ["<final>done</final>"])
    seen = []
    original = agent.run_store.lease.acquire
    agent.run_store.lease.acquire = lambda run_id: seen.append(run_id) or original(run_id)

    agent.ask("hello")

    assert seen == [agent.current_task_state.run_id]
    # 收尾必须释放：留着一份过期租约会让下次启动误以为它还在跑。
    assert not agent.run_store.lease.path(agent.current_task_state.run_id).exists()


def test_interrupted_run_still_releases_the_lease(tmp_path):
    class Boom:
        model = "boom"
        provider = "test"

        def complete(self, *args, **kwargs):
            raise KeyboardInterrupt()

    agent = _agent(tmp_path, [])
    agent.model_client = Boom()

    try:
        agent.ask("hello")
    except KeyboardInterrupt:
        pass

    run_id = agent.current_task_state.run_id
    assert not agent.run_store.lease.path(run_id).exists()


def test_lease_uses_injected_clock_and_dir_resolver(tmp_path):
    lease = RunLease(lambda run_id: tmp_path / str(run_id), clock=lambda: "2026-01-01T00:00:00+00:00")

    payload = lease.acquire("r1")

    assert payload["started_at"] == "2026-01-01T00:00:00+00:00"
    assert (tmp_path / "r1" / "lease.json").exists()
