"""run 租约：判断一个 `status=running` 的 run 目录是不是真的还活着。

为什么存在：
启动时把所有 `status=running` 的 run 一律标成 interrupted，在并发下是**静默数据
损坏**——一个终端开着 REPL 正在跑，另一个终端起一次 `moss`，前一个正在进行的
run 就被判死，并被覆写上 failed 的 task_state 和 report。用户看到的是"我的任务
无缘无故失败了"，而 trace 里根本没有对应的失败原因。

租约就是那份"我还活着"的证明：run 开始时写 `lease.json`（PID + 主机 + boot_id +
心跳），主循环每步刷新心跳，收尾（含中断路径）时删除。别的进程要接管一个 run
之前，先按这份证明判断它是死是活。

判定链刻意**保守**：探测不到就当活的。最坏结果是"该接管的没接管"（用户下次再
看到一个 running 的旧 run），而不是"误杀一个正在跑的 run"。
"""

import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

from ..clock import now

# 心跳超时。90s 是这么来的：主循环每步刷新一次，而单步里最慢的是模型调用
# （默认超时 300s）——所以工具/模型执行期间由独立心跳线程续租，主循环的步间隔
# 不会接近这个值。真正超过 90s 没心跳，基本就是进程 hang 或被 SIGKILL 了。
DEFAULT_TTL_S = 90

LEASE_FILENAME = "lease.json"

# 接管一个 run 时的两种结论。分开统计，不混算：
# interrupted = 有租约且判死，确认是"跑到一半没了"；
# stale = 压根没有租约文件（旧版本留下的，或写租约之前就崩了），只能说"状态不明"。
TAKEOVER_INTERRUPTED = "interrupted"
TAKEOVER_STALE = "stale"


def host_name():
    try:
        return socket.gethostname()
    except OSError:
        return ""


def boot_id():
    """本次开机的标识。拿不到就返回空串（空串 = 不参与判定）。

    为什么需要它：机器重启之后 PID 会被重新分配，`os.kill(pid, 0)` 探测到的
    可能是一个完全无关的新进程——那样一个早就死掉的 run 会被永远判活。
    """
    path = Path("/proc/sys/kernel/random/boot_id")
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def pid_alive(pid):
    """PID 对应的进程还在不在。探测不出来时一律返回 True（保守方向）。"""
    pid = int(pid or 0)
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在，只是不属于当前用户。存在就算活。
        return True
    except OSError:
        return True
    return True


def _parse_timestamp(text):
    try:
        parsed = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def age_seconds(timestamp, reference=None):
    """时间戳距今多少秒。解析不出来返回 None（= 无法判断，按保守处理）。"""
    parsed = _parse_timestamp(timestamp)
    if parsed is None:
        return None
    base = _parse_timestamp(reference) if reference else datetime.now(timezone.utc)
    if base is None:
        base = datetime.now(timezone.utc)
    return (base - parsed).total_seconds()


