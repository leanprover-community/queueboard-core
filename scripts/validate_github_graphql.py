from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import requests

SCHEMA_PATH = Path(os.environ.get("GITHUB_GRAPHQL_SCHEMA_PATH", "docs/graphql/github-schema.json"))

INTROSPECTION_QUERY = """
query IntrospectionQuery {
  __schema {
    queryType { name }
    mutationType { name }
    subscriptionType { name }
    types {
      ...FullType
    }
    directives {
      name
      description
      locations
      args {
        ...InputValue
      }
    }
  }
}

fragment FullType on __Type {
  kind
  name
  description
  fields(includeDeprecated: true) {
    name
    description
    args {
      ...InputValue
    }
    type {
      ...TypeRef
    }
    isDeprecated
    deprecationReason
  }
  inputFields {
    ...InputValue
  }
  interfaces {
    ...TypeRef
  }
  enumValues(includeDeprecated: true) {
    name
    description
    isDeprecated
    deprecationReason
  }
  possibleTypes {
    ...TypeRef
  }
}

fragment InputValue on __InputValue {
  name
  description
  type { ...TypeRef }
  defaultValue
}

fragment TypeRef on __Type {
  kind
  name
  ofType {
    kind
    name
    ofType {
      kind
      name
      ofType {
        kind
        name
        ofType {
          kind
          name
          ofType {
            kind
            name
            ofType {
              kind
              name
            }
          }
        }
      }
    }
  }
}
""".strip()


def _github_token() -> str | None:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token or token == "local-dev-token":
        try:
            token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
        except Exception:
            return None
    return token or None


def _post_graphql(token: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    resp = requests.post(
        "https://api.github.com/graphql",
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def _load_query(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _download_schema(token: str) -> Path:
    schema_path = SCHEMA_PATH
    try:
        schema_path.parent.mkdir(parents=True, exist_ok=True)
        data = _post_graphql(token, INTROSPECTION_QUERY)
        schema_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return schema_path
    except OSError:
        fallback = Path(os.environ.get("GITHUB_GRAPHQL_SCHEMA_FALLBACK", "/tmp/queueboard-github-schema.json"))
        fallback.parent.mkdir(parents=True, exist_ok=True)
        data = _post_graphql(token, INTROSPECTION_QUERY)
        fallback.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return fallback


def _require_no_errors(label: str, payload: dict[str, Any]) -> None:
    errors = payload.get("errors") or []
    if errors:
        details = json.dumps(errors, indent=2, sort_keys=True)
        raise RuntimeError(f"{label}: GraphQL errors detected\\n{details}")


def main() -> int:
    token = _github_token()
    if not token:
        print("Skipping GitHub GraphQL validation; GH_TOKEN/GITHUB_TOKEN not set.")
        return 0

    print("Downloading GitHub GraphQL schema...")
    schema_path = _download_schema(token)
    print(f"Schema saved to {schema_path}")

    owner = os.environ.get("GITHUB_GRAPHQL_VALIDATE_OWNER", "leanprover-community")
    name = os.environ.get("GITHUB_GRAPHQL_VALIDATE_NAME", "mathlib4")
    number = int(os.environ.get("GITHUB_GRAPHQL_VALIDATE_NUMBER", "1"))

    base_vars = {"owner": owner, "name": name, "number": number}

    bundle_query = _load_query(Path("qb_site/syncer/queries/pr_bundle.graphql"))
    bundle_vars = {
        **base_vars,
        "timelineK": 2,
        "commitsM": 1,
        "inlineCommentsPerReview": 5,
        "timelineSince": None,
    }
    print("Validating pr_bundle.graphql...")
    bundle_payload = _post_graphql(token, bundle_query, bundle_vars)
    _require_no_errors("pr_bundle.graphql", bundle_payload)

    timeline_query = _load_query(Path("qb_site/syncer/queries/timeline_page.graphql"))
    timeline_vars = {
        **base_vars,
        "first": 2,
        "after": None,
        "inlineCommentsPerReview": 5,
        "since": None,
    }
    print("Validating timeline_page.graphql...")
    timeline_payload = _post_graphql(token, timeline_query, timeline_vars)
    _require_no_errors("timeline_page.graphql", timeline_payload)

    start_cursor = (
        (timeline_payload.get("data") or {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("timelineItems", {})
        .get("pageInfo", {})
        .get("startCursor")
    )
    if not start_cursor:
        print("Skipping timeline_page_back.graphql (no startCursor available).")
        return 0

    timeline_back_query = _load_query(Path("qb_site/syncer/queries/timeline_page_back.graphql"))
    timeline_back_vars = {
        **base_vars,
        "last": 2,
        "before": start_cursor,
        "inlineCommentsPerReview": 5,
    }
    print("Validating timeline_page_back.graphql...")
    timeline_back_payload = _post_graphql(token, timeline_back_query, timeline_back_vars)
    _require_no_errors("timeline_page_back.graphql", timeline_back_payload)

    repo_labels_query = _load_query(Path("qb_site/syncer/queries/repo_labels.graphql"))
    repo_labels_vars = {"owner": owner, "name": name, "first": 2, "after": None}
    print("Validating repo_labels.graphql...")
    repo_labels_payload = _post_graphql(token, repo_labels_query, repo_labels_vars)
    _require_no_errors("repo_labels.graphql", repo_labels_payload)

    print("GitHub GraphQL validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
