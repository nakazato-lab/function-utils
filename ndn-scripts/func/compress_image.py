import io

from PIL import Image


def execute(input_bytes, args):
    output = io.BytesIO()
    with Image.open(io.BytesIO(input_bytes)) as image:
        image.save(output, format='PNG', optimize=True)
    return output.getvalue()
