import torch
from diffusers import StableDiffusionXLPipeline, EulerDiscreteScheduler
import time
import os

# 1. Hardware Optimizations
torch.backends.cuda.matmul.allow_tf32 = True

if not torch.cuda.is_available():
    raise RuntimeError("CUDA is not available!")

print(f"Using GPU: {torch.cuda.get_device_name(0)}")

# 2. Load SDXL-Lightning (Requires much less VRAM than standard SDXL)
repo = "ByteDance/SDXL-Lightning"
ckpt = "sdxl_lightning_4step.safetensors" # 4-step checkpoint

print("Loading SDXL-Lightning Model...")
pipe = StableDiffusionXLPipeline.from_pretrained(
    "stabilityai/stable-diffusion-xl-base-1.0", 
    torch_dtype=torch.float16
).to("cuda")

# Load the lightning weights into the pipeline
pipe.load_lora_weights(repo, weight_name=ckpt)
pipe.fuse_lora()

# Use the specific Euler scheduler recommended for Lightning
pipe.scheduler = EulerDiscreteScheduler.from_config(
    pipe.scheduler.config, 
    timestep_spacing="trailing"
)

# 3. Memory Layout & VRAM Optimization (Vital for SDXL on 8GB VRAM)
pipe.unet.to(memory_format=torch.channels_last)
pipe.enable_xformers_memory_efficient_attention()

# If you get an OOM, uncomment the line below to save VRAM at a tiny speed cost:
# pipe.enable_model_cpu_offload()

prompt = "A high-quality, cinematic portrait of a futuristic cyberpunk hacker with neon reflections in glasses, 8k resolution, detailed skin texture"
steps = 4  # Lightning ONLY needs 4 steps!

# 4. Warm-up
print("Warming up CUDA...")
_ = pipe(prompt, num_inference_steps=1)

# 5. Run Benchmark
print(f"Running SDXL-Lightning Benchmark (1024x1024, {steps} steps)...")
torch.cuda.reset_peak_memory_stats()
start_time = time.time()

image = pipe(prompt, num_inference_steps=steps, guidance_scale=0.0).images[0]

end_time = time.time()
elapsed = end_time - start_time
vram_allocated = torch.cuda.max_memory_allocated() / (1024 ** 3)

print("\n" + "="*45)
print("   RTX 5060 SDXL-LIGHTNING RESULTS   ")
print("="*45)
print(f"Generation Time : {elapsed:.2f} seconds")
print(f"Throughput      : {steps / elapsed:.2f} iterations/second (it/s)")
print(f"Peak VRAM Used  : {vram_allocated:.2f} GB")
print("="*45)

# Save output to compare quality
output_path = "benchmark_sdxl.png"
image.save(output_path)
print(f"Saved SDXL image to: {os.path.abspath(output_path)}")