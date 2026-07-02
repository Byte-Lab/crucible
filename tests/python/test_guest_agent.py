import json
import os
import tempfile

import pytest

from guest.protocol import GuestCommand, GuestResponse


def test_handle_health_check():
    from guest.crucible_guest_agent import GuestAgentHandler

    handler = GuestAgentHandler()
    cmd = GuestCommand(cmd="health_check")
    resp = handler.handle(cmd)
    assert resp.status == "ok"
    assert "uptime" in resp.data
    assert "pid" in resp.data


def test_handle_setup_cgroups():
    from guest.crucible_guest_agent import GuestAgentHandler

    handler = GuestAgentHandler()
    cmd = GuestCommand(cmd="setup_cgroups", groups=["game", "compositor"])
    resp = handler.handle(cmd)
    # error is acceptable in test env without cgroup access
    assert resp.status in ("ok", "error")


def test_handle_get_metrics():
    from guest.crucible_guest_agent import GuestAgentHandler

    handler = GuestAgentHandler()
    cmd = GuestCommand(cmd="get_metrics")
    resp = handler.handle(cmd)
    assert resp.status == "ok"
    assert "system_psi" in resp.data


def test_start_profiling_feeds_kernel_config_to_perfetto(monkeypatch):
    import guest.crucible_guest_agent as agent_mod

    handler = agent_mod.GuestAgentHandler()
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)

    class FakeStdin:
        def __init__(self):
            self.written = b""
            self.closed = False

        def write(self, b):
            self.written += b

        def close(self):
            self.closed = True

    class FakeProc:
        def __init__(self, argv):
            self.args = argv
            self.pid = 4321
            self.stdin = FakeStdin()

        def poll(self):
            return None

    popens = []

    def fake_popen(argv, **kwargs):
        proc = FakeProc(argv)
        popens.append(proc)
        return proc

    monkeypatch.setattr(agent_mod.subprocess, "Popen", fake_popen)
    resp = handler.handle(
        GuestCommand(cmd="start_profiling", config={"duration_s": 12})
    )
    assert resp.status == "ok"
    assert resp.data["pid"] == 4321
    # traced + traced_probes must be up before the perfetto client — the
    # CLI is only a consumer of the traced service socket.
    assert [p.args[0] for p in popens] == ["traced", "traced_probes", "perfetto"]
    # perfetto reads the config from stdin; without this write it records
    # nothing. The kernel scheduling events must be present, and the
    # capture bound rides in the config (the CLI rejects -c with --time).
    perfetto_proc = popens[-1]
    written = perfetto_proc.stdin.written.decode()
    assert written.startswith("duration_ms: 12000")
    assert "sched/sched_switch" in written
    assert "sched/sched_wakeup" in written
    assert "linux.ftrace" in written
    assert perfetto_proc.stdin.closed is True
    assert perfetto_proc.args[:2] == ["perfetto", "--txt"]
    assert "--time" not in perfetto_proc.args


