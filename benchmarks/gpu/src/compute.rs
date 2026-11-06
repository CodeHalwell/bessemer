use std::{num::NonZeroU64, sync::mpsc, time::Duration};
use wgpu::util::DeviceExt;

use crate::Result;

pub struct Gpu {
    pub info: wgpu::AdapterInfo,
    device: wgpu::Device,
    queue: wgpu::Queue,
    layout: wgpu::BindGroupLayout,
    pipeline: wgpu::ComputePipeline,
}

pub struct Matrices {
    input_a: wgpu::Buffer,
    input_b: wgpu::Buffer,
    output: wgpu::Buffer,
    download: wgpu::Buffer,
    bindings: wgpu::BindGroup,
    n: u32,
}

impl Gpu {
    pub fn new(backend: wgpu::Backends) -> Result<Self> {
        let mut descriptor = wgpu::InstanceDescriptor::new_without_display_handle();
        descriptor.backends = backend;
        let instance = wgpu::Instance::new(descriptor);
        let adapter = pollster::block_on(instance.request_adapter(&wgpu::RequestAdapterOptions {
            power_preference: wgpu::PowerPreference::HighPerformance,
            force_fallback_adapter: false,
            compatible_surface: None,
            ..Default::default()
        }))?;
        let info = adapter.get_info();
        if !matches!(
            info.device_type,
            wgpu::DeviceType::IntegratedGpu | wgpu::DeviceType::DiscreteGpu
        ) {
            return Err(format!("refusing non-physical GPU adapter: {info:?}").into());
        }
        if !adapter
            .get_downlevel_capabilities()
            .flags
            .contains(wgpu::DownlevelFlags::COMPUTE_SHADERS)
        {
            return Err("adapter does not support compute shaders".into());
        }
        let (device, queue) =
            pollster::block_on(adapter.request_device(&wgpu::DeviceDescriptor {
                label: Some("GPU compute probe"),
                required_features: wgpu::Features::empty(),
                required_limits: wgpu::Limits::downlevel_defaults(),
                experimental_features: wgpu::ExperimentalFeatures::disabled(),
                memory_hints: wgpu::MemoryHints::MemoryUsage,
                trace: wgpu::Trace::Off,
            }))?;
        let module = device.create_shader_module(wgpu::include_wgsl!("matmul.wgsl"));
        let entries = std::array::from_fn::<_, 4, _>(|binding| wgpu::BindGroupLayoutEntry {
            binding: binding as u32,
            visibility: wgpu::ShaderStages::COMPUTE,
            ty: wgpu::BindingType::Buffer {
                ty: if binding == 3 {
                    wgpu::BufferBindingType::Uniform
                } else {
                    wgpu::BufferBindingType::Storage {
                        read_only: binding < 2,
                    }
                },
                min_binding_size: NonZeroU64::new(if binding == 3 { 16 } else { 4 }),
                has_dynamic_offset: false,
            },
            count: None,
        });
        let layout = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
            label: Some("matmul buffers"),
            entries: &entries,
        });
        let pipeline_layout = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
            label: None,
            bind_group_layouts: &[Some(&layout)],
            immediate_size: 0,
        });
        let pipeline = device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: Some("tiled FP32 matmul"),
            layout: Some(&pipeline_layout),
            module: &module,
            entry_point: Some("matmul"),
            compilation_options: wgpu::PipelineCompilationOptions::default(),
            cache: None,
        });
        Ok(Self {
            info,
            device,
            queue,
            layout,
            pipeline,
        })
    }

    pub fn matrices(&self, n: u32) -> Matrices {
        let size = u64::from(n) * u64::from(n) * 4;
        let buffer = |label, usage| {
            self.device.create_buffer(&wgpu::BufferDescriptor {
                label: Some(label),
                size,
                usage,
                mapped_at_creation: false,
            })
        };
        let input_a = buffer(
            "A",
            wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
        );
        let input_b = buffer(
            "B",
            wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
        );
        let output = buffer(
            "C",
            wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC,
        );
        let download = buffer(
            "CPU readback",
            wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::MAP_READ,
        );
        let parameters = self
            .device
            .create_buffer_init(&wgpu::util::BufferInitDescriptor {
                label: Some("matrix dimensions"),
                contents: bytemuck::cast_slice(&[n, 0, 0, 0]),
                usage: wgpu::BufferUsages::UNIFORM,
            });
        let entries = [&input_a, &input_b, &output, &parameters]
            .into_iter()
            .enumerate()
            .map(|(binding, buffer)| wgpu::BindGroupEntry {
                binding: binding as u32,
                resource: buffer.as_entire_binding(),
            })
            .collect::<Vec<_>>();
        let bindings = self.device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: None,
            layout: &self.layout,
            entries: &entries,
        });
        Matrices {
            input_a,
            input_b,
            output,
            download,
            bindings,
            n,
        }
    }

    pub fn multiply(&self, matrices: &Matrices, a: &[f32], b: &[f32]) -> Result<Vec<f32>> {
        self.queue
            .write_buffer(&matrices.input_a, 0, bytemuck::cast_slice(a));
        self.queue
            .write_buffer(&matrices.input_b, 0, bytemuck::cast_slice(b));
        let mut encoder = self
            .device
            .create_command_encoder(&wgpu::CommandEncoderDescriptor { label: None });
        {
            let mut pass = encoder.begin_compute_pass(&wgpu::ComputePassDescriptor {
                label: None,
                timestamp_writes: None,
            });
            pass.set_pipeline(&self.pipeline);
            pass.set_bind_group(0, &matrices.bindings, &[]);
            let groups = matrices.n.div_ceil(16);
            pass.dispatch_workgroups(groups, groups, 1);
        }
        encoder.copy_buffer_to_buffer(
            &matrices.output,
            0,
            &matrices.download,
            0,
            matrices.output.size(),
        );
        self.queue.submit([encoder.finish()]);
        let slice = matrices.download.slice(..);
        let (sender, receiver) = mpsc::sync_channel(1);
        slice.map_async(wgpu::MapMode::Read, move |result| {
            let _ = sender.send(result);
        });
        self.device.poll(wgpu::PollType::Wait {
            submission_index: None,
            timeout: Some(Duration::from_secs(30)),
        })?;
        receiver.recv_timeout(Duration::from_secs(30))??;
        let view = slice.get_mapped_range()?;
        let result = bytemuck::allocation::pod_collect_to_vec(&view);
        drop(view);
        matrices.download.unmap();
        Ok(result)
    }
}
