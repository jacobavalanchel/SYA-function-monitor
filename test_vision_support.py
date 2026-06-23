import requests, json, base64, sys

url = "https://aigc-api.hkust-gz.edu.cn/v1/chat/completions"

API_KEY = sys.argv[1] if len(sys.argv) > 1 else ""
if not API_KEY:
    print("Usage: python3 test_vision_support.py <YOUR_API_KEY>")
    sys.exit(1)

headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}"
}

# Create a tiny 8x8 red square as a test image
import struct, zlib
def make_red_png():
    width, height = 8, 8
    raw = b''
    for y in range(height):
        raw += b'\x00' + b'\xff\x00\x00' * width
    def chunk(ctype, data):
        c = ctype + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')

img_b64 = base64.b64encode(make_red_png()).decode()

# Test 1: List models
print("=== Test 1: List models ===")
try:
    r = requests.get("https://aigc-api.hkust-gz.edu.cn/v1/models", headers=headers, timeout=15)
    print(f"Status: {r.status_code}")
    data = r.json()
    if "data" in data:
        for m in data["data"]:
            print(f"  - {m.get('id', m)}")
    else:
        print(r.text[:500])
except Exception as e:
    print(f"Error: {e}")

# Test 2: Vision with gpt-4-turbo
print("\n=== Test 2: gpt-4-turbo with image ===")
payload = {
    "model": "gpt-4-turbo",
    "messages": [{
        "role": "user",
        "content": [
            {"type": "text", "text": "What color is this image? Reply in one word."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}}
        ]
    }],
    "max_tokens": 50
}
try:
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    print(f"Status: {r.status_code}")
    print(r.json())
except Exception as e:
    print(f"Error: {e}")

# Test 3: Vision with Qwen
print("\n=== Test 3: Qwen with image ===")
payload["model"] = "Qwen"
try:
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    print(f"Status: {r.status_code}")
    print(r.json())
except Exception as e:
    print(f"Error: {e}")
