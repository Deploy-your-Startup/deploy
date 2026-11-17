"""Verify an opted-in deployment endpoint without exposing URLs or credentials."""
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def report(message):
    print(message)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a') as summary:
            summary.write(message + '\n')


def main():
    url = os.environ.get('STARTUP_HEALTH_URL', '')
    if not url:
        report('Deployment health: skipped; no health_url configured. Live health is unverified.')
        return
    parts = urlsplit(url)
    if (parts.scheme != 'https' or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment):
        raise ValueError('health_url must be HTTPS without credentials, query or fragment')
    timeout = int(os.environ.get('STARTUP_HEALTH_TIMEOUT', '120'))
    if not 1 <= timeout <= 600:
        raise ValueError('Health timeout must be between 1 and 600 seconds')
    deadline = time.monotonic() + timeout
    opener = build_opener(NoRedirect)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Health endpoint did not return HTTP 2xx within the timeout')
        try:
            with opener.open(Request(url, headers={'User-Agent': 'startup-deployment-health'}),
                             timeout=min(10, remaining)) as response:
                if 200 <= response.status < 300:
                    report('Deployment health: HTTPS endpoint returned HTTP 2xx.')
                    return
        except (HTTPError, URLError, TimeoutError, OSError):
            pass
        time.sleep(min(2, max(0, deadline - time.monotonic())))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never include server responses, URLs or credentials in logs.
        report('Deployment health: failed (' + type(error).__name__ + ').')
        raise SystemExit(1) from None
