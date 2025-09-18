#!/usr/bin/env python3

"""
This file contains various utility functions, which are needed in several otherwise unrelated scripts.
Currently, this contains the following
- a function to parse JSON files with PR info (with error handling),
- a helper for comparing lists of PR numbers (with detailed information about the differences)
- a function to format a |relativedelta|
"""

import json
import sys
from typing import List
from dateutil import relativedelta
from datetime import timedelta


def eprint(val):
    print(val, file=sys.stderr)


# Parse the JSON file 'name' for PR 'number'. Returned the parsed file if successful,
# and an error message describing what went wrong otherwise.
def parse_json_file(name: str, pr_number: str) -> dict | str:
    data = None
    with open(name, "r") as fi:
        try:
            data = json.load(fi)
        except json.decoder.JSONDecodeError:
            return f"error: the file {name} for PR {pr_number} is invalid JSON, ignoring"
    if "errors" in data:
        return f"warning: the data for PR {pr_number} is incomplete, ignoring"
    elif "data" not in data:
        return f"warning: the data for PR {pr_number} is incomplete (perhaps a time out downloading it), ignoring"
    return data


# Compare two lists of PR numbers for equality, printing informative output if different.
def my_assert_eq(msg: str, left: List[int], right: List[int]) -> bool:
    if left != right:
        print(f"assertion failure comparing {msg}\n  found {len(left)} PR(s) on the left, {len(right)} PR(s) on the right", file=sys.stderr)
        left_sans_right = set(left) - set(right)
        right_sans_left = set(right) - set(left)
        if left_sans_right:
            print(f"  the following {len(left_sans_right)} PR(s) are contained in left, but not right: {sorted(left_sans_right)}", file=sys.stderr)
        if right_sans_left:
            print(f"  the following {len(right_sans_left)} PR(s) are contained in right, but not left: {sorted(right_sans_left)}", file=sys.stderr)
        return False
    return True


def format_delta(delta: relativedelta.relativedelta) -> str:
    def pluralize(n: int, s: str) -> str:
        return f"{n} {s}" if n == 1 else f"{n} {s}s"
    if delta.years > 0:
        return pluralize(delta.years, "year")
    elif delta.months > 0:
        return pluralize(delta.months, "month")
    elif delta.days > 0:
        return pluralize(delta.days, "day")
    elif delta.hours > 0:
        return pluralize(delta.hours, "hour")
    elif delta.minutes > 0:
        return pluralize(delta.minutes, "minute")
    else:
        return pluralize(delta.seconds, "second")

# We consciously do not use the repr() instance on timedelta, as this does not round-trip:
# it displays everything with hours and minutes, even when the interval representation might differ.
#
# NB. This representation ignores microseconds, as we don't need them.
def timedelta_tostr(delta: timedelta) -> str:
    # This is not producing zero-padded outputs; that is fine.
    res = f"timedelta(days={delta.days}, seconds={delta.seconds})"
    back = timedelta_tryParse(res)
    assert back == timedelta(days=delta.days, seconds=delta.seconds), f"mismatch for {delta}, stringified {res} gets re-parsed as {back}"
    return res

# Inverse to timedelta_tryParse.
def timedelta_tryParse(value: str) -> timedelta | None:
    if not (value.startswith("timedelta(") and value.endswith(")")):
        print(f"bad input: {value}")
        return None
    inner = value[len("timedelta("):][:-1]
    attrs = {}
    parts = [p for p in inner.split(", ") if p]
    for part in parts:
        (attr, val) = part.split('=')
        attrs[attr] = int(val)
    return timedelta(**attrs)

# Expects input from |value|'s repr instance, i.e. of the form
# "relativedelta(days=2, hours=4)".
def relativedelta_tryParse(value: str) -> relativedelta.relativedelta | None:
    if not (value.startswith("relativedelta(") and value.endswith(")")):
        print(f"bad input: {value}")
        return None
    inner = value[len("relativedelta("):][:-1]
    attrs = {}
    parts = [p for p in inner.split(", ") if p]
    for part in parts:
        (attr, val) = part.split('=')
        attrs[attr] = int(val)
    return relativedelta.relativedelta(**attrs)
