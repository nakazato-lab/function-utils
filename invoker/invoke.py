"""Run .ndn source provided by the Job through ndnc."""
import asyncio
import base64
import json
import logging
import os
import tempfile
from pathlib import Path

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from utils.nfd import wait_for_nfd


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
    environment = os.environ.copy()
    environment['NDN_CLIENT_TRANSPORT'] = f'tcp4://{host}:{port}'
    # The DHCP initContainer writes ROUTER_NAME to the shared volume mounted at /etc/ndn-config.
    # The Job sets NFD_NAME_PATH to /etc/ndn-config/ROUTER_NAME.
    # The Invoker reads it to pass the connected router's prefix to ndnc.
    router_name = Path(os.getenv('NFD_NAME_PATH', '/etc/ndn-config/ROUTER_NAME')).read_text(encoding='utf-8').strip()
    environment['NDN_ROUTER_PREFIX'] = '/' + router_name.strip('/')
    environment['OTEL_SERVICE_NAME'] = 'ndn-interpreter'
    result_path = script_path.with_suffix('.json')
    output_dir = os.getenv('NDN_OUTPUT_DIR')
    command = ['ndnc', 'run', str(script_path)]
    if output_dir:
        command.extend(['--result-file', str(result_path)])
    deadline = asyncio.get_running_loop().time() + 180
    attempt = 0
    while asyncio.get_running_loop().time() < deadline:
        attempt += 1
        logging.info('Running NDN script: %s', script_path)
        with tracer.start_as_current_span('invoker.ndnc', attributes={'retry.attempt': attempt}) as span:
            # Carry the active span to the ndnc subprocess through environment variables.
            carrier = {}
            # Write the current span's context into carrier, e.g. {'traceparent': '00-<Trace ID>-<Span ID>-01'}.
            TraceContextTextMapPropagator().inject(carrier)
            # Add it to the subprocess environment as TRACEPARENT.
            environment.update({key.upper(): value for key, value in carrier.items()})
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            stdout, stderr = await process.communicate()
            span.set_attribute('process.exit.code', process.returncode)
            if process.returncode != 0:
                span.set_status(trace.Status(trace.StatusCode.ERROR, 'ndnc execution failed'))
        if process.returncode == 0:
            if stderr:
                logging.info('ndnc stderr: %s', stderr.decode('utf-8', errors='replace').strip())
            print(stdout.decode('utf-8'), end='', flush=True)
            if output_dir:
                with tracer.start_as_current_span('invoker.save_result') as span:
                    result = json.loads(result_path.read_text(encoding='utf-8'))
                    if isinstance(result, dict) and result.get('encoding') == 'base64':
                        output = Path(output_dir) / f'{script_path.stem}.bin'
                        output.parent.mkdir(parents=True, exist_ok=True)
                        data = base64.b64decode(result['data'])
                        output.write_bytes(data)
                        span.set_attribute('file.path', str(output))
                        span.set_attribute('file.size', len(data))
                        logging.info('Saved result to %s', output)
            return
        logging.info('Function route/response not ready: %s',
                     stderr.decode('utf-8', errors='replace').strip())
        await asyncio.sleep(2)
    raise RuntimeError('ndnc run did not complete within 180 seconds')


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
