import io
import sys

from PIL import Image


content = sys.stdin.buffer.read()
output = io.BytesIO()
with Image.open(io.BytesIO(content)) as image:
    image.save(output, format='PNG', optimize=True)
sys.stdout.buffer.write(output.getvalue())
sys.stdout.buffer.flush()
