#!/usr/bin/env python3
"""Post the PR-noia review to the pull request as inline review comments.

Runs inside the GitHub Action *after* prnoia_client.py has polled PR-noia and
written the completed review to ./pr_assets/review.json. Posting happens here, in
the user's own CI run, using the run's built-in GITHUB_TOKEN (scoped to this
repository) — the PR-noia backend never holds a GitHub credential. This is what
makes the product multi-tenant.

For every finding whose file:line lies inside this PR's diff, we post a GitHub
**pull-request review comment** anchored to that exact line (visible on the Files
changed tab), with the explanation, remediation, and — when the reviewer supplied
one — a committable ```suggestion block (one-click fix). Findings that don't land
on a diff line (or whose inline post is rejected) are collected into a single
persistent summary comment, which also carries the overall counts.

Re-runs don't stack: prior PR-noia inline comments are deleted first (keyed by a
hidden marker) and the summary comment is edited in place.

Usage:
  post_comment.py [--review ./pr_assets/review.json] [--diff ./pr_assets/pr_diff.patch]
                  [--repo owner/repo] [--pr N] [--dry-run]

Environment:
  GH_TOKEN / GITHUB_TOKEN   Token `gh` authenticates with (provided by Actions).
  GITHUB_REPOSITORY         owner/repo (fallback for --repo).
  PR_NUMBER                 pull request number (fallback for --pr).
"""
import argparse
import json
import os
import re
import subprocess
import sys

SUMMARY_MARKER = "<!-- pr-noia:security-review -->"
FINDING_MARKER = "<!-- pr-noia:finding -->"

SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]
SEVERITY_BADGE = {
    "critical": "🔴 Critical",
    "high": "🟠 High",
    "medium": "🟡 Medium",
    "low": "⚪ Low",
    "info": "ℹ️ Info",
}

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_commentable_lines(diff_text):
    """Map each file path → set of RIGHT-side line numbers that live in the diff.

    Only lines present in a hunk (added or context) can carry a review comment on
    the RIGHT side; anything else is a 422 from GitHub, so we filter up front.
    """
    commentable = {}
    path = None
    new_line = 0
    for raw in (diff_text or "").splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else re.sub(r"^b/", "", target)
            continue
        if raw.startswith("@@"):
            m = _HUNK_RE.match(raw)
            new_line = int(m.group(1)) if m else 0
            continue
        if path is None or not raw:
            continue
        tag = raw[0]
        if tag == "+":
            commentable.setdefault(path, set()).add(new_line)
            new_line += 1
        elif tag == " ":
            commentable.setdefault(path, set()).add(new_line)
            new_line += 1
        elif tag == "-" or tag == "\\":
            continue  # removed line / "No newline" marker: no RIGHT-side number
    return commentable


def _header(f):
    sev = (f.get("severity") or "info").lower()
    badge = SEVERITY_BADGE.get(sev, SEVERITY_BADGE["info"])
    tag = f.get("cwe") or f.get("owasp")
    tag = f" — {tag}" if tag else ""
    meta = f"_source: {f.get('source') or '?'}, confidence: {f.get('confidence') or 'medium'}_"
    return f"**[Security Review] {badge} {f.get('title', 'Untitled finding')}**{tag}\n\n{meta}"


def inline_body(f, with_suggestion):
    lines = [FINDING_MARKER, _header(f)]
    if f.get("description"):
        lines.append("\n" + f["description"])
    if f.get("recommendation"):
        lines.append(f"\n**Remediation:** {f['recommendation']}")
    if with_suggestion and f.get("suggestion"):
        lines.append("\n```suggestion\n" + str(f["suggestion"]).rstrip("\n") + "\n```")
    return "\n".join(lines)


