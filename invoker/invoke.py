"""Run .ndn source in the Invoker process and save binary results."""
import asyncio
import logging
import os
import tempfile
from pathlib import Path

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from utils.nfd import wait_for_nfd
from utils.ndn_runtime import execute_script


logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
tracer = trace.get_tracer(__name__)


@tracer.start_as_current_span('invoker.run')
async def main():
    source = os.environ['NDN_SCRIPT_SOURCE']
    script_name = os.environ['NDN_SCRIPT_NAME']
    trace.get_current_span().set_attribute('ndn.script.name', script_name)
    if trace.get_current_span().is_recording():
        logging.info('Trace ID: %032x', trace.get_current_span().get_span_context().trace_id)
    if not script_name.endswith('.ndn') or Path(script_name).name != script_name:
        raise ValueError('NDN_SCRIPT_NAME must be an .ndn filename')
    if not source.strip() or '\x00' in source:
        raise ValueError('NDN_SCRIPT_SOURCE must be nonempty text without NUL characters')
    with tempfile.TemporaryDirectory(prefix='ndn-invoke-') as directory:
        script_path = Path(directory) / script_name
        script_path.write_text(source, encoding='utf-8')
        await run_script(script_path)


async def run_script(script_path):
    with tracer.start_as_current_span('invoker.wait_for_nfd'):
        host, port = await wait_for_nfd(os.environ['NFD_CONFIG_PATH'])
    os.environ['NDN_CLIENT_TRANSPORT'] = f'tcp4://{host}:{port}'
    # The DHCP initContainer writes ROUTER_NAME to the shared volume mounted at /etc/ndn-config.
    # The Job sets NFD_NAME_PATH to /etc/ndn-config/ROUTER_NAME.
    # The Invoker reads it to pass the connected router's prefix to ndnc.
    router_name = Path(os.getenv('NFD_NAME_PATH', '/etc/ndn-config/ROUTER_NAME')).read_text(encoding='utf-8').strip()
    os.environ['NDN_ROUTER_PREFIX'] = '/' + router_name.strip('/')
    output_dir = os.getenv('NDN_OUTPUT_DIR')
    source = script_path.read_text(encoding='utf-8')
    deadline = asyncio.get_running_loop().time() + 180
    attempt = 0
    while asyncio.get_running_loop().time() < deadline:
        attempt += 1
        logging.info('Running NDN script: %s', script_path)
        try:
            # The Interpreter inherits the active span in this process.
            with tracer.start_as_current_span('invoker.execution', attributes={'retry.attempt': attempt}):
                result = await execute_script(source, script_path.parent)
        except Exception as exc:
            logging.info('Function route/response not ready: %s', exc)
            await asyncio.sleep(2)
            continue
        if output_dir and isinstance(result, bytes):
            with tracer.start_as_current_span('invoker.save_result') as span:
                output = Path(output_dir) / f'{script_path.stem}.bin'
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(result)
                span.set_attribute('file.path', str(output))
                span.set_attribute('file.size', len(result))
                logging.info('Saved result to %s', output)
        return
    raise RuntimeError('NDN script did not complete within 180 seconds')


if __name__ == '__main__':
    provider = None
    if os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT'):
        provider = TracerProvider(resource=Resource.create({
            'service.name': os.getenv('OTEL_SERVICE_NAME', 'invoker'),
        }))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
    try:
        asyncio.run(main())
    except Exception as exc:
        logging.error('Function invocation failed: %s', exc)
        raise SystemExit(1)
    finally:
        if provider is not None:
            provider.shutdown()
