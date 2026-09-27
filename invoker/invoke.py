"""Run .ndn source provided by the Job through ndnc."""
import asyncio
import logging
import os
import tempfile
from pathlib import Path

from utils.nfd import wait_for_nfd


logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')


async def main():
    source = os.environ['NDN_SCRIPT_SOURCE']
    script_name = os.environ['NDN_SCRIPT_NAME']
    if not script_name.endswith('.ndn') or Path(script_name).name != script_name:
        raise ValueError('NDN_SCRIPT_NAME must be an .ndn filename')
    if not source.strip() or '\x00' in source:
        raise ValueError('NDN_SCRIPT_SOURCE must be nonempty text without NUL characters')
    with tempfile.TemporaryDirectory(prefix='ndn-invoke-') as directory:
        script_path = Path(directory) / script_name
        script_path.write_text(source, encoding='utf-8')
        await run_script(script_path)


async def run_script(script_path):
    host, port = await wait_for_nfd(os.environ['NFD_CONFIG_PATH'])
    environment = os.environ.copy()
    environment['NDN_CLIENT_TRANSPORT'] = f'tcp4://{host}:{port}'
    deadline = asyncio.get_running_loop().time() + 180
    while asyncio.get_running_loop().time() < deadline:
        logging.info('Running NDN script: %s', script_path)
        process = await asyncio.create_subprocess_exec(
            'ndnc', 'run', str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            if stderr:
                logging.info('ndnc stderr: %s', stderr.decode('utf-8', errors='replace').strip())
            print(stdout.decode('utf-8'), end='', flush=True)
            return
        logging.info('Function route/response not ready: %s',
                     stderr.decode('utf-8', errors='replace').strip())
        await asyncio.sleep(2)
    raise RuntimeError('ndnc run did not complete within 180 seconds')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        logging.error('Function invocation failed: %s', exc)
        raise SystemExit(1)
