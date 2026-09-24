"""Small ComfyUI nodes owned by narration-video-gen."""

from pathlib import Path

import cv2
import folder_paths
import numpy as np


class NVGDetectPrimaryFace:
    """Detect the largest face in the first frame and return a SAM2 bbox."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "model_name": ("STRING", {"default": "face_detection_yunet_2023mar.onnx"}),
                "score_threshold": ("FLOAT", {
                    "default": 0.7, "min": 0.0, "max": 1.0, "step": 0.01,
                }),
            },
        }

    RETURN_TYPES = ("BBOX",)
    RETURN_NAMES = ("face_bbox",)
    FUNCTION = "detect"
    CATEGORY = "narration-video-gen"

    def detect(self, image, model_name, score_threshold):
        if len(image) == 0:
            raise ValueError("Cannot detect a face in an empty image batch")

        model_path = Path(folder_paths.models_dir) / "face_detection" / model_name
        if not model_path.is_file():
            raise ValueError("Face detector model is missing: %s" % model_path)

        frame = (image[0].detach().cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
        height, width = frame.shape[:2]
        detector = cv2.FaceDetectorYN.create(
            str(model_path), "", (width, height), float(score_threshold), 0.3, 5000
        )
        _retval, faces = detector.detect(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
        if faces is None or len(faces) == 0:
            raise ValueError(
                "No face was detected in the first frame. Use a custom recipe with "
                "recorded face points for this input."
            )

        # Portrait workflows have one subject. If another face is present, the
        # largest face is the most stable definition of the primary subject.
        face = max(faces, key=lambda row: float(row[2] * row[3]))
        x, y, box_width, box_height = (float(value) for value in face[:4])
        x1 = max(0.0, x)
        y1 = max(0.0, y)
        x2 = min(float(width - 1), x + box_width)
        y2 = min(float(height - 1), y + box_height)
        if x2 <= x1 or y2 <= y1:
            raise ValueError("The detected face bounding box is empty")

        print("Primary face detected: %.0f,%.0f to %.0f,%.0f" % (x1, y1, x2, y2))
        return ([[x1, y1, x2, y2]],)


NODE_CLASS_MAPPINGS = {
    "NVGDetectPrimaryFace": NVGDetectPrimaryFace,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "NVGDetectPrimaryFace": "NVG Detect Primary Face",
}
