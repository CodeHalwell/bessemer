"""Automatic CPU sizing uses the tightest visible process/container limits."""

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from bessemer._cpu import cpu_budget
from bessemer.config import DEFAULTS
from bessemer.supervisor import Supervisor, cpu_quota_count


class CPUBudgetTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(directory) / "cgroup"
        self.root.mkdir()
        self.proc = Path(directory) / "proc"
        (self.proc / "self").mkdir(parents=True)
        self.write(self.proc / "self/cgroup", "")
        self.write(self.proc / "self/mountinfo", "")
        self.affinity = self.stack.enter_context(
            patch(
                "bessemer._cpu.os.sched_getaffinity",
                return_value=set(range(32)),
                create=True,
            )
        )
        self.process = self.stack.enter_context(
            patch("bessemer._cpu.os.process_cpu_count", return_value=32, create=True)
        )
        self.host = self.stack.enter_context(
            patch("bessemer._cpu.os.cpu_count", return_value=32)
        )

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def budget(self):
        return cpu_budget(self.root, proc_root=self.proc)

    def test_process_count_affinity_and_host_count_are_all_bounds(self):
        self.affinity.return_value = {0, 2, 4, 6}
        self.process.return_value = 3
        self.assertEqual(self.budget(), 3)
        self.process.return_value = None
        self.assertEqual(self.budget(), 4)
        self.affinity.side_effect = OSError("not available")
        self.process.return_value = 5
        self.assertEqual(self.budget(), 5)
        self.process.return_value = None
        self.host.return_value = 2
        self.assertEqual(self.budget(), 2)
        self.host.return_value = None
        self.assertEqual(self.budget(), 1)

    def test_v2_cpuset_intersects_affinity_and_visible_ancestors(self):
        self.affinity.return_value = {0, 2, 4, 6, 8, 10}
        self.write(self.proc / "self/cgroup", "0::/group/worker\n")
        self.write(self.root / "cpuset.cpus.effective", "0-10")
        self.write(self.root / "group/cpuset.cpus", "2-8")
        self.write(self.root / "group/worker/cpuset.cpus", "")
        self.write(self.root / "group/worker/cpuset.cpus.effective", "4-10")
        self.assertEqual(self.budget(), 3)  # CPUs 4, 6, 8.

    def test_v2_tightest_ancestor_quota_and_fractional_ceiling(self):
        self.write(self.proc / "self/cgroup", "0::/group/worker\n")
        self.write(self.root / "cpu.max", "400000 100000")
        self.write(self.root / "group/cpu.max", "150000 100000")
        self.write(self.root / "group/worker/cpu.max", "max 100000")
        self.assertEqual(self.budget(), 2)
        self.write(self.root / "group/worker/cpu.max", "25000 100000")
        self.assertEqual(self.budget(), 1)

    def test_nested_quota_requires_process_membership(self):
        self.write(self.root / "cpu.max", "400000 100000")
        self.write(self.root / "group/worker/cpu.max", "150000 100000")
        self.assertEqual(self.budget(), 4)
        self.write(self.proc / "self/cgroup", "0::/group/worker\n")
        self.assertEqual(self.budget(), 2)

    def test_v1_cpu_and_cpuset_memberships_can_differ(self):
        self.write(
            self.proc / "self/cgroup",
            "2:cpu,cpuacct:/cpu-group/worker\n3:cpuset:/set-group/worker\n",
        )
        self.write(self.root / "cpu,cpuacct/cpu-group/cpu.cfs_quota_us", "250000")
        self.write(self.root / "cpu,cpuacct/cpu-group/cpu.cfs_period_us", "100000")
        self.write(self.root / "cpuset/set-group/cpuset.cpus", "1,3,5,7")
        self.write(self.root / "cpuset/set-group/worker/cpuset.effective_cpus", "3,5")
        self.assertEqual(self.budget(), 2)

    def test_v2_delegated_mount_maps_membership_relative_to_mount_root(self):
        mounted = self.root / "delegated"
        self.write(self.proc / "self/cgroup", "0::/tenant/group/worker\n")
        self.write(
            self.proc / "self/mountinfo",
            f"31 20 0:27 /tenant {mounted} rw - cgroup2 cgroup rw\n",
        )
        self.write(mounted / "cpu.max", "150000 100000")
        self.write(mounted / "group/worker/cpuset.cpus.effective", "2-5")
        self.assertEqual(self.budget(), 2)

    def test_v1_custom_controller_mounts_and_escaped_mount_paths(self):
        mounted = self.root / "cpu controllers"
        escaped = str(mounted).replace(" ", r"\040")
        self.write(self.proc / "self/cgroup", "4:cpu,cpuacct:/tenant/worker\n")
        self.write(
            self.proc / "self/mountinfo",
            f"31 20 0:27 /tenant {escaped} rw - cgroup cgroup rw,cpu,cpuacct\n",
        )
        self.write(mounted / "cpu.cfs_quota_us", "225000")
        self.write(mounted / "cpu.cfs_period_us", "100000")
        self.assertEqual(self.budget(), 3)

    def test_malformed_memberships_limits_and_cpusets_do_not_hide_valid_limits(self):
        self.write(self.proc / "self/cgroup", "broken\n0::/group/worker\n")
        self.write(self.root / "cpu.max", "bad 100000")
        self.write(self.root / "group/cpu.max", "100000 100000")
        self.write(self.root / "group/worker/cpu.max", "200000 0")
        self.write(self.root / "group/worker/cpuset.cpus", "3-1,broken")
        self.assertEqual(self.budget(), 1)

    def test_large_sparse_and_overlapping_cpuset_ranges_need_no_expansion(self):
        self.affinity.side_effect = OSError("unavailable")
        self.host.return_value = self.process.return_value = 1_000_000_000
        self.write(self.root / "cpuset.cpus.effective", "0-999999999,2-8")
        self.assertEqual(self.budget(), 256)
        self.write(self.root / "cpuset.cpus.effective", "1,3-4,4-5,999999999")
        self.assertEqual(self.budget(), 5)

    def test_membership_cannot_escape_the_controller_root(self):
        self.write(self.proc / "self/cgroup", "0::/../../elsewhere\n")
        self.write(self.root / "cpu.max", "200000 100000")
        self.write(self.root.parent / "cpu.max", "100000 100000")
        self.assertEqual(self.budget(), 2)

    def test_v2_sibling_named_like_a_v1_controller_does_not_limit_this_process(self):
        self.write(self.proc / "self/cgroup", "0::/group/worker\n")
        self.write(self.root / "cpu.max", "800000 100000")
        self.write(self.root / "cpu/cpu.max", "100000 100000")
        self.write(self.root / "cpuset/cpuset.cpus.effective", "1")
        self.assertEqual(self.budget(), 8)

    def test_supervisor_wrapper_and_explicit_worker_count_are_preserved(self):
        with patch("bessemer.supervisor.cpu_budget", return_value=2) as budget:
            self.assertEqual(cpu_quota_count(self.root), 2)
            budget.assert_called_once_with(self.root)
            budget.reset_mock()
            supervisor = Supervisor("app:app", DEFAULTS | {"workers": 3})
            try:
                self.assertEqual(supervisor.workers, 3)
                budget.assert_not_called()
            finally:
                supervisor._selector.close()


if __name__ == "__main__":
    unittest.main()
