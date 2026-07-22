from enumerations import JSONFields, Login, NameStatus, Responses, GameState, TeamState, GamePlay
from fastapi import FastAPI, WebSocket, Depends, WebSocketDisconnect
import time
import json
import httpx
import asyncio
import random
import os
from huggingface_hub import AsyncInferenceClient
from io import BytesIO
import base64
import open_clip
from PIL import Image
import torch
import re
import nltk
from nltk.corpus import words
import urllib.parse
from dotenv import load_dotenv

load_dotenv()
HF_TOKEN = os.getenv("HF_TOKEN")
if not HF_TOKEN:
    raise RuntimeError("System configuration error: HF_TOKEN missing from environment.")

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

image_comparator, _, preprocess = open_clip.create_model_and_transforms('ViT-B-32', pretrained='laion2b_s34b_b79k')
image_comparator.eval()
custom_timeout = httpx.Timeout(60.0, connect=10.0, read=None, write=20.0)
imagegen_client = AsyncInferenceClient(
        model="stabilityai/stable-diffusion-xl-base-1.0",  # Free tier friendly
        token=HF_TOKEN,            # Explicit token argument
        timeout=custom_timeout
    )

def load_image(img_input):
    if isinstance(img_input, str) and img_input.startswith("data:image"):
        img_input = img_input.split("base64,")[1]
        return Image.open(BytesIO(base64.b64decode(img_input)))
    elif isinstance(img_input, str) and img_input.startswith("/static/"):
        path = img_input.lstrip("/")
        return Image.open(path)
    else:
        return Image.open(img_input)

    
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
    image1 = preprocess(load_image(original)).unsqueeze(0)
    image2 = preprocess(load_image(new)).unsqueeze(0)
    
    with torch.no_grad(), torch.amp.autocast('cpu'):
        feat1 = image_comparator.encode_image(image1)
        feat2 = image_comparator.encode_image(image2)
        feat1 /= feat1.norm(dim=-1, keepdim=True)
        feat2 /= feat2.norm(dim=-1, keepdim=True)
        similarity = (feat1 @ feat2.T).item()
    
    sim_clipped = max(0, min(similarity, 1))
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