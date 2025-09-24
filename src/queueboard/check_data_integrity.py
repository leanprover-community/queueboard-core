#!/usr/bin/env python3

"""
This script checks if all files in the `data` directory are up to date,
by comparing their time stamps with the data in the files `all-open-PRs-{1,2,3}.json`.

This script assumes these files exist.
"""

import json
import glob
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from typing import List, NamedTuple, Tuple

from dateutil import parser

from queueboard.ci_status import CIStatus
from queueboard.compute_dashboard_prs import AggregatePRInfo, infer_pr_url, Label
from queueboard.dashboard_data import parse_aggregate_file
from queueboard.util import eprint, parse_json_file


# Read the input JSON files, return a dictionary mapping each PR number
# to the (current) last update data github provides.
# This operation is (supposed to be) infallible.
def extract_last_update_from_input() -> dict[int, str]:
    output = dict()
    with (
        open("all-open-PRs-1.json", "r") as file1,
        open("all-open-PRs-2.json", "r") as file2,
        open("all-open-PRs-3.json", "r") as file3,
    ):
        data = json.load(file1)
        for page in data["output"]:
            for entry in page["data"]["search"]["nodes"]:
                output[entry["number"]] = entry["updatedAt"]
        data2 = json.load(file2)
        for page in data2["output"]:
            for entry in page["data"]["search"]["nodes"]:
                output[entry["number"]] = entry["updatedAt"]
        data3 = json.load(file3)
        for page in data3["output"]:
            for entry in page["data"]["search"]["nodes"]:
                output[entry["number"]] = entry["updatedAt"]
    return output


# Check that a timestamp file at 'path' is well-formed;
# print errors to standard output if not.
def _check_timestamp_file(path: str) -> bool:
    is_valid = True
    with open(path, "r") as file:
        content = file.read()
        if not content.endswith("\n"):
            eprint(f'error: timestamp file at path "{path}" should end with a newline')
            is_valid = False
        content = content.removesuffix("\n")
        if "\n" in content:
            eprint(f'error: timestamp file at path "{path}" contains more than one line of content')
            return False
        try:
            _time = parser.isoparse(content)
        except ValueError:
            eprint(f'error: timestamp file at path "{path}" does not contain a valid date and time')
            is_valid = False
    return is_valid


def _check_directory(dir: str, pr_number: int, files: List[str]) -> bool:
    is_valid = True
    for file in files:
        if file == "timestamp.txt":
            is_valid = is_valid and _check_timestamp_file(os.path.join(dir, file))
        else:
            assert file.endswith(".json")
            match parse_json_file(os.path.join(dir, file), str(pr_number)):
                case str(err):
                    eprint(err)
                    is_valid = False
                case dict(_data):
                    pass
    return is_valid


# Check the contents of the data directory; print information about errors to standard error.
# - this contains only directories of the form "PR_number" or "PR_number-basic",
# - no PR has both forms present,
# - each directory only contains the expected files, and these parse successfully.
# Return a tuple (normal, stubborn) of all PR numbers whose data was mal-formed (if any):
# first all normal PRs, then all "stubborn" PRs.
# For each normal PRs, we return the PR number as well as "true" iff the directory was temporary.
def check_data_directory_contents() -> Tuple[List[Tuple[int, bool]], List[int]]:
    data_dirs: List[str] = sorted(os.listdir("data"))
    normal_prs_with_errors = []
    stubborn_prs_with_errors = []
    for dir in data_dirs:
        if dir.endswith("-basic"):
            number = dir.removesuffix("-basic")
            if number in data_dirs:
                eprint(f"error: there is both a normal and a 'basic' data directory for PR {number}")
                normal_prs_with_errors.append((int(number), False))
            expected = ["basic_pr_info.json", "timestamp.txt"]
            files = sorted(os.listdir(os.path.join("data", dir)))
            if files != expected:
                eprint(
                    f"files for PR {number} (in directory {dir}) did not match what I wanted: expected {expected}, got {files}"
                )
                stubborn_prs_with_errors.append(int(number))
                continue
            if not _check_directory(os.path.join("data", dir), int(number), files):
                stubborn_prs_with_errors.append(int(number))
        elif dir.endswith("-temp"):
            number = dir.removesuffix("-temp")
            eprint(f"error: found a temporary directory for PR {number}")
            normal_prs_with_errors.append((int(number), True))
        elif dir.isnumeric():
            expected = ["pr_info.json", "pr_reactions.json", "timestamp.txt"]
            files = sorted(os.listdir(os.path.join("data", dir)))
            if files != expected:
                eprint(f"files for PR {dir} (in directory {dir}) did not match what I wanted: expected {expected}, got {files}")
                normal_prs_with_errors.append((int(dir), False))
                continue
            if not _check_directory(os.path.join("data", dir), int(dir), files):
                normal_prs_with_errors.append((int(dir), False))
        else:
            eprint(f"error: found directory {dir}, which was unexpected")
    # Deduplicate the output: the logic above might add a PR twice.
    return (list(set(sorted(normal_prs_with_errors))), list(set(sorted(stubborn_prs_with_errors))))


