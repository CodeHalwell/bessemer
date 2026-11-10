use std::{env, process::Command};

fn main() {
    // Set by the sanitizer jobs only; see the global allocator in src/lib.rs.
    println!("cargo::rustc-check-cfg=cfg(bessemer_system_allocator)");
    println!("cargo:rerun-if-env-changed=PYO3_PYTHON");
    if env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("macos") {
        return;
    }

    let interpreter = env::var("PYO3_PYTHON").unwrap_or_else(|_| "python3".to_owned());
    let output = Command::new(interpreter)
        .args([
            "-c",
            "import sysconfig; print(sysconfig.get_config_var('LIBDIR'))",
        ])
        .output()
        .expect("run the configured Python interpreter to locate libpython");
    assert!(output.status.success(), "failed to locate libpython");
    let libdir =
        String::from_utf8(output.stdout).expect("Python returned a non-UTF-8 library path");
    println!("cargo:rustc-link-arg=-Wl,-rpath,{}", libdir.trim());
}
