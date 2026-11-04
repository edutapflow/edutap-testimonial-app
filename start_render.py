"""Start the authenticated Streamlit server on Render."""
import os
from pathlib import Path


def get_port(value):
    try:
        port = int(value)
    except ValueError:
        raise ValueError('PORT must be an integer between 1 and 65535') from None
    if not 1 <= port <= 65535:
        raise ValueError('PORT must be an integer between 1 and 65535')
    return port


if __name__ == '__main__':
    import uvicorn
    from authenticated_server import create_application
    os.chdir(Path(__file__).resolve().parent)
    uvicorn.run(create_application(), host='0.0.0.0', port=get_port(os.getenv('PORT', '10000')),
                access_log=False, proxy_headers=True, forwarded_allow_ips='*',
                timeout_graceful_shutdown=20)
