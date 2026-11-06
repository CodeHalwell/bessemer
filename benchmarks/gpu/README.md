# Optional Rust GPU compute probe

This standalone Cargo workspace measures one numeric batch operation. It adds no
GPU dependency to Bessemer, its wheel, HTTP runtime or public API. It does not
move arbitrary Python handlers, request parsing or HTTP I/O onto a GPU.

The headless initialization, bind groups, dispatch, download mapping and polling
are ported from wgpu's official
[01_hello_compute example](https://github.com/gfx-rs/wgpu/tree/trunk/examples/standalone/01_hello_compute).
Its doubling kernel is replaced with a 16×16 tiled FP32 matrix multiplication.
The CPU reference uses the purpose-built Rust
[matrixmultiply](https://github.com/bluss/matrixmultiply) sgemm implementation,
including its AArch64 NEON or x86 SIMD kernels; its optional threading feature is
disabled. This compares one CPU thread with the selected GPU, not all CPU cores,
Apple Accelerate, cuBLAS or a tuned matrix library on the GPU.

## Build and run

From the Bessemer repository root, after other performance measurements finish:

```sh
gpu_probe_target="${TMPDIR:-/tmp}/bessemer-gpu-target"
CARGO_TARGET_DIR="$gpu_probe_target" \
  cargo build --manifest-path benchmarks/gpu/Cargo.toml --release --locked -j 2
"$gpu_probe_target/release/bessemer-gpu-probe" \
  --backend metal --sizes 32,257,1024 --warmup 3 --samples 5 \
  --output benchmarks/results/gpu-metal.json
```

Apple Silicon uses native Metal. Linux can use `--backend vulkan` with an installed
GPU Vulkan driver; Windows can use `--backend dx12` or `--backend vulkan`.
The default is Metal on macOS, DX12 on Windows and Vulkan elsewhere. The JSON
records the actual adapter/backend, GPU type, vendor, driver and raw timings.
CPU, virtual and unidentified adapters are rejected; missing GPU/backend support
fails the run instead of substituting a CPU implementation.

The shader pipeline is created once. Device buffers and bind groups are allocated
once per matrix size and reused across warmup and measured calls. Each GPU timing
includes both host-input uploads, command encoding/submission, kernel execution,
output copy/map, CPU result allocation/copy and unmapping. Initialization and
per-size allocation times are reported separately. Apple unified memory still
has those synchronization/copy costs. These are wall-clock transfer-inclusive
latencies, not hardware GPU timestamp measurements.

Default cases include a small workload, a dimension not divisible by the tile
size, and a 1024×1024 workload. CPU/GPU measurement order alternates. Every warmup
and measured output is checked element by element using
`abs(actual-reference) <= 0.0002 + 0.0002*abs(reference)`; non-finite results fail.
The JSON includes raw samples, medians, maximum absolute error and the maximum
fraction of the allowed tolerance used. A ratio above 1 means the GPU completed
this operation faster than the single-thread CPU reference.

Matrix dimensions are limited to 1–2048, samples to 1–100 and warmups to 1–20.
The default 1024 case has four 4 MiB GPU buffers plus CPU inputs/results; it does not
download models or use cloud services. First release compilation builds wgpu/Naga
and platform bindings, so schedule it separately from HTTP benchmarks. No hardware
result is claimed until the probe has actually run on a named adapter.

## Recorded Metal result

The probe ran on 6 November 2026 using the **Apple M5 Max integrated GPU / Metal**,
with three warmups and five alternating-order measured samples per size.
Cold device/pipeline initialization took **121.31 ms**; cached per-size resource
setup took **0.060–0.117 ms**.

| Matrix | Single-thread SIMD CPU median | GPU median, including transfers | CPU time / GPU time |
|---|---:|---:|---:|
| 32×32 | 0.00150 ms | 0.61408 ms | 0.00244× |
| 257×257 | 0.34796 ms | 0.95825 ms | 0.363× |
| 1024×1024 | 19.35083 ms | 6.79800 ms | 2.847× |

GPU submission and transfer costs outweighed the computation for the smaller
cases. The largest case completed **2.85× faster** on the GPU than this
single-thread CPU reference. Every warmup and measured output passed the stated
tolerance; maximum absolute error was `2.002716e-5` (3.68% of the allowed tolerance
at its worst element). These results cover cached numeric batch compute and do
not establish an HTTP/framework speedup.

Raw artifacts, relative to the repository root:

- [JSON](../results/2026-11-06/multicore-pass/gpu-metal.json): `benchmarks/results/2026-11-06/multicore-pass/gpu-metal.json`
- [Execution log](../results/2026-11-06/multicore-pass/gpu-metal.log): `benchmarks/results/2026-11-06/multicore-pass/gpu-metal.log`

The cuTile/CUDA source port below was not compiled or executed on NVIDIA hardware.

## Rust library choices

| Library | Apple Silicon | NVIDIA | Use here |
|---|---|---|---|
| [wgpu](https://wgpu.rs/) | Native Metal | Vulkan or DX12, according to host | Portable explicit compute and backend evidence in this probe |
| [cuTile Rust](https://github.com/NVIDIA/cuda-rust/tree/main/cutile-rs) | No CUDA on Apple GPU | CUDA Tile IR | First NVIDIA kernel direction, following the supplied NVIDIA article |
| [CubeCL](https://github.com/tracel-ai/cubecl) | Metal backend | CUDA backend | Rust-authored kernels with JIT specialization; a candidate if maintaining kernels across vendors becomes worthwhile |
| [cudarc](https://github.com/chelsea0x3b/cudarc) | No CUDA on Apple GPU | CUDA driver, NVRTC, cuBLAS/cuBLASLt and other CUDA libraries | NVIDIA-specific operations or established vendor math libraries |

wgpu's native backends are graphics/compute APIs, not CUDA. CubeCL is a different
kernel/runtime choice; cudarc exposes NVIDIA's stack. Neither is installed by
this probe. An eventual application integration would need an explicit batched
numeric workload, bounded queueing, cancellation and measured transfer costs.

## NVIDIA's CUDA Rust reference

The supplied [NVIDIA article](https://developer.nvidia.com/blog/introducing-cuda-rust-two-tracks-for-writing-gpu-kernels/)
recommends starting with the Tile model (`cutile-rs`) and choosing SIMT
(`cuda-oxide`) when explicit thread/memory control is needed. The article's
September 2026 setup uses Linux, SM 8.0+ GPUs and CUDA 13.3 with stable Rust 1.89+
for Tile; its SIMT track uses CUDA 12+, pinned nightly Rust and LLVM. Both are
described as early projects rather than production-ready toolchains.

Current owning source has moved to
[NVIDIA/cuda-rust](https://github.com/NVIDIA/cuda-rust). Its
[cuTile compatibility matrix](https://github.com/NVIDIA/cuda-rust/tree/main/cutile-rs#requirements)
now recommends CUDA 13.3 and lists architecture/feature-specific Tile IR versions.
Its SIMT README now uses a managed pinned stable toolchain; follow the current
owning instructions when installing rather than assuming the article's nightly
command still applies.

`cutile/` is a separate optional workspace containing the article's 1024-element
vector-add pattern ported against the owning SDK's current `load_like` and
`sync_on` APIs, pinned to `cutile==0.5.0`. It uses exclusive output partitions and
verifies every returned element. Its sources follow the owning
[quick start](https://github.com/NVIDIA/cuda-rust/tree/main/cutile-rs#quick-start)
and [explicit-stream example](https://github.com/NVIDIA/cuda-rust/blob/main/cutile-rs/cutile-examples/examples/saxpy.rs).
This source port has **not been compiled or run on an NVIDIA system**. It is a
correctness smoke example, not a CUDA performance comparison.

On a separately prepared Linux/NVIDIA machine:

```sh
CUDA_TOOLKIT_PATH=/usr/local/cuda-13.3 \
  cargo run --manifest-path benchmarks/gpu/cutile/Cargo.toml --release --locked
```

The Metal probe never builds cuTile or CUDA crates. The cuTile example fails on
non-Linux systems instead of pretending to execute CUDA on an Apple GPU.
