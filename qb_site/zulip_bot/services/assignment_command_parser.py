from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

GITHUB_PR_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)(?:[/?#][^\s]*)?",
    re.IGNORECASE,
)
RAW_ZULIP_MENTION_RE = re.compile(r"@\*\*(?P<label>.+?)\*\*")
RAW_ZULIP_SILENT_MENTION_RE = re.compile(r"@_\*\*(?P<label>.+?)\*\*")
RAW_PR_LINKIFIER_TOKEN_RE = re.compile(r"(?:^|\s)(?:#\d+|[A-Za-z0-9_.-]+#\d+)(?=$|\s)")


@dataclass(frozen=True)
class GitHubPullRequestRef:
    owner: str
    repo: str
    number: int


@dataclass(frozen=True)
class ParsedAssignmentCommand:
    pr: GitHubPullRequestRef
    target_user_ids: tuple[int, ...]
    unresolved_mentions: tuple[str, ...]
    mention_labels_by_user_id: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class AssignmentCommandParseError(ValueError):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


class _RenderedContentExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.mention_user_ids: list[int] = []
        self.unresolved_mentions: list[str] = []
        self.mention_labels_by_user_id: dict[int, str] = {}
        self._open_mention_user_id: int | None = None
        self._open_mention_label_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key: value for key, value in attrs}
        if tag == "a":
            href = attrs_map.get("href")
            if isinstance(href, str) and href.strip():
                self.hrefs.append(href.strip())

        class_name = attrs_map.get("class") or ""
        classes = {token for token in class_name.split() if token}
        if not classes.intersection({"user-mention", "mention"}):
            return

        raw_id = attrs_map.get("data-user-id")
        if isinstance(raw_id, str) and raw_id.isdigit():
            mention_user_id = int(raw_id)
            self.mention_user_ids.append(mention_user_id)
            self._open_mention_user_id = mention_user_id
            self._open_mention_label_parts = []
            return

        label = (
            attrs_map.get("data-user-id")
            or attrs_map.get("data-user-email")
            or attrs_map.get("data-user-name")
            or attrs_map.get("title")
            or "unknown mention"
        )
        self.unresolved_mentions.append(str(label))

    def handle_data(self, data: str) -> None:
        if self._open_mention_user_id is None:
            return
        if data:
            self._open_mention_label_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "span" or self._open_mention_user_id is None:
            return
        label = _normalize_mention_label("".join(self._open_mention_label_parts))
        if label and self._open_mention_user_id not in self.mention_labels_by_user_id:
            self.mention_labels_by_user_id[self._open_mention_user_id] = label
        self._open_mention_user_id = None
        self._open_mention_label_parts = []


def parse_assignment_command_args(*, args: str, rendered_content: str | None, sender_id: int | None) -> ParsedAssignmentCommand:
    """Parse PR target and reviewer mentions for assign/unassign commands."""
    pr = _parse_single_pr_ref(args=args, rendered_content=rendered_content)
    parsed_mentions = _parse_mentions(args=args, rendered_content=rendered_content)
    unexpected_tokens = _find_unexpected_tokens(args=args)
    if unexpected_tokens:
        rendered_tokens = ", ".join(f"`{token}`" for token in unexpected_tokens)
        raise AssignmentCommandParseError(
            code="unexpected_args",
            message=f"Unexpected argument token(s): {rendered_tokens}. Use: <pr> <optional zulip mention(s)>.",
        )

    if parsed_mentions.resolved_user_ids:
        target_user_ids = parsed_mentions.resolved_user_ids
    elif parsed_mentions.mentions_present:
        target_user_ids = ()
    elif sender_id is not None:
        target_user_ids = (sender_id,)
    else:
        raise AssignmentCommandParseError(
            code="missing_sender",
            message="Could not determine default reviewer because sender_id is missing.",
        )

    return ParsedAssignmentCommand(
        pr=pr,
        target_user_ids=target_user_ids,
        unresolved_mentions=parsed_mentions.unresolved_mentions,
        mention_labels_by_user_id=parsed_mentions.mention_labels_by_user_id,
    )


def _find_unexpected_tokens(*, args: str) -> tuple[str, ...]:
    remaining = args.strip()
    if not remaining:
        return ()

    remaining = GITHUB_PR_URL_RE.sub(" ", remaining)
    remaining = RAW_ZULIP_MENTION_RE.sub(" ", remaining)
    remaining = RAW_ZULIP_SILENT_MENTION_RE.sub(" ", remaining)
    remaining = RAW_PR_LINKIFIER_TOKEN_RE.sub(" ", remaining)
    remaining = re.sub(r"[,\s]+", " ", remaining).strip()
    if not remaining:
        return ()
    return tuple(token for token in remaining.split(" ") if token)


