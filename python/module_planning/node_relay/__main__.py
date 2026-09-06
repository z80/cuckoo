"""Run a node relay server from the command line."""

import argparse
import asyncio

from .server import create_relay_server


def _arguments():
    parser = argparse.ArgumentParser(description="Relay a PC NRF node over ZMQ")
    parser.add_argument("service", choices=("transport", "hardware"))
    parser.add_argument("port", help="USB serial port, for example COM8")
    parser.add_argument(
        "bind",
        nargs="?",
        default="tcp://127.0.0.1:43840",
        help="ZMQ bind endpoint",
    )
    parser.add_argument("--baudrate", type=int, default=115200)
    return parser.parse_args()


async def _main(args):
    server = await create_relay_server(
        args.service,
        port=args.port,
        bind=args.bind,
        baudrate=args.baudrate,
    )
    print("relay", args.service, "listening on", args.bind)
    try:
        await server.serve_forever()
    finally:
        await server.close()


def main():
    args = _arguments()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