# All data we are currently extracting from each PR's aggregate info.
class AggregateData(NamedTuple):
    last_updated: str
    ci_status: CIStatus
    # either "open" or "closed"
    state: str


# Is there valid and complete PR data for a PR numbered |number|?
# Either detailed or basic information counts, assuming all files are intact.
#
# |data_dirs| is the list of all (known/relevant) directories in the |data| dir:
# we pass this as an argument to avoid re-computing it many times.
def _has_valid_entries(data_dirs: List[str], number: int) -> bool:
    has_basic_dir = f"{number}-basic" in data_dirs
    has_std_dir = str(number) in data_dirs
    match (has_basic_dir, has_std_dir):
        case (True, True) | (False, False):
            return False
        case (True, False):
            expected = ["basic_pr_info.json", "timestamp.txt"]
            path = os.path.join("data", f"{number}-basic")
            files = sorted(os.listdir(path))
            return files == expected and _check_directory(path, number, files)
        case (False, True):
            expected = ["pr_info.json", "pr_reactions.json", "timestamp.txt"]
            path = os.path.join("data", str(number))
            files = sorted(os.listdir(path))
            return files == expected and _check_directory(path, number, files)
        case _:
            assert False  # unreachable


comment_second = "-- second attempt for "
comment_third = "-- third attempt for "


# Read the file 'missing_prs.txt', check for entries which can be removed now
# and write out the updated file. Take care to keep manual comments in the file
# (except for obsolete lines '-- second attempt for <N>' or '-- third attempt for <N>').
# Return a list of all PR numbers which are in the new file.
# Prune 'closed_prs_to_backfill.txt' in a similar way; the PRs in that file are not returned.
def prune_missing_prs_files() -> List[int]:
    with open("closed_prs_to_backfill.txt", "r") as file:
        closed_pr_lines = file.read().strip().splitlines()

    def inner(closed_pr_lines: List[str], filename: str) -> List[int]:
        current_lines: List[str] = []
        with open(filename, "r") as file:
            current_lines = file.read().strip().splitlines()

        data_dirs: List[str] = sorted(os.listdir("data"))
        # Remove all superfluous lines: corresponding to PR numbers which have valid entries now.
        # Keep the remaining ones unchanged.
        new_lines = []
        current_missing_prs: List[int] = []
        superfluous: List[int] = []
        comments = []
        for line in current_lines:
            if not line:
                new_lines.append(line)
                continue
            elif line.startswith("--"):
                comments.append(line)
                new_lines.append(line)
                continue
            if _has_valid_entries(data_dirs, int(line)):
                superfluous.append(int(line))
            else:
                new_lines.append(line)
                current_missing_prs.append(int(line))
        for comment in comments:
            if comment.startswith(comment_second):
                nstr = comment.removeprefix(comment_second)
                if int(nstr) not in current_missing_prs and nstr not in closed_pr_lines:
                    print(f"PR {nstr} is marked as 'second attempt', but is fine now --- removing the comment")
                    new_lines.remove(comment)
            elif comment.startswith(comment_third):
                nstr = comment.removeprefix(comment_third)
                if int(nstr) not in current_missing_prs and nstr not in closed_pr_lines:
                    print(f"PR {nstr} is marked as 'third attempt', but is fine now --- removing the comment")
                    new_lines.remove(comment)
        if superfluous:
            eprint(f"{len(superfluous)} PR(s) marked as missing have present entries now, removing: {superfluous}")
        with open(filename, "w") as file:
            file.write("\n".join(new_lines) + "\n")
        return current_missing_prs

    _unused = inner(closed_pr_lines, "closed_prs_to_backfill.txt")
    return inner(closed_pr_lines, "missing_prs.txt")


