"""`helm test`: checks that a release answers. Reads its checks from $CHECKS (JSON):
http checks need a status of exactly 200 (redirects are not followed); tcp checks connect,
optionally complete a TLS handshake (without verifying the certificate, which may not
exist yet), and read the protocol's greeting."""
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request

TIMEOUT = 10
GREETING_LIMIT = 512


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a 3xx answer then raises HTTPError instead of being followed


def http(check):
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(check["url"], timeout=TIMEOUT) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
    if status != 200:
        raise RuntimeError(f"expected HTTP 200, got {status}")
    return "HTTP 200"


def read_line(sock):
    """The first line of the greeting, which TCP may deliver in several pieces."""
    data = b""
    while b"\n" not in data and len(data) < GREETING_LIMIT:
        chunk = sock.recv(GREETING_LIMIT - len(data))
        if not chunk:
            break
        data += chunk
    return data.split(b"\n", 1)[0].decode("utf-8", "replace").strip()


def tcp(check):
    sock = socket.create_connection((check["host"], check["port"]), timeout=TIMEOUT)
    with sock:
        if check.get("tls"):
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            sock = context.wrap_socket(sock, server_hostname=check.get("sni") or None)
            detail = sock.version()
        else:
            detail = "connected"
        greeting = check.get("greeting")
        if greeting:
            line = read_line(sock)
            if not line.startswith(greeting):
                raise RuntimeError(f"expected a greeting starting with {greeting!r}, got {line[:80]!r}")
            detail += f", {line[:60]}"
        return detail


def main(checks):
    failed = 0
    for check in checks:
        try:
            result = (http if check["type"] == "http" else tcp)(check)
            print(f"ok    {check['name']}: {result}")
        except Exception as error:  # noqa: BLE001 - every failure is reported the same way
            failed += 1
            print(f"FAIL  {check['name']}: {error}")
    print(f"{failed} check(s) failed" if failed else "all checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(json.loads(os.environ["CHECKS"])))
