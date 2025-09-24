#!/usr/bin/env python3

"""Send a zulip direct message to a user. Can be used for the generate "reviewer assignment" page.

This is slightly non-trivial as there is a bug in zulip-send to work around. This script does that!
"""

import argparse
import json
import zulip
import ast


def send_dm(config_file, to, message):
    client = zulip.Client(config_file=config_file)
    request = {
        "type": "private",
        "to": to,
        "content": message,
    }
    result = client.send_message(request)
    print(json.dumps(result, indent=4))


def main():
    parser = argparse.ArgumentParser(description="Send a direct message to Zulip.")
    parser.add_argument("-c", "--config", type=str, default="~/.zuliprc", help="Path to the zuliprc file (default: ~/.zuliprc)")
    parser.add_argument(
        "-t", "--to", type=str, required=True, help='List of user IDs to send the message to, e.g., "[123456, 456789]"'
    )
    parser.add_argument("-m", "--message", type=str, required=True, help="Content of the message")

    args = parser.parse_args()
    to_list = ast.literal_eval(args.to)
    send_dm(args.config, to_list, args.message)


if __name__ == "__main__":
    main()
