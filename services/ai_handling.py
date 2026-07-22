import httpx
import random
import os
from huggingface_hub import AsyncInferenceClient
from io import BytesIO
import base64
import numpy as np
from PIL import Image
import re
import nltk
from nltk.corpus import words
import urllib.parse
from dotenv import load_dotenv

load_dotenv()
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    raise RuntimeError("System configuration error: HF_TOKEN missing from environment.")


HF_API_URL = "https://api-inference.huggingface.co/pipeline/feature-extraction/openai/clip-vit-base-patch32"
HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"}

nltk.download('words', quiet=True)
english_words = set(w.lower() for w in words.words())

COMMON_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "can", "could", "should", "may", "might", "shall",
    "i", "you", "he", "she", "it", "we", "they",
    "this", "that", "these", "those",
    "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "and", "but", "or", "so", "because", "if", "then",
    "my", "your", "his", "her", "its", "our", "their",
    "what", "who", "where", "when", "how", "why"
}
'''image_comparator, preprocess_train, preprocess_val = open_clip.create_model_and_transforms(
    'RN50', 
    pretrained='openai' # or 'laion400m_e32'
)
image_comparator.eval()
gc.collect()'''
custom_timeout = httpx.Timeout(60.0, connect=10.0, read=None, write=20.0)
imagegen_client = AsyncInferenceClient(
        model="stabilityai/stable-diffusion-xl-base-1.0",  # Free tier friendly
        token=HF_TOKEN,            # Explicit token argument
        timeout=custom_timeout
    )



def load_image_bytes(img_input) -> bytes:
    """Loads image and converts it directly to bytes for HF API transmission."""
    if isinstance(img_input, str) and img_input.startswith("data:image"):
        img_input = img_input.split("base64,")[1]
        return base64.b64decode(img_input)
    elif isinstance(img_input, str) and img_input.startswith("/static/"):
        path = img_input.lstrip("/")
        with open(path, "rb") as f:
            return f.read()
    elif isinstance(img_input, Image.Image):
        buf = BytesIO()
        img_input.save(buf, format="PNG")
        return buf.getvalue()
    else:
        with open(img_input, "rb") as f:
            return f.read()


async def get_image_embedding(
    client: httpx.AsyncClient, img_bytes: bytes
) -> np.ndarray:
    """Calls HF Inference API to get feature embeddings for an image."""
    response = await client.post(HF_API_URL, headers=HEADERS, data=img_bytes)

    if response.status_code != 200:
        raise Exception(f"HF API Error ({response.status_code}): {response.text}")

    # API returns vector embedding array
    embedding = np.array(response.json())
    return embedding
    
'''async def get_image(prompt):
    if prompt == "default_prompt":
        folder_path = os.path.join("static", "images")
        try:
            all_files = os.listdir(folder_path)
        except FileNotFoundError:
            print("folder path doesnt exist")
            return "/static/default.png"
        
        valid_extensions = (".png", ".jpeg", ".jpg", ".gif")

        image_files = [f for f in all_files if f.lower().endswith(valid_extensions)]

        if not image_files:
            print("no images in folder")
            return "/static/default.png"

        random_filename = random.choice(image_files)
        
        return f"http://127.0.0.1:8000/static/images/{random_filename}"
    else:
        image = await imagegen_client.text_to_image(
        prompt,
        model="black-forest-labs/FLUX.1-schnell" 
        )
        buffer = BytesIO()
        image.save(buffer, format="JPEG")
        image_base64 = base64.b64encode(buffer.getvalue()).decode()
        return f"data:image/jpeg;base64,{image_base64}"'''

async def get_image(prompt):
    if prompt == "default_prompt":
        folder_path = os.path.join("static", "images")
        try:
            all_files = os.listdir(folder_path)
        except FileNotFoundError:
            print("folder path doesnt exist")
            return "/static/default.png"
        
        valid_extensions = (".png", ".jpeg", ".jpg", ".gif")
        image_files = [f for f in all_files if f.lower().endswith(valid_extensions)]
 
        if not image_files:
            print("no images in folder")
            return "/static/default.png"
 
        random_filename = random.choice(image_files)
        return f"/static/images/{random_filename}"
        
    else:
        encoded_prompt = urllib.parse.quote(prompt)
        
        url = f"https://image.pollinations.ai/p/{encoded_prompt}?model=flux&width=1024&height=1024"
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=30.0)
                
                if response.status_code == 200:
                    image_base64 = base64.b64encode(response.content).decode()
                    return f"data:image/jpeg;base64,{image_base64}"
                else:
                    print(f"Pollinations error: Status code {response.status_code}")
                    return "/static/default.png"
                    
        except Exception as e:
            print(f"Failed to fetch image from Pollinations: {e}")
            return "/static/default.png"


async def compare_image(penalty, original, new):
    img1_bytes = load_image_bytes(original)
    img2_bytes = load_image_bytes(new)

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Get embeddings from HF Serverless API
        feat1 = await get_image_embedding(client, img1_bytes)
        feat2 = await get_image_embedding(client, img2_bytes)

    # L2 Normalize vectors (Equivalent to: feat /= feat.norm(dim=-1, keepdim=True))
    feat1 = feat1 / np.linalg.norm(feat1)
    feat2 = feat2 / np.linalg.norm(feat2)

    # Cosine Similarity (Equivalent to: (feat1 @ feat2.T).item())
    similarity = float(np.dot(feat1, feat2))

    # Clipping and score calculation identical to your original formula
    sim_clipped = max(0.0, min(similarity, 1.0))
    return round((sim_clipped * 100) - penalty, 2)

def classify_prompt(prompt):
    if not prompt or not prompt.strip():
        return False
    
    words_list = prompt.strip().lower().split()
    
    if len(words_list) < 4:
        return False
    
    letters = sum(c.isalpha() for c in prompt)
    if letters / len(prompt) < 0.6:
        return False
    
    if re.search(r'(.)\1{4,}', prompt):
        return False
    
    if not any(word in COMMON_WORDS for word in words_list):
        return False
    
    # stricter check for longer prompts
    if len(words_list) >= 8:
        real_words = sum(1 for w in words_list if w in english_words)
        if real_words / len(words_list) < 0.5:
            return False
    
    return True
