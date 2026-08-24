import torch
from diffusers import StableDiffusionPipeline
import time
import os

# 1. Check if PyTorch actually detects your RTX 5060
if not torch.cuda.is_available():
    raise RuntimeError("CUDA is not available. PyTorch is running on CPU! Check your installation.")

gpu_name = torch.cuda.get_device_name(0)
print(f"Using GPU: {gpu_name}")

# 2. Load SD 1.5 to GPU in FP16 precision (perfect for 8GB VRAM)
print("Loading model into VRAM...")
pipe = StableDiffusionPipeline.from_pretrained(
    "runwayml/stable-diffusion-v1-5", 
    torch_dtype=torch.float16
).to("cuda")

# Optimizations for 8GB VRAM
pipe.enable_attention_slicing()

prompt = "A vibrant cyberpunk street, high quality, digital art"
steps = 20

# 3. Warm-up run (forces CUDA compilation and prevents skewed metrics)
print("Warming up CUDA...")
_ = pipe(prompt, num_inference_steps=1)

# 4. Actual Benchmark Run
print("Running benchmark (20 steps)...")
torch.cuda.reset_peak_memory_stats()
start_time = time.time()

# Run inference
image = pipe(prompt, num_inference_steps=steps).images[0]

end_time = time.time()
elapsed = end_time - start_time
vram_allocated = torch.cuda.max_memory_allocated() / (1024 ** 3) # Convert bytes to GB

print("\n" + "="*30)
print("      RTX 5060 BENCHMARK RESULTS      ")
print("="*30)
print(f"Generation Time : {elapsed:.2f} seconds")
print(f"Throughput      : {steps / elapsed:.2f} iterations/second (it/s)")
print(f"Peak VRAM Used  : {vram_allocated:.2f} GB")
print("="*30)

# Save output to verify
output_path = "benchmark_output.png"
image.save(output_path)
print(f"Saved benchmark image to: {os.path.abspath(output_path)}")