import torch
from PIL import Image
import open_clip
import base64
from io import BytesIO

# 1. Setup Model
model, _, preprocess = open_clip.create_model_and_transforms('ViT-B-32', pretrained='laion2b_s34b_b79k')
model.eval()

def load_image(img_input):
    if isinstance(img_input, str) and img_input.startswith("data:image"):
        img_input = img_input.split("base64,")[1]
        return Image.open(BytesIO(base64.b64decode(img_input)))
    elif isinstance(img_input, str) and img_input.startswith("http://localhost"):
        # load from file directly instead of HTTP
        path = img_input.replace("http://localhost:8000/", "")
        return Image.open(path)
    else:
        return Image.open(img_input)

def get_score(img1, img2):
    image1 = preprocess(load_image(img1)).unsqueeze(0)
    image2 = preprocess(load_image(img2)).unsqueeze(0)
    
    with torch.no_grad(), torch.amp.autocast('cpu'):
        feat1 = model.encode_image(image1)
        feat2 = model.encode_image(image2)
        feat1 /= feat1.norm(dim=-1, keepdim=True)
        feat2 /= feat2.norm(dim=-1, keepdim=True)
        similarity = (feat1 @ feat2.T).item()
    
    sim_clipped = max(0, min(similarity, 1))
    return round(sim_clipped * 100, 2)