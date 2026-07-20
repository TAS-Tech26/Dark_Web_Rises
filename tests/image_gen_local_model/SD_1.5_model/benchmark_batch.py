import torch
from diffusers import StableDiffusionPipeline, DPMSolverMultistepScheduler
import time
import os

# 1. Hardware Optimizations (From last run)
torch.backends.cuda.matmul.allow_tf32 = True

if not torch.cuda.is_available():
    raise RuntimeError("CUDA is not available!")

print(f"Using GPU: {torch.cuda.get_device_name(0)}")

# 2. Setup Pipeline (FP16, Channels Last)
print("Loading model into VRAM...")
pipe = StableDiffusionPipeline.from_pretrained(
    "runwayml/stable-diffusion-v1-5", 
    torch_dtype=torch.float16
).to("cuda")

pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config)
pipe.unet.to(memory_format=torch.channels_last)

# 3. Define the Batch
# Since we are making multiple images, we need a prompt for each one.
# We will duplicate the prompt 4 times to fill the batch.
base_prompt = "A detailed photograph of a cat wearing a tiny space helmet, high quality, 8k"
batch_size = 4  # We are testing 4 parallel images
prompts = [base_prompt] * batch_size  # ["...", "...", "...", "..."]

steps = 20

# 4. Warm-up (Required)
print("Warming up CUDA...")
_ = pipe(prompts[0], num_inference_steps=1)

# 5. The Batch Benchmark Run
print(f"Running BATCHED benchmark (Batch Size={batch_size}, {steps} steps)...")
torch.cuda.reset_peak_memory_stats()
start_time = time.time()

# We pass the list of 4 prompts directly to the pipeline
results = pipe(prompts, num_inference_steps=steps).images

end_time = time.time()
elapsed = end_time - start_time
vram_allocated = torch.cuda.max_memory_allocated() / (1024 ** 3)

# Calculate throughput: Total images / total seconds
throughput = batch_size / elapsed

print("\n" + "="*50)
print(f"  RTX 5060 BATCHED ({batch_size}) BENCHMARK RESULTS  ")
print("="*50)
print(f"Total Generation Time: {elapsed:.2f} seconds")
print(f"System Throughput    : {throughput:.2f} images/second")
print(f"Peak VRAM Used       : {vram_allocated:.2f} GB")
print("="*50)

# Save the batch output to verify
if not os.path.exists('batch_results'):
    os.makedirs('batch_results')

for i, img in enumerate(results):
    img.save(f"batch_results/image_{i}.png")
print(f"Saved {batch_size} images to the 'batch_results' folder.")