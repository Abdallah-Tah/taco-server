#!/home/abdaltm86/Documents/blink-cam/.venv311/bin/python3
import face_recognition
from PIL import Image
import sys
import json

try:
    img = Image.open(sys.argv[1])
    w, h = img.size
    pixels = list(img.getdata())
    avg_brightness = sum(sum(p) for p in pixels) / (len(pixels) * 3)

    if avg_brightness < 20:
        print(json.dumps({"ok": False, "error": "Image is too dark (avg brightness: " + str(round(avg_brightness)) + "). Use a well-lit photo."}))
        sys.exit(0)

    if w < 200 or h < 200:
        print(json.dumps({"ok": False, "error": "Image is too small (" + str(w) + "x" + str(h) + "). Minimum 200x200 required."}))
        sys.exit(0)

    known_img = face_recognition.load_image_file(sys.argv[1])
    locs = face_recognition.face_locations(known_img)

    if len(locs) == 0:
        print(json.dumps({"ok": False, "error": "No face detected. Upload a clear photo with a visible face."}))
        sys.exit(0)

    if len(locs) > 1:
        print(json.dumps({"ok": False, "error": "Multiple faces detected (" + str(len(locs)) + "). Upload a photo with exactly one person."}))
        sys.exit(0)

    top, right, bottom, left = locs[0]
    face_w = right - left
    face_h = bottom - top
    face_area = face_w * face_h
    img_area = w * h
    face_ratio = face_area / img_area

    if face_ratio < 0.02:
        print(json.dumps({"ok": False, "error": "Face is too small in the image (" + str(round(face_ratio*100, 1)) + "%). Move closer or crop the photo."}))
        sys.exit(0)

    encs = face_recognition.face_encodings(known_img, locs)
    if len(encs) == 0:
        print(json.dumps({"ok": False, "error": "Face detected but could not generate encoding. Try a clearer photo."}))
        sys.exit(0)

    print(json.dumps({"ok": True, "faces": len(locs), "face_ratio": round(face_ratio*100, 1), "brightness": round(avg_brightness), "size": str(w) + "x" + str(h)}))
except Exception as e:
    print(json.dumps({"ok": False, "error": "Validation failed: " + str(e)}))
