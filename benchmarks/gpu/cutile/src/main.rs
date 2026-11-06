// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES.
// SPDX-License-Identifier: Apache-2.0

#[cfg(target_os = "linux")]
use cutile::prelude::*;

#[cfg(target_os = "linux")]
#[cutile::module]
mod kernel {
    use cutile::core::*;

    #[cutile::entry()]
    fn add<const B: i32>(
        output: &mut Tensor<f32, { [B] }>,
        x: &Tensor<f32, { [-1] }>,
        y: &Tensor<f32, { [-1] }>,
    ) {
        let tx = x.load_like(output);
        let ty = y.load_like(output);
        output.store(tx + ty);
    }
}

#[cfg(target_os = "linux")]
fn main() -> Result<(), Error> {
    let context = Context::new(0)?;
    let stream = context.new_stream()?;
    let x = api::ones::<f32>(&[1024]);
    let y = api::ones::<f32>(&[1024]);
    let output = api::zeros::<f32>(&[1024]).partition([128]);
    let result: Vec<f32> = kernel::add(output, x, y)
        .first()
        .unpartition()
        .to_host_vec()
        .sync_on(&stream)?;
    assert_eq!(result, vec![2.0; 1024]);
    println!("PASSED: CUDA device index 0, all 1024 elements correct; no timing claim");
    Ok(())
}

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("The cuTile reference requires Linux, a supported NVIDIA GPU and CUDA toolkit.");
    std::process::exit(1);
}