# Remove broken data for a "normal" PR with number 'number':
# - remove the entire directory of this PR's data,
# - add/update a running comment to 'missing_prs.txt' resp. 'closed_prs_to_backfill.txt'
#   about this being the second (or third) time this PR is downloaded,
# - if there was a comment about the third attempt, i.e. a download failed thrice in a row, mark this PR as stubborn.
# 'prune_missing_prs_files()' ensures that no stale "third attempt" comments are left behind.
# If |is_temporary| is true, remove a '123-temp' directory instead.
# If |no_remove| is true, don't try to remove any directory (but just mark the PR download as failed).
# Recently, the temporary download directories are deleted by the shell script,
# so there is no need to delete them again.
def remove_broken_data(number: int, is_temporary: bool, no_remove: bool) -> None:
    if not no_remove:
        dirname = f"{number}-temp" if is_temporary else str(number)
        shutil.rmtree(os.path.join("data", dirname))

    # Return whether the PR should now be marked as stubborn.
    def _inner(number: int, filename: str) -> bool:
        # NB. We write a comment "second time" to both missing_prs.txt and closed_prs_to_backfill.txt
        # (as we don't know where the original one came from). This causes duplicate messages and entries,
        # but is otherwise harmless.
        with open(filename, "r") as fi:
            content = fi.read().splitlines()
        previous_comments = list(filter(lambda s: s.startswith("-- ") and s.rstrip().endswith(str(number)), content))
        if not previous_comments:
            # No comment about the file: just write a comment 'second' time.
            with open(filename, "a") as fi:
                fi.write(f"{comment_second}{number}\n")
        else:
            assert len(previous_comments) == 1
            new_content = content[:]
            new_content.remove(previous_comments[0])
            if previous_comments[0].startswith(comment_second):
                # Replace "second" by "third" in that line; remove broken data.
                new_content.append(f"{comment_third}{number}")
                with open(filename, "w") as fi:
                    fi.write("\n".join(new_content) + "\n")
            elif previous_comments[0].startswith(comment_third):
                # Remove the comment; remove the PR number from the file (any number of times);
                # write an entry to stubborn_prs.txt instead.
                new_content = [line for line in content if line != str(number)]
                with open(filename, "w") as fi:
                    fi.write("\n".join(new_content) + "\n")
                return True
            else:
                print(f"error: comment {previous_comments} for PR {number} is unexpected; aborting!")
        return False

    newly_stubborn = (_inner(number, "missing_prs.txt"), _inner(number, "closed_prs_to_backfill.txt"))
    if newly_stubborn[0] or newly_stubborn[1]:
        with open("stubborn_prs.txt", "a") as fi:
            fi.write(f"{number}\n")


# All data contained in the files all-open-PRs.json passed to the dashboard.
class RESTData(NamedTuple):
    number: int
    url: str
    author: str
    title: str
    state: str
    updatedAt: str
    labels: List[Label]


# If the aggregate data is less than this amount behind the REST data,
# we don't warn yet (but allow for `gather_stats.sh` to download this normally).
ALLOWED_DELAY_MINS = 12