class RunLease:
    """一个 run 目录上的租约文件读写与存活判定。

    在链路里的位置：`RunStore` 持有它，`start_run` 时 acquire、主循环每步
    heartbeat、收尾时 release；`mark_interrupted_runs` 靠 `is_alive` 决定
    要不要接管别的进程留下的 run。
    """

    def __init__(self, run_dir_for, ttl_s=DEFAULT_TTL_S, clock=now, probe=pid_alive):
        # run_dir_for 是一个 run_id -> Path 的函数，避免 lease 反过来依赖 RunStore。
        self._run_dir_for = run_dir_for
        self.ttl_s = int(ttl_s or DEFAULT_TTL_S)
        self._clock = clock
        self._probe = probe
        self._host = host_name()
        self._boot_id = boot_id()

    def path(self, run_id):
        return Path(self._run_dir_for(run_id)) / LEASE_FILENAME

    def acquire(self, run_id):
        timestamp = self._clock()
        payload = {
            "owner_pid": os.getpid(),
            "host": self._host,
            "boot_id": self._boot_id,
            "started_at": timestamp,
            "heartbeat_at": timestamp,
            "ttl_s": self.ttl_s,
        }
        self._write(run_id, payload)
        return payload

    def heartbeat(self, run_id):
        """刷新心跳。租约不存在（被别人接管过）时重新 acquire，不抛异常。

        为什么不抛：心跳是旁路，它失败不该把一次正在进行的任务带走。
        """
        payload = self.read(run_id)
        if not payload or int(payload.get("owner_pid", 0) or 0) != os.getpid():
            return self.acquire(run_id)
        payload["heartbeat_at"] = self._clock()
        payload["ttl_s"] = self.ttl_s
        self._write(run_id, payload)
        return payload

    def release(self, run_id):
        try:
            self.path(run_id).unlink()
        except OSError:
            # 收尾路径（含中断）必须绝不抛异常：删不掉最多是留下一份过期租约，
            # 下次启动会按 TTL 判死并接管。
            return False
        return True

    def read(self, run_id):
        path = self.path(run_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def is_alive(self, lease):
        """租约描述的进程还在不在。判不出来一律算活着。"""
        if not lease:
            return False
        if int(lease.get("owner_pid", 0) or 0) == os.getpid():
            # 自己的租约。同一进程内的 delegate 子 agent 共用 run_store，
            # 绝不能把自己正在跑的 run 判死。
            return True
        ttl = int(lease.get("ttl_s", self.ttl_s) or self.ttl_s)
        elapsed = age_seconds(lease.get("heartbeat_at"))
        host = str(lease.get("host", "") or "")
        if host and self._host and host != self._host:
            # 跨机器无法探测进程，只能看 TTL。
            return elapsed is None or elapsed <= ttl
        recorded_boot = str(lease.get("boot_id", "") or "")
        if recorded_boot and self._boot_id and recorded_boot != self._boot_id:
            # 同一台机器但重启过：那个 PID 早就没了，现在同号的进程与它无关。
            return False
        if not self._probe(lease.get("owner_pid")):
            return False
        # 进程还在，但心跳停了很久 —— 大概率是 hang 住。
        if elapsed is not None and elapsed > ttl:
            return False
        return True

    def classify(self, run_id):
        """返回 (是否可以接管, 接管类别)。"""
        lease = self.read(run_id)
        if lease is None:
            # 没有租约文件：旧版本的 run，或写租约之前就崩了。可以接管，
            # 但不能当成"确认中断"计入统计——我们并不知道它是怎么没的。
            return True, TAKEOVER_STALE
        if self.is_alive(lease):
            return False, ""
        return True, TAKEOVER_INTERRUPTED

    def _write(self, run_id, payload):
        from ..atomic_io import write_json_atomic

        path = self.path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, payload)
        return path


class LeaseHeartbeat:
    """后台续租线程。

    为什么必须独立于主循环：一步里最慢的动作是模型调用（默认超时 300s）和
    大 pytest，主循环在那期间根本回不到步边界。只靠步边界刷新心跳的话，
    一次长工具就能让租约过期，另一个进程于是把这个**正在跑**的 run 判死。

    刻意做成"绝不抛异常"：它是旁路，写不动租约文件也不该把任务带走。
    """

    def __init__(self, refresh, interval_s=None, ttl_s=DEFAULT_TTL_S):
        import threading

        # 间隔取 TTL 的三分之一：丢一两次心跳也还在 TTL 之内。
        self.interval_s = float(interval_s or max(1.0, int(ttl_s or DEFAULT_TTL_S) / 3.0))
        self._refresh = refresh
        self._stop = threading.Event()
        self._thread = None
        self._threading = threading

    def start(self):
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = self._threading.Thread(target=self._loop, name="moss-lease", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=1.0)
        return self

    def _loop(self):
        while not self._stop.wait(self.interval_s):
            try:
                self._refresh()
            except Exception:
                # 心跳失败不影响控制流；最坏结果是租约过期被别人接管。
                pass
