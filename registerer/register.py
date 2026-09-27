"""Register an .ndn function with the Manager through NFD."""
import asyncio
import json
import logging
import os
from pathlib import Path

from ndn.app import NDNApp
from ndn.encoding import Name
from ndn.security import KeychainDigest
from ndn.transport.stream_face import TcpFace
from ndn.types import InterestNack, InterestTimeout

from utils.nfd import wait_for_nfd


logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')


async def main():
    function_path = Path(os.environ['FUNCTION_PATH'])
    function_name = function_path.stem
    operation = os.getenv('FUNCTION_OPERATION', 'CREATE')
    if operation not in ('CREATE', 'DELETE'):
        raise ValueError('FUNCTION_OPERATION must be CREATE or DELETE')
    request = {'name': function_name}
    host, port = await wait_for_nfd(os.environ['NFD_CONFIG_PATH'])
    app = NDNApp(face=TcpFace(host, port), keychain=KeychainDigest())
    params = json.dumps(request).encode('utf-8')
    register_name = (os.getenv('MANAGER_DELETE_NAME', '/Manager/delete')
                     if operation == 'DELETE' else
                     os.getenv('MANAGER_REGISTER_NAME', '/Manager/register'))

    async def register():
        try:
            logging.info('Sending register Interest: %s (name=%s, file=%s)',
                         register_name, function_name, function_path)
            _, _, content = await app.express_interest(
                Name.from_str(register_name), app_param=params,
                must_be_fresh=True, can_be_prefix=False, lifetime=120000)
            response = bytes(content or b'').decode('utf-8')
            logging.info('Manager response: %s', response)
            print(response, flush=True)
            if not response.startswith('Success:'):
                raise RuntimeError(f'Manager rejected {operation}: {response}')
        except InterestNack as exc:
            raise RuntimeError(f'Manager NACK: {exc.reason}') from exc
        except InterestTimeout as exc:
            raise RuntimeError('Timeout waiting for Manager response') from exc
        finally:
            app.shutdown()

    await app.main_loop(after_start=register())


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        logging.error('Function registration failed: %s', exc)
        raise SystemExit(1)