# Return a list of PR numbers whose aggregate data differs from the REST data,
# and whose aggregate data is not newer than the REST data.
def compare_data_inner(rest: List[RESTData], aggregate: dict[int, AggregatePRInfo]) -> List[int]:
    # Return whether left and right are equal. Print an error if not.
    def different(left, right, field_name, number) -> bool:
        if left != right:
            print(f"mismatched data field '{field_name}' for PR {number}: REST data says {left}, aggregate data {right}")
            return True
        return False

    outdated = []
    # For each PR in the REST data, check if the aggregate data matches.
    # This will overlook aggregate PRs with no REST data; this is fine.
    for pr in rest:
        if pr.number not in aggregate:
            print(f"error: no aggregate data for PR {pr.number}")
            outdated.append(pr.number)
            continue
        agg = aggregate[pr.number]
        if parser.isoparse(pr.updatedAt) < agg.last_updated:
            # If the aggregate information is newer, different data is fine.
            continue
        elif parser.isoparse(pr.updatedAt) <= agg.last_updated + timedelta(minutes=ALLOWED_DELAY_MINS):
            # If the aggregate data just very slightly outdated, we don't warn either.
            continue
        if pr.url != infer_pr_url(pr.number):
            print(f"error for PR {pr.number}: REST data has url {pr.url}, but inferred {infer_pr_url(pr.number)}")
            outdated.append(pr.number)
        elif different(pr.author, agg.author, "author", pr.number):
            outdated.append(pr.number)
        elif different(pr.title, agg.title, "title", pr.number):
            outdated.append(pr.number)
        elif different(pr.state.lower(), agg.state, "state", pr.number):
            outdated.append(pr.number)
        elif different(parser.isoparse(pr.updatedAt), agg.last_updated, "updatedAt", pr.number):
            outdated.append(pr.number)
        else:
            # For PR labels, also normalise the colours into lower-case and sort alphabetically.
            norm1 = [
                Label(lab.name, lab.color.lower(), lab.url.replace(" ", "%20"))
                for lab in sorted(pr.labels, key=lambda lab: lab.name)
            ]
            norm2 = [
                Label(lab.name, lab.color.lower(), lab.url.replace(" ", "%20"))
                for lab in sorted(agg.labels, key=lambda lab: lab.name)
            ]
            if different(norm1, norm2, "labels", pr.number):
                outdated.append(pr.number)
    print(f"Compared information about {len(rest)} PRs, found {len(outdated)} PRs with different data")
    return outdated


# Compare the information from the aggregate data file with the contents of
# a pr_info.json file downloaded via the REST API: the goal is to find PRs
# where the data differs, to find PRs with outdated information sooner.
def compare_data_aggressive() -> List[int]:
    rest_data: List[RESTData] = []
    with open("all-open-PRs-1.json", "r") as fi:
        data1 = json.load(fi)
    with open("all-open-PRs-2.json", "r") as fi:
        data2 = json.load(fi)
    with open("all-open-PRs-3.json", "r") as fi:
        data3 = json.load(fi)
    for data in [data1, data2, data3]:
        for page in data["output"]:
            for pr in page["data"]["search"]["nodes"]:
                parsed_labels = [Label(lab["name"], lab["color"], lab["url"]) for lab in pr["labels"]["nodes"]]
                # dependabot PRs don't have a login name in their REST API data; handle this gracefully.
                if "login" in pr["author"]:
                    author = pr["author"]["login"]
                    url = pr["author"]["url"]
                    if url != f"https://github.com/{author}":
                        print("warning: PR author {author} has URL {url}, which is unexpected", file=sys.stderr)
                else:
                    author = "dependabot?"
                rest_data.append(
                    RESTData(int(pr["number"]), pr["url"], author, pr["title"], pr["state"], pr["updatedAt"], parsed_labels)
                )
    with open(os.path.join("processed_data", "all_pr_data.json"), "r") as f:
        aggregate_data = parse_aggregate_file(json.load(f))
    return compare_data_inner(rest_data, aggregate_data)


