"""Register a function with the Manager through NFD."""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

from ndn.encoding import Name
from ndn.security import KeychainDigest
from ndn.transport.stream_face import TcpFace
from ndn.types import InterestNack, InterestTimeout
from segmented_node_wrapper import SegmentedNodeWrapper
from opentelemetry import context, trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from utils.nfd import wait_for_nfd
from utils.ndn_routing import RoutedNDNApp
from utils.tracing import setup_tracing


logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

tracer = trace.get_tracer(__name__)

FUNCTION_UPLOAD_PREFIX = '/registerer/functions'
FUNCTION_REGISTRATION_TIMEOUT = 150


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
        request['runtime'] = runtime
        package_path = os.getenv('FUNCTION_PACKAGE_PATH')
        if package_path:
            # Store the ZIP file's bytes.
            request['zip_content'] = Path(package_path).read_bytes()
            request['source'] = os.environ['FUNCTION_SOURCE']
        else:
            code = os.environ['FUNCTION_CODE']
            if not code.strip() or '\x00' in code:
                raise ValueError('FUNCTION_CODE must be nonempty text without NUL characters')
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

        # 要求リソース指定:
        # 1. FUNCTION_DEMAND_* (絶対値: CPUコア数, メモリBytes, 帯域bps) が指定されている場合
        # 2. 指定されていない場合は従来の FUNCTION_PREFERENCE_* (high/medium/low) を使用
        demand = {}
        for key in ('cpu', 'memory', 'bandwidth'):
            env_val = os.getenv(f'FUNCTION_DEMAND_{key.upper()}')
            if env_val is not None and env_val.strip():
                try:
                    demand[key] = float(env_val.strip())
                except ValueError as exc:
                    raise ValueError(f'FUNCTION_DEMAND_{key.upper()} must be a valid number') from exc
        if demand:
            request['demand'] = demand

        # preference が明示的に指定されている場合、または demand が指定されていない場合は preference を設定
        preference_specified = any(os.getenv(f'FUNCTION_PREFERENCE_{k.upper()}') for k in ('cpu', 'memory', 'bandwidth'))
        if preference_specified or not demand:
            preference = {key: os.getenv(f'FUNCTION_PREFERENCE_{key.upper()}', 'medium')
                          for key in ('cpu', 'memory', 'bandwidth')}
            for key, value in preference.items():
                if value not in ('high', 'medium', 'low'):
                    raise ValueError(f'FUNCTION_PREFERENCE_{key.upper()} must be high, medium, or low')
            request['preference'] = preference
    return operation, request


