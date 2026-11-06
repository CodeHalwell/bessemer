struct Parameters {
    n: u32,
    padding0: u32,
    padding1: u32,
    padding2: u32,
}

@group(0) @binding(0) var<storage, read> a: array<f32>;
@group(0) @binding(1) var<storage, read> b: array<f32>;
@group(0) @binding(2) var<storage, read_write> c: array<f32>;
@group(0) @binding(3) var<uniform> parameters: Parameters;

var<workgroup> tile_a: array<array<f32, 16>, 16>;
var<workgroup> tile_b: array<array<f32, 16>, 16>;

@compute @workgroup_size(16, 16)
fn matmul(
    @builtin(global_invocation_id) global: vec3<u32>,
    @builtin(local_invocation_id) local: vec3<u32>,
) {
    let row = global.y;
    let column = global.x;
    let n = parameters.n;
    var total = 0.0;
    for (var start = 0u; start < n; start += 16u) {
        var av = 0.0;
        var bv = 0.0;
        if (row < n && start + local.x < n) {
            av = a[row * n + start + local.x];
        }
        if (column < n && start + local.y < n) {
            bv = b[(start + local.y) * n + column];
        }
        tile_a[local.y][local.x] = av;
        tile_b[local.y][local.x] = bv;
        workgroupBarrier();
        for (var k = 0u; k < 16u; k += 1u) {
            total += tile_a[local.y][k] * tile_b[k][local.x];
        }
        workgroupBarrier();
    }
    if (row < n && column < n) {
        c[row * n + column] = total;
    }
}
