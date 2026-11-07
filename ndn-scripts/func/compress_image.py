import io
import sys

from ndn.app import NDNApp
from ndn.encoding import Component, Name
from ndn.security import KeychainDigest
from PIL import Image
from segmented_node_wrapper import SegmentedNodeWrapper


app = NDNApp(keychain=KeychainDigest())


async def main():
    try:
        name, _, content = await app.express_interest(
            sys.argv[1], can_be_prefix=True, must_be_fresh=True)
        if Component.get_type(name[-1]) == Component.TYPE_SEGMENT:
            content = await SegmentedNodeWrapper(app).fetch(
                Name.to_str(name[:-1]), must_be_fresh=True)
        output = io.BytesIO()
        with Image.open(io.BytesIO(bytes(content))) as image:
            image.save(output, format='PNG', optimize=True)
        sys.stdout.buffer.write(output.getvalue())
        sys.stdout.buffer.flush()
    finally:
        app.shutdown()


if __name__ == '__main__':
    app.run_forever(after_start=main())
