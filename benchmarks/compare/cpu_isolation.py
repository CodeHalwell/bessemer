"""Temporarily pin other same-user Linux threads; restore their saved CPU masks."""

import argparse
import json
import os
import tempfile
from pathlib import Path

from servers import cpu_list


def identity(task):
    # comm can contain spaces and parentheses; starttime is field 22.
    return task.joinpath("stat").read_text().rsplit(")", 1)[1].split()[19]


def isolate(cpus, state):
    if state.exists():
        raise ValueError(f"state already exists; restore it first: {state}")
    saved = []
    for process in Path("/proc").glob("[0-9]*"):
        try:
            if int(process.name) == os.getpid() or process.stat().st_uid != os.getuid():
                continue
        except OSError:
            continue
        for task in process.joinpath("task").glob("[0-9]*"):
            try:
                tid = int(task.name)
                saved.append(
                    {
                        "tid": tid,
                        "start": identity(task),
                        "cpus": sorted(os.sched_getaffinity(tid)),
                    }
                )
            except (OSError, ValueError):
                continue
    # Save before changing anything so an interrupted run can be restored.
    with state.open("x") as handle:
        json.dump(saved, handle)
    changed = 0
    for entry in saved:
        try:
            task = Path(f"/proc/{entry['tid']}")
            if identity(task) == entry["start"]:
                os.sched_setaffinity(entry["tid"], cpus)
                changed += 1
        except OSError:
            continue
    return changed


def restore(state):
    changed = 0
    failed = []
    for entry in json.loads(state.read_text()):
        try:
            if identity(Path(f"/proc/{entry['tid']}")) == entry["start"]:
                os.sched_setaffinity(entry["tid"], entry["cpus"])
                changed += 1
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            failed.append(entry)
    if failed:
        state.write_text(json.dumps(failed))
        raise OSError(
            f"could not restore {len(failed)} threads; retry with state {state}"
        )
    state.unlink()
    return changed


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("mode", choices=("on", "off"))
    cli.add_argument(
        "--cpus", help="housekeeping CPUs (default: last CPU in current affinity)"
    )
    cli.add_argument(
        "--state",
        type=Path,
        default=Path(tempfile.gettempdir())
        / f"bessemer-cpu-isolation-{os.getuid()}.json",
    )
    args = cli.parse_args()
    try:
        cpus = set(cpu_list(args.cpus)) if args.cpus else {max(os.sched_getaffinity(0))}
        if not cpus:
            raise ValueError("housekeeping CPU list must not be empty")
        if args.mode == "on" and not cpus <= os.sched_getaffinity(0):
            raise ValueError(
                "housekeeping CPUs must be in this process's current affinity"
            )
        changed = (
            isolate(cpus, args.state) if args.mode == "on" else restore(args.state)
        )
    except (OSError, ValueError) as error:
        cli.exit(1, f"{error}\n")
    print(
        f"isolation {args.mode}: changed {changed} existing threads; state {args.state}"
    )


if __name__ == "__main__":
    main()
