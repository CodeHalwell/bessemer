"""One verified TLS 1.3 HTTP/1 request per connection, with explicit resumption."""

import socket
import ssl
import sys


def main():
    port, certificate, count, mode, route = sys.argv[1:]
    context = ssl.create_default_context(cafile=certificate)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.set_alpn_protocols(["http/1.1"])
    session = None
    resumed = 0
    request = (
        f"GET {route} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
    ).encode()
    for _ in range(int(count)):
        with (
            socket.create_connection(("127.0.0.1", int(port)), timeout=30) as raw,
            context.wrap_socket(
                raw, server_hostname="localhost", session=session
            ) as connection,
        ):
            resumed += connection.session_reused
            connection.sendall(request)
            response = bytearray()
            while chunk := connection.recv(16384):
                response.extend(chunk)
            if not response.startswith(b"HTTP/1.1 200 "):
                raise RuntimeError(f"unexpected response: {response[:100]!r}")
            head, separator, body = response.partition(b"\r\n\r\n")
            lengths = [
                int(line.split(b":", 1)[1])
                for line in head.split(b"\r\n")[1:]
                if line.lower().startswith(b"content-length:")
            ]
            if not separator or lengths != [len(body)]:
                raise RuntimeError("response missing or mismatching Content-Length")
            if mode == "tls-resume":
                session = connection.session
                if not session.has_ticket:
                    raise RuntimeError("server did not issue a TLS 1.3 session ticket")
    expected = int(count) - 1 if mode == "tls-resume" else 0
    if resumed != expected:
        raise RuntimeError(f"expected {expected} resumed connections, got {resumed}")
    print(f"200 responses: {count}; TLS 1.3 resumed: {resumed}")


if __name__ == "__main__":
    main()
