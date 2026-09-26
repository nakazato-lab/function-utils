"""Run a mounted-in-image .ndn client script through ndnc."""
import asyncio
import logging
import os
from pathlib import Path

from utils.nfd import wait_for_nfd


logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')


async def main():
    script_path = Path(os.environ['NDN_SCRIPT_PATH'])
    if not script_path.is_file():
        raise FileNotFoundError(f'NDN script not found: {script_path}')
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
