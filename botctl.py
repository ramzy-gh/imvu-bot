"""Run inside the bot container: python botctl.py join OWNER-ROOM."""
import argparse
import asyncio
import sys

from local_control import send_command


def main():
    parser = argparse.ArgumentParser(description="Control the running RAMSEI bot from its container shell")
    parser.add_argument("command", choices=("rooms", "join", "leave", "help"))
    parser.add_argument("room", nargs="?")
    args = parser.parse_args()
    if (args.command in {"join", "leave"}) != (args.room is not None):
        parser.error("join/leave require a room ID; rooms/help take no room ID")
    command = args.command + (" " + args.room if args.room else "")
    try:
        result = asyncio.run(send_command(command))
    except (OSError, ValueError, KeyError, asyncio.TimeoutError):
        print("Cannot reach the bot. Enable TERMINAL_CONTROL=1, wait for startup, and run this inside the same bot container.", file=sys.stderr)
        return 1
    print(result["message"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