def summary_body(review, off_diff, posted_count):
    findings = review.get("findings") or []
    stats = review.get("stats") or {}
    lines = [SUMMARY_MARKER, "## 🔎 PR-noia security review", ""]
    if not findings:
        lines.append((review.get("summary") or "").strip()
                     or "No security findings for the lines this PR touches. ✅")
        return "\n".join(lines)

    counts = ", ".join(f"{stats.get(s, 0)} {s}" for s in SEVERITY_ORDER if stats.get(s))
    lines.append(f"**{stats.get('total', len(findings))} finding(s)** ({counts})")
    if posted_count:
        lines.append(f"\n{posted_count} inline comment(s) posted on the changed lines above. 👆")
    if off_diff:
        lines.append("\n### Findings outside this PR's diff")
        lines.append("")
        for f in off_diff:
            sev = SEVERITY_BADGE.get((f.get("severity") or "info").lower(), "")
            loc = f.get("file") or "?"
            if f.get("line"):
                loc += f":{f['line']}"
            tag = f.get("cwe") or f.get("owasp")
            tag = f" ({tag})" if tag else ""
            lines.append(f"- {sev} **{f.get('title', 'Untitled finding')}**{tag} — `{loc}`")
            if f.get("description"):
                lines.append(f"  {f['description']}")
            if f.get("recommendation"):
                lines.append(f"  _Remediation:_ {f['recommendation']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# gh helpers
# --------------------------------------------------------------------------- #

def gh(args, params=None, check=True):
    """Run `gh api ...`; JSON params (if any) are piped on stdin via --input -."""
    cmd = ["gh", *args]
    stdin = None
    if params is not None:
        cmd += ["--input", "-"]
        stdin = json.dumps(params)
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, check=check)


def delete_stale_inline(repo, pr):
    res = gh(["api", f"repos/{repo}/pulls/{pr}/comments", "--paginate"])
    for c in json.loads(res.stdout or "[]"):
        if FINDING_MARKER in (c.get("body") or ""):
            gh(["api", f"repos/{repo}/pulls/comments/{c['id']}", "-X", "DELETE"], check=False)


def post_inline(repo, pr, head_sha, f, commentable):
    """Post one anchored review comment. Returns True on success, False to defer."""
    path, line = f.get("file"), f.get("line")
    lines = commentable.get(path) or set()
    if not path or not line or line not in lines:
        return False
    params = {"body": "", "commit_id": head_sha, "path": path, "line": int(line), "side": "RIGHT"}

    end_line = f.get("end_line")
    full_range = (
        end_line and end_line > line
        and all(n in lines for n in range(int(line), int(end_line) + 1))
    )
    if full_range:
        params.update({"start_line": int(line), "start_side": "RIGHT", "line": int(end_line)})
    # A ```suggestion only replaces the exact anchored range, so only attach one
    # when the anchor covers the finding's whole line span.
    with_suggestion = bool(f.get("suggestion")) and (full_range or not end_line or end_line == line)
    params["body"] = inline_body(f, with_suggestion)

    res = gh(["api", f"repos/{repo}/pulls/{pr}/comments", "-X", "POST"], params=params, check=False)
    if res.returncode != 0:
        print(f"  inline post deferred for {path}:{line}: {res.stderr.strip()[:160]}", file=sys.stderr)
        return False
    return True


def upsert_summary(repo, pr, body):
    res = gh(["api", f"repos/{repo}/issues/{pr}/comments", "--paginate"])
    existing = next((c["id"] for c in json.loads(res.stdout or "[]")
                     if SUMMARY_MARKER in (c.get("body") or "")), None)
    if existing:
        gh(["api", f"repos/{repo}/issues/comments/{existing}", "-X", "PATCH"], params={"body": body})
        print(f"Updated summary comment {existing}", file=sys.stderr)
    else:
        gh(["api", f"repos/{repo}/issues/{pr}/comments", "-X", "POST"], params={"body": body})
        print("Posted summary comment", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--review", default="./pr_assets/review.json")
    ap.add_argument("--diff", default="./pr_assets/pr_diff.patch")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    ap.add_argument("--pr", default=os.environ.get("PR_NUMBER"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.review, "r", encoding="utf-8") as fh:
        review = json.load(fh)
    try:
        with open(args.diff, "r", encoding="utf-8", errors="replace") as fh:
            diff_text = fh.read()
    except FileNotFoundError:
        diff_text = ""

    repo = args.repo or review.get("repository")
    pr = str(args.pr or review.get("pr_number") or "")
    head_sha = review.get("head_sha")
    if not repo or not pr:
        print("Error: could not determine repo/pr for posting.", file=sys.stderr)
        return 1

    findings = review.get("findings") or []
    commentable = parse_commentable_lines(diff_text)

    if args.dry_run:
        print(summary_body(review, findings, 0))
        return 0

    posted, off_diff = 0, []
    try:
        delete_stale_inline(repo, pr)
        for f in findings:
            if head_sha and post_inline(repo, pr, head_sha, f, commentable):
                posted += 1
            else:
                off_diff.append(f)
        upsert_summary(repo, pr, summary_body(review, off_diff, posted))
    except subprocess.CalledProcessError as exc:
        print(f"Error: gh failed: {exc.stderr or exc.stdout}", file=sys.stderr)
        return 1
    print(f"Done: {posted} inline, {len(off_diff)} in summary.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
