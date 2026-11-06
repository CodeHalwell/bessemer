mod compute;

use compute::Gpu;
use serde_json::{Value, json};
use std::{hint::black_box, path::PathBuf, time::Instant};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
const ABS_TOLERANCE: f32 = 0.0002;
const REL_TOLERANCE: f32 = 0.0002;

struct Options {
    backend: wgpu::Backends,
    sizes: Vec<usize>,
    samples: usize,
    warmup: usize,
    output: Option<PathBuf>,
}

impl Options {
    fn parse() -> Result<Option<Self>> {
        let backend = if cfg!(target_os = "macos") {
            "metal"
        } else if cfg!(target_os = "windows") {
            "dx12"
        } else {
            "vulkan"
        };
        let mut options = Self {
            backend: parse_backend(backend)?,
            sizes: vec![32, 257, 1024],
            samples: 5,
            warmup: 3,
            output: None,
        };
        let mut args = std::env::args().skip(1);
        while let Some(flag) = args.next() {
            if flag == "--help" {
                println!(
                    "bessemer-gpu-probe [--backend metal|vulkan|dx12] [--sizes 32,257,1024] [--samples 5] [--warmup 3] [--output report.json]"
                );
                return Ok(None);
            }
            let value = args
                .next()
                .ok_or_else(|| format!("missing value for {flag}"))?;
            match flag.as_str() {
                "--backend" => options.backend = parse_backend(&value)?,
                "--sizes" => {
                    options.sizes = value
                        .split(',')
                        .map(str::parse)
                        .collect::<std::result::Result<_, _>>()?
                }
                "--samples" => options.samples = value.parse()?,
                "--warmup" => options.warmup = value.parse()?,
                "--output" => options.output = Some(value.into()),
                _ => return Err(format!("unknown argument {flag}").into()),
            }
        }
        if options.sizes.is_empty() || options.sizes.iter().any(|&n| !(1..=2048).contains(&n)) {
            return Err("matrix dimensions must be between 1 and 2048".into());
        }
        if !(1..=100).contains(&options.samples) || !(1..=20).contains(&options.warmup) {
            return Err("samples must be 1..100 and warmup must be 1..20".into());
        }
        Ok(Some(options))
    }
}

fn parse_backend(name: &str) -> Result<wgpu::Backends> {
    match name {
        "metal" => Ok(wgpu::Backends::METAL),
        "vulkan" => Ok(wgpu::Backends::VULKAN),
        "dx12" => Ok(wgpu::Backends::DX12),
        _ => Err("backend must be metal, vulkan or dx12".into()),
    }
}

fn input(n: usize, seed: u32) -> Vec<f32> {
    let mut state = seed;
    (0..n * n)
        .map(|_| {
            state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            (state >> 8) as f32 / 16_777_216.0 - 0.5
        })
        .collect()
}

fn cpu_multiply(n: usize, a: &[f32], b: &[f32], output: &mut [f32]) {
    // Disjoint row-major n×n buffers satisfy sgemm's stride and aliasing rules.
    unsafe {
        matrixmultiply::sgemm(
            n,
            n,
            n,
            1.0,
            a.as_ptr(),
            n as isize,
            1,
            b.as_ptr(),
            n as isize,
            1,
            0.0,
            output.as_mut_ptr(),
            n as isize,
            1,
        );
    }
    black_box(output);
}

fn correctness(reference: &[f32], actual: &[f32]) -> Result<(f32, f32)> {
    if reference.len() != actual.len() {
        return Err("GPU returned the wrong output length".into());
    }
    let mut max_absolute = 0.0f32;
    let mut max_scaled = 0.0f32;
    for (index, (&expected, &found)) in reference.iter().zip(actual).enumerate() {
        let error = (expected - found).abs();
        let tolerance = ABS_TOLERANCE + REL_TOLERANCE * expected.abs();
        if !expected.is_finite() || !found.is_finite() || error > tolerance {
            return Err(format!(
                "incorrect GPU output at {index}: {found} vs {expected}, tolerance {tolerance}"
            )
            .into());
        }
        max_absolute = max_absolute.max(error);
        max_scaled = max_scaled.max(error / tolerance);
    }
    Ok((max_absolute, max_scaled))
}

fn median(values: &[f64]) -> f64 {
    let mut ordered = values.to_vec();
    ordered.sort_by(f64::total_cmp);
    let middle = ordered.len() / 2;
    if ordered.len() % 2 == 0 {
        (ordered[middle - 1] + ordered[middle]) / 2.0
    } else {
        ordered[middle]
    }
}

