import torch
import cv2
import numpy as np
import os

# Set local cache directory for torch
base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
torch.hub.set_dir(os.path.join(base_dir, '.cache', 'torch', 'hub'))

# Load model once
model_type = "MiDaS_small"
midas = torch.hub.load("intel-isl/MiDaS", model_type, trust_repo=True)
midas.eval()

transform = torch.hub.load("intel-isl/MiDaS", "transforms", trust_repo=True).small_transform


def generate_depth_map(input_path, output_path):
    img = cv2.imread(input_path)
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    input_batch = transform(img)

    with torch.no_grad():
        prediction = midas(input_batch)
        prediction = torch.nn.functional.interpolate(
            prediction.unsqueeze(1),
            size=img.shape[:2],
            mode="bicubic",
            align_corners=False,
        ).squeeze()

    depth = prediction.cpu().numpy()

    # Normalize
    depth = (depth - depth.min()) / (depth.max() - depth.min())
    depth = (depth * 255).astype(np.uint8)

    cv2.imwrite(output_path, depth)

    return output_path