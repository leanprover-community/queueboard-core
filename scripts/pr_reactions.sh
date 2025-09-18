#!/usr/bin/env bash

# Surface errors in this script to CI, so they get noticed.
# See e.g. http://redsymbol.net/articles/unofficial-bash-strict-mode/ for explanation.
set -e -u -o pipefail

OWNER=leanprover-community
REPO=mathlib4

PR_NUMBER=$1

gh api graphql -f owner=$OWNER -f repo=$REPO -F prNumber=$PR_NUMBER -F query=@queries/pr_reactions.graphql
