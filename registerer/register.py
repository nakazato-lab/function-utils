"""Register a function with the Manager through NFD."""
import asyncio
import json
import logging
import os
from pathlib import Path

from ndn.encoding import Name
from ndn.security import KeychainDigest
from ndn.transport.stream_face import TcpFace
from ndn.types import InterestNack, InterestTimeout
from segmented_node_wrapper import SegmentedNodeWrapper

from utils.nfd import wait_for_nfd
from utils.ndn_routing import RoutedNDNApp


logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

FUNCTION_UPLOAD_PREFIX = '/registerer/functions'


def get_router_prefix():
    path = os.getenv('NFD_NAME_PATH', '/etc/ndn-config/ROUTER_NAME')
    router_name = Path(path).read_text(encoding='utf-8').strip()
    return router_name


def build_request():
    function_name = os.environ['FUNCTION_NAME']
    if not function_name.strip():
        raise ValueError('FUNCTION_NAME must not be empty')
    operation = os.getenv('FUNCTION_OPERATION', 'CREATE')
    if operation not in ('CREATE', 'DELETE'):
        raise ValueError('FUNCTION_OPERATION must be CREATE or DELETE')
    request = {'name': function_name}
    if operation == 'CREATE':
        runtime = os.environ['FUNCTION_RUNTIME']
        if runtime not in ('ndn', 'python'):
            raise ValueError('FUNCTION_RUNTIME must be ndn or python')
        code = os.environ['FUNCTION_CODE']
        if not code.strip() or '\x00' in code:
            raise ValueError('FUNCTION_CODE must be nonempty text without NUL characters')
        request['runtime'] = runtime
        # Do not include in ApplicationParameters.
        request['_code'] = code
        try:
            requirements = json.loads(os.getenv('FUNCTION_REQUIREMENTS', '[]'))
        except json.JSONDecodeError as exc:
            raise ValueError('FUNCTION_REQUIREMENTS must be a JSON array') from exc
        if (not isinstance(requirements, list) or any(
                not isinstance(requirement, str) or not requirement.strip()
                or '\x00' in requirement or '\n' in requirement or '\r' in requirement
                for requirement in requirements)):
            raise ValueError('FUNCTION_REQUIREMENTS must be a list of nonempty single-line strings')
        if runtime != 'python' and requirements:
            raise ValueError('FUNCTION_REQUIREMENTS is supported only for python functions')
        request['requirements'] = requirements
        preference = {key: os.getenv(f'FUNCTION_PREFERENCE_{key.upper()}', 'medium')
                      for key in ('cpu', 'memory', 'bandwidth')}
        for key, value in preference.items():
            if value not in ('high', 'medium', 'low'):
                raise ValueError(f'FUNCTION_PREFERENCE_{key.upper()} must be high, medium, or low')
        request['preference'] = preference
    return operation, request


async def main():
    operation, request = build_request()
    function_name = request['name']
    upload_prefix = None
    if operation == 'CREATE':
        router_name = get_router_prefix()
        upload_prefix = f'/{router_name}{FUNCTION_UPLOAD_PREFIX}'
    host, port = await wait_for_nfd(os.environ['NFD_CONFIG_PATH'])
    app = RoutedNDNApp(face=TcpFace(host, port), keychain=KeychainDigest())
    register_name = (os.getenv('MANAGER_DELETE_NAME', '/Manager/delete')
                     if operation == 'DELETE' else
                     os.getenv('MANAGER_REGISTER_NAME', '/Manager/register'))

    async def register():
        try:
            if operation == 'CREATE':
                publisher = SegmentedNodeWrapper(app, serve=True)
                request['code_ref'] = await publisher.publish(
                    f'{upload_prefix}/{function_name.lstrip("/")}',
                    request.pop('_code').encode('utf-8'))
            params = json.dumps(request, ensure_ascii=False).encode('utf-8')
            logging.info('Sending %s Interest: %s (ApplicationParameters=%s)',
                         operation, register_name, params.decode('utf-8'))
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