def ensure_file(filename):
    """
    Ensure the file exists by joining split parts if necessary.

    Args:
        filename (str): Path to the file (e.g., 'processed_data/all_pr_data.json')

    Returns:
        str: Path to the complete file
    """
    if os.path.exists(filename):
        return filename

    # Look for split parts (e.g., processed_data/all_pr_data.json.aa, processed_data/all_pr_data.json.ab, etc.)
    parts = sorted(glob.glob(f"{filename}.*"))
    # Filter out directories or other non-file matches if any
    parts = [p for p in parts if os.path.isfile(p)]

    if not parts:
        raise FileNotFoundError(f"Neither {filename} nor its split parts were found")

    # Join the parts into the original file
    with open(filename, "wb") as outfile:
        for part in parts:
            with open(part, "rb") as infile:
                outfile.write(infile.read())

    return filename


# Read the last updated fields of the aggregate data file, and compare it with the
# dates from querying github.
def main() -> None:
    outdated_aggressive = compare_data_aggressive()

    (normal_prs_with_errors, stubborn_prs_with_errors) = check_data_directory_contents()
    lines = []
    try:
        with open("broken_pr_data.txt", "r") as fi:
            lines = fi.readlines()
    except FileNotFoundError:
        pass
    for line in lines:
        if line:
            print(f"trace: PR {line.strip()} had broken data; noting for future re-downloads")
            normal_prs_with_errors.append((int(line), True))

    # Prune broken data for all PRs, and remove superfluous entries from 'missing_prs.txt'.
    for pr_number, is_temporary in normal_prs_with_errors:
        remove_broken_data(pr_number, is_temporary, True)
    for pr_number in stubborn_prs_with_errors:
        shutil.rmtree(os.path.join("data", f"{pr_number}-basic"))
    current_missing_entries = prune_missing_prs_files()
    stubborn = f"and {len(stubborn_prs_with_errors)} stubborn " if stubborn_prs_with_errors else ""
    print(f"info: found {len(normal_prs_with_errors)} normal {stubborn}PR(s) with broken data")

    # "Last updated" information as returned from a fresh github query.
    current_last_updated = extract_last_update_from_input()
    # "Last updated" information as found in the aggregate data file.
    aggregate_last_updated: dict[int, AggregateData] = dict()
    with open(ensure_file(os.path.join("processed_data", "all_pr_data.json")), "r") as aggregate_file:
        data = json.load(aggregate_file)
        for pr in data["pr_statusses"]:
            updated = pr["last_updated"]
            ci = pr["CI_status"]
            state = pr["state"]
            aggregate_last_updated[pr["number"]] = AggregateData(updated, CIStatus.from_string(ci), state)

    # All PRs whose aggregate data is at least 10 minutes older than github's current "last update".
    outdated_prs: List[int] = []
    missing_prs = []
    # Note that both "last updated" fields have the same format.
    for pr_number in current_last_updated:
        current_updated = parser.isoparse(current_last_updated[pr_number])
        if pr_number not in aggregate_last_updated:
            print(f"mismatch: missing data for PR {pr_number}")
            missing_prs.append(pr_number)
            continue
        aggregate_updated = parser.isoparse(aggregate_last_updated[pr_number].last_updated)

        # current_updated should be at least as new,
        # aggregate_updated is allowed to lag behind by a small amount.
        if aggregate_updated < current_updated - timedelta(minutes=ALLOWED_DELAY_MINS):
            delta = current_updated - aggregate_updated
            print(f"mismatch: the aggregate file for PR {pr_number} is outdated by {delta}, please re-download!")
            print(f"  the aggregate file says {aggregate_updated}, current last update is {current_updated}")
            outdated_prs.append(pr_number)

    # Check for PRs which are still marked as open in the aggregate data,
    # but are in reality closed (or merged, if into a non-master branch).
    for pr_number in aggregate_last_updated:
        if aggregate_last_updated[pr_number].state == "open":
            if pr_number not in current_last_updated:
                print(f"mismatch: the aggregate file says PR {pr_number} is still open, which is wrong.")
                outdated_prs.append(pr_number)

    # Also check for PRs whose aggregate CI data is "almost surely not up to date".
    # Most commonly, this is about CI which is "running", but whose last update was at least 60 minutes old.
    # (Such runs are almost certainly already complete. 60 minutes is rather conservative).
    # Another, very rare, possibility is PR whose CI data is `None`. In both cases, we ask for re-downloading.
    ci_limit = 60
    for pr_number in aggregate_last_updated:
        ci_status = aggregate_last_updated[pr_number].ci_status
        if ci_status == CIStatus.Running and aggregate_updated < datetime.now(timezone.utc) - timedelta(minutes=ci_limit):
            print(
                f"outdated data: the aggregate data for PR {pr_number} claims CI is still running, "
                f"but was last updated more than {ci_limit} minutes ago"
            )
            outdated_prs.append(pr_number)
        elif ci_status == CIStatus.Missing and aggregate_last_updated[pr_number].state == "open":
            print(f"outdated data: PR {pr_number} has missing CI data")
            # When there are actual PRs to re-download, don't include PRs with merely missing CI data in them.
            # Future: if all commits are old enough, don't re-ask at all.
            if len(outdated_prs) < 5:
                outdated_prs.append(pr_number)

    outdated_prs.extend(outdated_aggressive)

    # Some PRs are marked as stubborn: for them, only basic information is downloaded.
    stubborn_prs = []
    with open("stubborn_prs.txt", "r") as file:
        for line in file:
            line = line.strip()
            if not line.startswith("--") and line:
                stubborn_prs.append(int(line))

    # NB. One PR might be missing or outdated in several ways: make sure to deduplicate it.
    missing_prs = sorted(list(set(missing_prs)))
    outdated_prs = sorted(list(set(outdated_prs)))

    # Write out the list of missing PRs.
    if missing_prs:
        print(f"SUMMARY: found {len(missing_prs)} PR(s) whose aggregate information is missing:\n{missing_prs}", file=sys.stderr)
        # Append any 'newly' missing PRs to the file.
        new_missing_entries = [n for n in missing_prs if n not in current_missing_entries and n not in stubborn_prs]
        # No need to shuffle this list: gather_stats.sh skips PRs with existing
        # broken data, so each PR is tried at most once anyway.
        if new_missing_entries:
            print(f"info: adding PR(s) {new_missing_entries} as missing")
            with open("missing_prs.txt", "a") as file:
                file.write("\n".join([str(n) for n in new_missing_entries]) + "\n")
            print("  Scheduled all PRs for backfilling")
    if outdated_prs:
        print(
            f"SUMMARY: the data integrity check found {len(outdated_prs)} PRs with outdated aggregate information:\n{outdated_prs}"
        )
        # Batch the PRs to to re-download: write the first 5 PRs into redownload.txt,
        # if that file is basically empty (i.e. no other files to already handle).
        # The next run of this script will pick this up and try to download them.
        with open("redownload.txt", "r") as file:
            content2 = file.read().strip().splitlines()
        if len(content2) != 5 and len(content2) > 1:
            return
        with open("redownload.txt", "w") as file:
            # Shuffle the list of outdated PRs, to avoid this getting stuck in a loop
            # of trying and failing to re-download the same PR over and over.
            import random

            random.shuffle(outdated_prs)
            file.write("\n".join([str(n) for n in outdated_prs[: min(5, len(outdated_prs))]]) + "\n")
        # Write all outdated PRs to a file "outdated_prs.txt". That file is not committed,
        # but is used to inform the reviewer suggestion algorithm.
        with open("outdated_prs.txt", "w") as fi:
            fi.write("\n".join([str(n) for n in outdated_prs]) + "\n")
    else:
        print("All PR aggregate data appears up to date, congratulations!")


main()
