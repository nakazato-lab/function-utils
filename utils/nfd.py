"""Wait for the NFD endpoint published by the NFD ConfigMap."""
import asyncio
from pathlib import Path
from urllib.parse import urlparse


async def wait_for_nfd(config_path, timeout=90):
    path = Path(config_path)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            endpoint = path.read_text().strip()
            if not endpoint or endpoint == 'not available yet':
                raise ValueError('NFD endpoint is not configured yet')
            uri = urlparse(endpoint if '://' in endpoint else 'tcp://' + endpoint)
            if uri.scheme not in ('tcp', 'tcp4', 'tcp6') or not uri.hostname:
                raise ValueError('NFD endpoint must be a TCP address')
            host, port = uri.hostname, uri.port or 6363
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 3)
            writer.close()
            await writer.wait_closed()
            return host, port
        except (OSError, ValueError, asyncio.TimeoutError):
            await asyncio.sleep(2)
    raise RuntimeError('NFD was not ready within 90 seconds')
