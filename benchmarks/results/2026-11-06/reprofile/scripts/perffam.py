import collections
import re

from sub import fam

P = "/home/user/bessemer/benchmarks/results/2026-11-06/reprofile/raw/perf/"
for d, total in [
    ("bessemer-rust-json", 12.22),
    ("bessemer-rust-echo", 16.41),
    ("axum-json", 11.44),
]:
    res = collections.Counter()
    with open(P + d + "/perf-report-dso-sym-full.demangled.txt") as fh:
        for line in fh:
            m = re.match(r"\s*([\d.]+)%\s+(\S+)\s+\[(.)\]\s+(.*)$", line)
            if not m or m.group(3) == "k":
                continue
            dso = m.group(2).split("/")[-1]
            s = m.group(4).strip()
            if "vdso" in dso:
                res["vdso clock_gettime"] += float(m.group(1))
                continue
            if dso.startswith("libc"):
                k = (
                    "libc mem*"
                    if re.search(r"mem(move|cpy|set|cmp)", s)
                    else (
                        "libc syscall wrappers"
                        if re.search(
                            r"send|recv|epoll|asynccancel|writev|syscall|read|write", s
                        )
                        else "libc other"
                    )
                )
                res[k] += float(m.group(1))
                continue
            if dso.startswith("python3") or re.match(
                r"^<?(pyo3|_native::invoker|_native::bridge|_native::conversion|bessemer_loop|bessemer_codec|mi_|_mi_)",
                s,
            ):
                res["python-side (see buckets)"] += float(m.group(1))
                continue
            res[
                fam(s)
                if "axum" not in dso
                else (
                    "axum:"
                    + (
                        "hyper"
                        if "hyper" in s
                        else "tokio"
                        if "tokio" in s
                        else "serde"
                        if "serde" in s
                        else "axum/http"
                        if re.search("axum|http::|httparse", s)
                        else "mem/alloc"
                        if re.search("malloc|free|alloc|mem", s)
                        else "other"
                    )
                )
            ] += float(m.group(1))
    print(d)
    for k, v in res.most_common():
        print(f"   {k:32s}{v:6.1f}%  {total * v / 100:5.2f} us")
