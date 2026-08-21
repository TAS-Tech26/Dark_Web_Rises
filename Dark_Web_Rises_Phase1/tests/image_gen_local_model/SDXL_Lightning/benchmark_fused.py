import torch
from diffusers import StableDiffusionXLPipeline, UNet2DConditionModel, EulerDiscreteScheduler
from huggingface_hub import hf_hub_download
from safetensors.torch import load_file

def main():
    # 1. Configuration
    base_model = "stabilityai/stable-diffusion-xl-base-1.0"
    lightning_repo = "ByteDance/SDXL-Lightning"
    unet_checkpoint = "sdxl_lightning_4step_unet.safetensors"
    device = "cuda" if torch.cuda.is_available() else "cpu"
    
    print(f"Using device: {device}")

    # 2. Load the UNet skeleton (Use CPU first to save GPU VRAM)
    print("Initializing UNet skeleton from base SDXL configuration...")
    unet = UNet2DConditionModel.from_config(base_model, subfolder="unet")
    
    # 3. Download and apply the SDXL-Lightning UNet weights (Keep on CPU initially)
    print(f"Downloading and loading weights from {lightning_repo}/{unet_checkpoint}...")
    checkpoint_path = hf_hub_download(repo_id=lightning_repo, filename=unet_checkpoint)
    state_dict = load_file(checkpoint_path, device="cpu")
    unet.load_state_dict(state_dict)
    
    # Cast UNet to float16
    unet = unet.to(dtype=torch.float16)

    # 4. Initialize the Complete Pipeline
    # Note: We load the whole pipeline onto the CPU first (.to("cuda") is omitted)
    # because CPU offloading will handle moving blocks to the GPU dynamically!
    print("Loading base SDXL pipeline with custom Lightning UNet on CPU...")
    pipe = StableDiffusionXLPipeline.from_pretrained(
        base_model,
        unet=unet,
        torch_dtype=torch.float16,
        variant="fp16"
    )

    # 5. Configure the Euler Scheduler for Lightning
    print("Configuring Euler discrete scheduler...")
    pipe.scheduler = EulerDiscreteScheduler.from_config(
        pipe.scheduler.config, 
        timestep_spacing="trailing"
    )

    # 6. 🔥 VRAM Optimizations for 8GB GPUs 🔥
    print("Applying VRAM optimizations...")
    
    # This automatically moves models to GPU only when they are executing
    pipe.enable_model_cpu_offload()
    
    # Slices VAE decoding to prevent OOM when converting latent noise to a 1024x1024 image
    pipe.enable_vae_slicing()
    pipe.enable_vae_tiling()

    # 7. Run Inference
    prompt = "An astronaut riding a green horse on mars, cinematic lighting, highly detailed, 8k"
    num_inference_steps = 4
    guidance_scale = 0.0  # Must be 0 for SDXL-Lightning
    
    print(f"Generating image (steps={num_inference_steps}, guidance={guidance_scale})...")
    
    # Note: We do not need to pass ".to('cuda')" since CPU offloading manages the tensors
    image = pipe(
        prompt=prompt,
        num_inference_steps=num_inference_steps,
        guidance_scale=guidance_scale
    ).images[0]

    # 8. Save output
    output_filename = "lightning_output.png"
    image.save(output_filename)
    print(f"Success! Image successfully saved to '{output_filename}'!")

if __name__ == "__main__":
    main()