"""Stage a verified local Git bundle into a new detached checkout. Run via RunSubmit."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path


def stage(bundle: str, repository: str, checkout: str, commit: str, ref: str):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", GIT_ASKPASS="", SSH_ASKPASS="")
    repo, dest = Path(repository).resolve(), Path(checkout).resolve()
    if dest.exists():
        raise ValueError("checkout must be a new path; existing work is never reset")

    def git(*args):
        return subprocess.check_output(
            ["git", *args], env=env, stderr=subprocess.STDOUT, encoding="utf-8", timeout=90
        ).strip()

    if not repo.exists():
        git("init", "--bare", str(repo))
    git("-C", str(repo), "bundle", "verify", str(Path(bundle).resolve()))
    git("-C", str(repo), "fetch", "--no-tags", str(Path(bundle).resolve()), ref)
    found = git("-C", str(repo), "rev-parse", "FETCH_HEAD^{commit}")
    if found != commit:
        raise ValueError("bundle commit differs from requested commit")
    git("-C", str(repo), "worktree", "add", "--detach", str(dest), commit)
    if git("-C", str(dest), "status", "--porcelain"):
        raise ValueError("staged worktree is unexpectedly dirty")
    return {"commit": found, "checkout": str(dest), "repository": str(repo)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("bundle", "repository", "checkout", "commit", "ref"):
        p.add_argument("--" + name, required=True)
    print(json.dumps(stage(**vars(p.parse_args()))))


if __name__ == "__main__":
    main()
