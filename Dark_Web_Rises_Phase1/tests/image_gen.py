import asyncio
import httpx
from huggingface_hub import AsyncInferenceClient

# Paste your actual Hugging Face token here
HF_TOKEN = "your_hf_token_here" 
# Use your current model name, or leave this standard one to test
MODEL_NAME = "stabilityai/stable-diffusion-xl-base-1.0" 

async def test_generation():
    # We set read=None to completely remove the timeout limit while waiting for data
    custom_timeout = httpx.Timeout(60.0, connect=10.0, read=None, write=20.0)
    
    print("Initializing client...")
    client = AsyncInferenceClient(
        model="stabilityai/stable-diffusion-xl-base-1.0",  # Free tier friendly
        token="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",            # Explicit token argument
        timeout=custom_timeout
    )
    
    # Test with a foolproof prompt first
    prompt = "A simple red apple on a wooden table, studio lighting"
    print(f"Sending request for prompt: '{prompt}'...")
    
    try:
        image = await client.text_to_image(prompt=prompt)
        
        # Save it to see if we actually got the bytes
        image.save("test_output.png")
        print("Success! Image saved as test_output.png")
        
    except httpx.ReadError as e:
        print(f"\n[ReadError] The server still dropped the connection: {e}")
    except Exception as e:
        print(f"\n[Unexpected Error] Something else went wrong: {type(e).__name__}: {e}")

# Run the async loop
if __name__ == "__main__":
    asyncio.run(test_generation())