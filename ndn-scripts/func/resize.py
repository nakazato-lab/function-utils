import io
import sys

from PIL import Image


content = sys.stdin.buffer.read()
width_ratio = float(sys.argv[1])
height_ratio = float(sys.argv[2])
output = io.BytesIO()
with Image.open(io.BytesIO(content)) as image:
    size = (round(image.width * width_ratio), round(image.height * height_ratio))
    resized = image.resize(size, Image.Resampling.LANCZOS)
    resized.save(output, format='PNG')
sys.stdout.buffer.write(output.getvalue())
sys.stdout.buffer.flush()
