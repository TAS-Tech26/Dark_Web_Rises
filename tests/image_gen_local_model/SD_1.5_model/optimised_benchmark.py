import torch
from diffusers import StableDiffusionPipeline, DPMSolverMultistepScheduler
import time
import os

# 1. Enable TF32 for faster math on your RTX 5060 Tensor Cores
torch.backends.cuda.matmul.allow_tf32 = True

if not torch.cuda.is_available():
    raise RuntimeError("CUDA is not available!")

print(f"Using GPU: {torch.cuda.get_device_name(0)}")

# 2. Load SD 1.5 in FP16
print("Loading model into VRAM...")
pipe = StableDiffusionPipeline.from_pretrained(
    "runwayml/stable-diffusion-v1-5", 
    torch_dtype=torch.float16
).to("cuda")

# 3. Swap Scheduler to DPM-Solver (Saves steps!)
pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config)

# 4. Optimize memory layout (Channels Last)
pipe.unet.to(memory_format=torch.channels_last)
if hasattr(pipe, "vae"):
    pipe.vae.to(memory_format=torch.channels_last)

# Note: We removed enable_attention_slicing() to unlock full parallel speed!

prompt = "A vibrant cyberpunk street, high quality, digital art"
steps = 12  # DPM-Solver only needs 12 steps for identical quality compared to 20 on PNDM!

# 5. Warm-up
print("Warming up CUDA...")
_ = pipe(prompt, num_inference_steps=1)

# 6. Optimized Benchmark Run
print(f"Running optimized benchmark ({steps} steps)...")
torch.cuda.reset_peak_memory_stats()
start_time = time.time()

image = pipe(prompt, num_inference_steps=steps).images[0]

end_time = time.time()
elapsed = end_time - start_time
vram_allocated = torch.cuda.max_memory_allocated() / (1024 ** 3)

print("\n" + "="*40)
print("   RTX 5060 OPTIMIZED BENCHMARK RESULTS   ")
print("="*40)
print(f"Generation Time : {elapsed:.2f} seconds")
print(f"Throughput      : {steps / elapsed:.2f} iterations/second (it/s)")
print(f"Peak VRAM Used  : {vram_allocated:.2f} GB")
print("="*40)

# Save output to verify quality
output_path = "benchmark_optimized.png"
image.save(output_path)
print(f"Saved optimized image to: {os.path.abspath(output_path)}")