fn measure<T>(work: impl FnOnce() -> T) -> (T, f64) {
    let started = Instant::now();
    let result = work();
    (result, started.elapsed().as_secs_f64() * 1000.0)
}

fn case(gpu: &Gpu, n: usize, options: &Options) -> Result<Value> {
    let a = input(n, 17);
    let b = input(n, 31);
    let mut cpu = vec![0.0; n * n];
    let (matrices, setup_ms) = measure(|| gpu.matrices(n as u32));
    for _ in 0..options.warmup {
        cpu_multiply(n, &a, &b, &mut cpu);
        correctness(&cpu, &gpu.multiply(&matrices, &a, &b)?)?;
    }
    let mut cpu_times = Vec::new();
    let mut gpu_times = Vec::new();
    let mut samples = Vec::new();
    for index in 0..options.samples {
        let (gpu_output, cpu_ms, gpu_ms) = if index % 2 == 0 {
            let (_, cpu_ms) = measure(|| cpu_multiply(n, &a, &b, &mut cpu));
            let (output, gpu_ms) = measure(|| gpu.multiply(&matrices, &a, &b));
            (output?, cpu_ms, gpu_ms)
        } else {
            let (output, gpu_ms) = measure(|| gpu.multiply(&matrices, &a, &b));
            let (_, cpu_ms) = measure(|| cpu_multiply(n, &a, &b, &mut cpu));
            (output?, cpu_ms, gpu_ms)
        };
        let (max_absolute, max_scaled) = correctness(&cpu, &gpu_output)?;
        cpu_times.push(cpu_ms);
        gpu_times.push(gpu_ms);
        samples.push(json!({"cpu_ms": cpu_ms, "gpu_transfer_inclusive_ms": gpu_ms, "max_absolute_error": max_absolute, "max_error_over_tolerance": max_scaled}));
    }
    Ok(json!({
        "n": n,
        "matrix_bytes": n * n * 4,
        "case_setup_ms": setup_ms,
        "cpu_median_ms": median(&cpu_times),
        "gpu_transfer_inclusive_median_ms": median(&gpu_times),
        "cpu_over_gpu_time_ratio": median(&cpu_times) / median(&gpu_times),
        "samples": samples,
    }))
}

fn run() -> Result<()> {
    let Some(options) = Options::parse()? else {
        return Ok(());
    };
    if cfg!(debug_assertions) {
        return Err("use --release; debug builds are not comparable compute measurements".into());
    }
    let started = Instant::now();
    let gpu = Gpu::new(options.backend)?;
    let initialization_ms = started.elapsed().as_secs_f64() * 1000.0;
    let cases = options
        .sizes
        .iter()
        .map(|&n| case(&gpu, n, &options))
        .collect::<Result<Vec<_>>>()?;
    let report = json!({
        "probe": "optional Rust tiled FP32 matmul",
        "wgpu_version": "30.0.1",
        "os": std::env::consts::OS,
        "arch": std::env::consts::ARCH,
        "requested_backend": format!("{:?}", options.backend),
        "adapter": {
            "name": gpu.info.name,
            "backend": format!("{:?}", gpu.info.backend),
            "device_type": format!("{:?}", gpu.info.device_type),
            "vendor": gpu.info.vendor,
            "device": gpu.info.device,
            "driver": gpu.info.driver,
            "driver_info": gpu.info.driver_info,
        },
        "initialization_ms": initialization_ms,
        "warmup_runs": options.warmup,
        "measured_runs": options.samples,
        "cpu_reference": "matrixmultiply 0.3.11 sgemm; single thread; runtime SIMD",
        "gpu_cached": ["device", "pipeline", "input/output/readback buffers", "bind group"],
        "gpu_timed": ["two host uploads", "command encoding/submission", "matmul", "output copy/map", "CPU result allocation/copy", "unmap"],
        "correctness": {"absolute_tolerance": ABS_TOLERANCE, "relative_tolerance": REL_TOLERANCE, "all_samples_checked": true},
        "cases": cases,
    });
    let encoded = serde_json::to_string_pretty(&report)?;
    if let Some(path) = options.output {
        if let Some(parent) = path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(path, &encoded)?;
    }
    println!("{encoded}");
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("GPU probe failed: {error}");
        std::process::exit(1);
    }
}