class Registerer:
    def __init__(self, operation, request):
        self.operation = operation
        self.request = request
        self.function_name = request['name']
        self.register_name = (os.getenv('MANAGER_DELETE_NAME', '/Manager/delete')
                              if operation == 'DELETE' else
                              os.getenv('MANAGER_REGISTER_NAME', '/Manager/register'))
        self.app = None
        self.wait_until_function_result = asyncio.Event()
        self.result_ref = None
        self.registration_context = None
        self.result_context = None

    async def run(self):
        self.registration_context = context.get_current()
        span = trace.get_current_span()
        span.set_attribute('ndn.function.name', self.function_name)
        if span.is_recording():
            logging.info('Trace ID: %032x', span.get_span_context().trace_id)
        with tracer.start_as_current_span('registerer.wait_for_nfd'):
            host, port = await wait_for_nfd(os.environ['NFD_CONFIG_PATH'])
        self.app = RoutedNDNApp(face=TcpFace(host, port), keychain=KeychainDigest())
        await self.app.main_loop(after_start=self.register())

    def on_function_placement_result(self, name, _param, app_param):
        if not app_param:
            return
        received_at = time.time_ns()
        try:
            notification = json.loads(bytes(app_param))
            result_ref = notification['result_ref']
            parent = TraceContextTextMapPropagator().extract(notification, context=self.registration_context)
        except (ValueError, UnicodeDecodeError, KeyError, TypeError) as exc:
            with tracer.start_as_current_span(
                    'registerer.receive.result', context=self.registration_context,
                    kind=trace.SpanKind.SERVER, start_time=received_at) as span:
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, str(exc))
            logging.warning('Invalid placement result notification: %s', Name.to_str(name))
            return
        with tracer.start_as_current_span(
                'registerer.receive.result', context=parent, kind=trace.SpanKind.SERVER,
                start_time=received_at, attributes={'ndn.name': Name.to_str(name)}):
            self.result_context = context.get_current()
            self.result_ref = result_ref
            self.wait_until_function_result.set()

    async def fetch_function_result(self, result_ref):
        with tracer.start_as_current_span(
                'registerer.fetch.result', context=self.result_context, kind=trace.SpanKind.CLIENT,
                attributes={'ndn.name': result_ref}) as span:
            content = await SegmentedNodeWrapper(self.app).fetch(result_ref, must_be_fresh=True)
            result = json.loads(content)
            if (not isinstance(result, dict)
                    or result.get('name') != Name.to_str(Name.normalize('/' + self.function_name.strip('/')))):
                raise RuntimeError(f'Placement result does not match function {self.function_name}: {result_ref}')
            span.set_attribute('k8s.pod.phase', result.get('phase', 'Unknown'))
            if result.get('phase') != 'Running' or result.get('logs') or result.get('log_errors'):
                reason = result.get('reason') or 'Function startup failed'
                span.set_status(trace.StatusCode.ERROR, reason)
                trace.get_current_span(self.registration_context).set_status(trace.StatusCode.ERROR, reason)
            logs = result.get('logs')
            if logs:
                for error_log in logs.values():
                    logging.error('%s', error_log)
            else:
                logging.info('Function placement result: %s', json.dumps(result, ensure_ascii=False))

    async def register(self):
        deadline = asyncio.get_running_loop().time() + FUNCTION_REGISTRATION_TIMEOUT
        try:
            if self.operation == 'CREATE':
                await self.publish_function()
                await self.request_registration(deadline)
                await self.wait_for_placement_result(deadline)
            else:
                await self.request_registration(deadline)
        finally:
            self.app.shutdown()

    @tracer.start_as_current_span('registerer.publish')
    async def publish_function(self):
        upload_prefix = f'/{get_router_prefix()}{FUNCTION_UPLOAD_PREFIX}'
        function_prefix = f'{upload_prefix}/{self.function_name.strip("/")}'
        # Seed sends the function placement result to function_prefix.
        if not await self.app.register(function_prefix, self.on_function_placement_result):
            raise RuntimeError(f'Cannot register notification prefix: {function_prefix}')
        self.request['registerer_prefix'] = function_prefix
        publisher = SegmentedNodeWrapper(self.app, serve=True)
        zip_content = self.request.pop('zip_content', None)
        if zip_content is not None:
            with tracer.start_as_current_span('registerer.publish.package'):
                self.request['zip_content'] = await publisher.publish(
                    f'{function_prefix}/package', zip_content)
        else:
            with tracer.start_as_current_span('registerer.publish.code'):
                self.request['code_ref'] = await publisher.publish(
                    f'{function_prefix}/code', self.request.pop('_code').encode('utf-8'))

    @tracer.start_as_current_span('registerer.manager.request', kind=trace.SpanKind.CLIENT)
    async def request_registration(self, deadline):
        TraceContextTextMapPropagator().inject(self.request)
        params = json.dumps(self.request, ensure_ascii=False).encode('utf-8')
        logging.info('Sending %s Interest: %s (ApplicationParameters=%s)',
                     self.operation, self.register_name, params.decode('utf-8'))
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise RuntimeError('Function registration deadline exceeded before contacting Manager')
        try:
            _, _, content = await self.app.express_interest(
                Name.from_str(self.register_name), app_param=params,
                must_be_fresh=True, can_be_prefix=False, lifetime=min(120000, max(1, int(remaining * 1000))))
        except InterestNack as exc:
            raise RuntimeError(f'Manager NACK: {exc.reason}') from exc
        except InterestTimeout as exc:
            raise RuntimeError('Timeout waiting for Manager response') from exc
        response = bytes(content or b'').decode('utf-8')
        if not response.startswith('Success:'):
            raise RuntimeError(f'Manager rejected {self.operation}: {response}')

    async def wait_for_placement_result(self, deadline):
        logging.info('Waiting for Seed to report Function placement result: %s', self.function_name)
        try:
            with tracer.start_as_current_span('registerer.wait.result'):
                await asyncio.wait_for(
                    self.wait_until_function_result.wait(), timeout=max(0, deadline - asyncio.get_running_loop().time()))
            await asyncio.wait_for(
                self.fetch_function_result(self.result_ref),
                timeout=max(0, deadline - asyncio.get_running_loop().time()))
        except asyncio.TimeoutError as exc:
            raise RuntimeError('Timed out waiting for Function placement result from Seed') from exc


@tracer.start_as_current_span('registerer.run')
async def main():
    with tracer.start_as_current_span('registerer.prepare'):
        operation, request = build_request()
    await Registerer(operation, request).run()


if __name__ == '__main__':
    provider = setup_tracing('registerer')
    try:
        asyncio.run(main())
    except Exception as exc:
        logging.error('Function registration failed: %s', exc)
        raise SystemExit(1)
    finally:
        if provider is not None:
            provider.shutdown()