def _parse_single_pr_ref(*, args: str, rendered_content: str | None) -> GitHubPullRequestRef:
    matches: set[GitHubPullRequestRef] = set()
    matches.update(_extract_pr_refs(args))
    if rendered_content:
        extractor = _RenderedContentExtractor()
        extractor.feed(rendered_content)
        for href in extractor.hrefs:
            matches.update(_extract_pr_refs(href))

    if not matches:
        raise AssignmentCommandParseError(
            code="missing_pr",
            message="No GitHub pull request link found. Include exactly one PR URL or Zulip linkifier.",
        )

    if len(matches) > 1:
        sorted_refs = sorted(matches, key=lambda pr: (pr.owner.lower(), pr.repo.lower(), pr.number))
        refs = ", ".join(f"{ref.owner}/{ref.repo}#{ref.number}" for ref in sorted_refs)
        raise AssignmentCommandParseError(
            code="ambiguous_pr",
            message=f"Found multiple PR references: {refs}. Include only one PR.",
        )

    return next(iter(matches))


def _extract_pr_refs(content: str) -> set[GitHubPullRequestRef]:
    refs: set[GitHubPullRequestRef] = set()
    for match in GITHUB_PR_URL_RE.finditer(content):
        refs.add(
            GitHubPullRequestRef(
                owner=match.group("owner"),
                repo=match.group("repo"),
                number=int(match.group("number")),
            )
        )
    return refs


@dataclass(frozen=True)
class _ParsedMentions:
    resolved_user_ids: tuple[int, ...]
    unresolved_mentions: tuple[str, ...]
    mentions_present: bool
    mention_labels_by_user_id: tuple[tuple[int, str], ...]


def _parse_mentions(*, args: str, rendered_content: str | None) -> _ParsedMentions:
    rendered_resolved_ids: list[int] = []
    rendered_unresolved: list[str] = []
    rendered_labels_by_id: dict[int, str] = {}
    if rendered_content:
        extractor = _RenderedContentExtractor()
        extractor.feed(rendered_content)
        rendered_resolved_ids = extractor.mention_user_ids
        rendered_unresolved = extractor.unresolved_mentions
        rendered_labels_by_id = extractor.mention_labels_by_user_id

    arg_mentions = [
        _normalize_mention_label(match.group("label"))
        for match in RAW_ZULIP_MENTION_RE.finditer(args)
        if _normalize_mention_label(match.group("label"))
    ]
    arg_mentions.extend(
        _normalize_mention_label(match.group("label"))
        for match in RAW_ZULIP_SILENT_MENTION_RE.finditer(args)
        if _normalize_mention_label(match.group("label"))
    )
    arg_mention_set = set(arg_mentions)

    if arg_mention_set:
        filtered_resolved_ids: list[int] = []
        filtered_labels_by_id: dict[int, str] = {}
        for mention_id in rendered_resolved_ids:
            normalized_label = _normalize_mention_label(rendered_labels_by_id.get(mention_id, ""))
            if normalized_label in arg_mention_set:
                filtered_resolved_ids.append(mention_id)
                if normalized_label:
                    filtered_labels_by_id[mention_id] = normalized_label
        rendered_resolved_ids = filtered_resolved_ids
        rendered_labels_by_id = filtered_labels_by_id
        rendered_unresolved = [label for label in rendered_unresolved if _normalize_mention_label(label) in arg_mention_set]
    else:
        rendered_resolved_ids = []
        rendered_labels_by_id = {}
        rendered_unresolved = []

    mentions_present = bool(rendered_resolved_ids or rendered_unresolved or arg_mentions)

    resolved_user_ids = tuple(sorted(set(rendered_resolved_ids)))
    unresolved_mentions: set[str] = set(rendered_unresolved)
    if arg_mentions and not resolved_user_ids:
        unresolved_mentions.update(arg_mentions)

    return _ParsedMentions(
        resolved_user_ids=resolved_user_ids,
        unresolved_mentions=tuple(sorted(unresolved_mentions)),
        mentions_present=mentions_present,
        mention_labels_by_user_id=tuple(sorted(rendered_labels_by_id.items(), key=lambda item: item[0])),
    )


def _normalize_mention_label(value: str) -> str:
    label = re.sub(r"\s+", " ", value.strip())
    if label.startswith("@_"):
        label = label[2:].strip()
    elif label.startswith("@"):
        label = label[1:].strip()
    return label