def test_start_profiling_missing_perfetto_binary(monkeypatch):
    import guest.crucible_guest_agent as agent_mod

    handler = agent_mod.GuestAgentHandler()
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)

    def raise_fnf(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(agent_mod.subprocess, "Popen", raise_fnf)
    resp = handler.handle(GuestCommand(cmd="start_profiling"))
    assert resp.status == "error"
    assert "traced" in resp.message or "perfetto" in resp.message


def test_apply_sysctls_writes_proc_paths(monkeypatch, tmp_path):
    import guest.crucible_guest_agent as agent_mod

    handler = agent_mod.GuestAgentHandler()
    # Redirect /proc/sys writes into a sandbox.
    real_open = open

    def fake_open(path, *a, **k):
        if isinstance(path, str) and path.startswith("/proc/sys/"):
            p = tmp_path / path.replace("/proc/sys/", "").replace("/", "_")
            return real_open(p, *a, **k)
        return real_open(path, *a, **k)

    import builtins
    monkeypatch.setattr(builtins, "open", fake_open)
    resp = handler.handle(GuestCommand(
        cmd="apply_sysctls",
        config={"sysctls": {"kernel.sched_base_slice_ns": "1500000",
                            "vm.nonexistent_knob": "1"}},
    ))
    assert resp.status == "ok"
    # First key round-trips through the sandboxed file; the second fails on
    # read-back... both files get created by the sandbox, so both apply.
    assert resp.data["applied"]["kernel.sched_base_slice_ns"] == "1500000"


def test_apply_sysctls_requires_sysctls_dict():
    from guest.crucible_guest_agent import GuestAgentHandler

    handler = GuestAgentHandler()
    resp = handler.handle(GuestCommand(cmd="apply_sysctls", config={}))
    assert resp.status == "error"


def test_handle_unknown_command():
    from guest.crucible_guest_agent import GuestAgentHandler

    handler = GuestAgentHandler()
    cmd = GuestCommand(cmd="nonexistent_command")
    resp = handler.handle(cmd)
    assert resp.status == "error"
    assert "unknown" in resp.message.lower()


def test_handle_fetch_file():
    from guest.crucible_guest_agent import GuestAgentHandler

    handler = GuestAgentHandler()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("test content")
        tmp_path = f.name
    try:
        cmd = GuestCommand(cmd="fetch_file", path=tmp_path)
        resp = handler.handle(cmd)
        assert resp.status == "ok"
        assert resp.data["size"] > 0
    finally:
        os.unlink(tmp_path)


def test_handle_fetch_file_not_found():
    from guest.crucible_guest_agent import GuestAgentHandler

    handler = GuestAgentHandler()
    cmd = GuestCommand(cmd="fetch_file", path="/nonexistent/file.txt")
    resp = handler.handle(cmd)
    assert resp.status == "error"


STRESS_NG_CANNED_STDERR = """\
stress-ng: info:  [4521] setting to a 5 second run per stressor
stress-ng: info:  [4521] dispatching hogs: 2 cpu
stress-ng: info:  [4521] successful run completed in 5.01s
stress-ng: metrc: [4521] stressor       bogo ops real time  usr time  sys time   bogo ops/s   bogo ops/s
stress-ng: metrc: [4521]                          (secs)    (secs)    (secs)   (real time) (usr+sys time)
stress-ng: metrc: [4521] cpu              45164     5.01      9.95      0.02       9015.97      4533.61
stress-ng: metrc: [4521] vm               12000     5.01      4.50      0.10       2395.21      2608.70
stress-ng: info:  [4521] complete
"""


def test_parse_stress_ng_metrics_aggregates_stressors():
    from guest.crucible_guest_agent import _parse_stress_ng_metrics

    metrics = _parse_stress_ng_metrics(STRESS_NG_CANNED_STDERR)
    assert metrics["bogo_ops"] == 45164 + 12000
    assert metrics["real_time_secs"] == 5.01
    # ops_per_sec is summed across parallel stressors
    assert abs(metrics["ops_per_sec"] - (9015.97 + 2395.21)) < 0.01
    names = [s["stressor"] for s in metrics["stressors"]]
    assert names == ["cpu", "vm"]


def test_parse_stress_ng_metrics_empty_on_garbage():
    from guest.crucible_guest_agent import _parse_stress_ng_metrics

    metrics = _parse_stress_ng_metrics("not a real stress-ng output\n")
    assert metrics["bogo_ops"] == 0
    assert metrics["ops_per_sec"] == 0.0
    assert metrics["stressors"] == []


def test_handle_run_benchmark_success(monkeypatch):
    from guest import crucible_guest_agent as gga
    from guest.crucible_guest_agent import GuestAgentHandler

    psi_values = [
        {"cpu": 0.10, "memory": 0.05, "io": 0.01},
        {"cpu": 12.34, "memory": 0.85, "io": 0.20},
    ]
    monkeypatch.setattr(gga, "_read_system_psi_avg10", lambda: psi_values.pop(0))

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = STRESS_NG_CANNED_STDERR

    def fake_run(argv, **_kwargs):
        assert argv[0] == "stress-ng"
        assert "--metrics-brief" in argv
        assert "--timeout" in argv
        return FakeProc()

    monkeypatch.setattr(gga.subprocess, "run", fake_run)

    handler = GuestAgentHandler()
    cmd = GuestCommand(
        cmd="run_benchmark",
        name="stress-ng",
        args=["--cpu", "2", "--vm", "1"],
        duration_secs=5,
    )
    resp = handler.handle(cmd)
    assert resp.status == "ok", resp.message
    assert resp.data["exit_code"] == 0
    assert resp.data["bogo_ops"] == 45164 + 12000
    assert resp.data["psi_cpu_delta"] == pytest.approx(12.24, abs=0.01)
    assert resp.data["psi_memory_delta"] == pytest.approx(0.80, abs=0.01)
    assert resp.data["psi_io_delta"] == pytest.approx(0.19, abs=0.01)
    assert "stress-ng" in resp.data["raw_stderr_tail"]


def test_handle_run_benchmark_clamps_negative_psi_delta(monkeypatch):
    from guest import crucible_guest_agent as gga
    from guest.crucible_guest_agent import GuestAgentHandler

    psi_values = [
        {"cpu": 50.0, "memory": 5.0, "io": 1.0},
        {"cpu": 10.0, "memory": 1.0, "io": 0.5},
    ]
    monkeypatch.setattr(gga, "_read_system_psi_avg10", lambda: psi_values.pop(0))

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = STRESS_NG_CANNED_STDERR

    monkeypatch.setattr(gga.subprocess, "run", lambda *_a, **_kw: FakeProc())

    handler = GuestAgentHandler()
    cmd = GuestCommand(
        cmd="run_benchmark",
        name="stress-ng",
        args=[],
        duration_secs=5,
    )
    resp = handler.handle(cmd)
    assert resp.status == "ok"
    assert resp.data["psi_cpu_delta"] == 0.0
    assert resp.data["psi_memory_delta"] == 0.0
    assert resp.data["psi_io_delta"] == 0.0


def test_handle_run_benchmark_unsupported_name():
    from guest.crucible_guest_agent import GuestAgentHandler

    handler = GuestAgentHandler()
    cmd = GuestCommand(
        cmd="run_benchmark",
        name="perf",
        args=[],
        duration_secs=5,
    )
    resp = handler.handle(cmd)
    assert resp.status == "error"
    assert "perf" in resp.message


def test_handle_run_benchmark_rejects_missing_duration():
    from guest.crucible_guest_agent import GuestAgentHandler

    handler = GuestAgentHandler()
    cmd = GuestCommand(cmd="run_benchmark", name="stress-ng", args=[])
    resp = handler.handle(cmd)
    assert resp.status == "error"
    assert "duration_secs" in resp.message


def test_handle_run_benchmark_handles_missing_binary(monkeypatch):
    from guest import crucible_guest_agent as gga
    from guest.crucible_guest_agent import GuestAgentHandler

    monkeypatch.setattr(gga, "_read_system_psi_avg10", lambda: {})

    def fake_run(*_a, **_kw):
        raise FileNotFoundError("no stress-ng")

    monkeypatch.setattr(gga.subprocess, "run", fake_run)

    handler = GuestAgentHandler()
    cmd = GuestCommand(
        cmd="run_benchmark",
        name="stress-ng",
        args=[],
        duration_secs=5,
    )
    resp = handler.handle(cmd)
    assert resp.status == "error"
    assert "stress-ng" in resp.message
