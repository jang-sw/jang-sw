#!/usr/bin/env python3
"""Build a profile language atlas from GitHub's public repository languages API.

No dependencies. Tokens are optional, are sent only to api.github.com, and are
never logged. README marker blocks must already exist. All fetching and
validation complete before any generated file is replaced.
"""

from __future__ import annotations

import argparse
import html
import itertools
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


START = "<!-- LANGUAGE_ATLAS:START -->"
END = "<!-- LANGUAGE_ATLAS:END -->"
API = "https://api.github.com"
COLORS = ("#6fe8ff", "#b59bff", "#80e4c4", "#ffaed5", "#92baff", "#e4d183")


class AtlasError(Exception):
    """A safe-to-display generation failure."""


class SameHostRedirect(HTTPRedirectHandler):
    """Do not forward a GitHub token to another origin or an insecure URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            target = urlsplit(newurl)
            safe = (target.scheme == "https" and target.hostname == "api.github.com"
                    and target.port in (None, 443) and target.username is None
                    and target.password is None)
        except ValueError:
            safe = False
        if not safe:
            raise HTTPError(req.full_url, code, "Unsafe API redirect blocked", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_owner(owner: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", owner):
        raise AtlasError("Invalid GitHub owner name.")
    return owner


def request_json(path: str, token: str | None = None):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-profile-language-atlas",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with build_opener(SameHostRedirect()).open(Request(API + path, headers=headers), timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        raise AtlasError(f"GitHub API returned HTTP {exc.code}; no files were changed.") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise AtlasError("GitHub API could not be read; no files were changed.") from None


def scoped_repositories(owner: str, repositories: list[dict]) -> list[dict]:
    if not isinstance(repositories, list):
        raise AtlasError("Repository response must be an array.")
    result = {}
    for repo in repositories:
        if not isinstance(repo, dict):
            raise AtlasError("Invalid repository record.")
        name = repo.get("name")
        if not isinstance(name, str) or not name or len(name) > 100:
            raise AtlasError("Invalid repository name.")
        login = repo.get("owner", {}).get("login", "")
        if (login.casefold() != owner.casefold() or repo.get("fork") is not False
                or repo.get("private") is not False or name.casefold() == owner.casefold()):
            continue
        if repo.get("visibility", "public") != "public":
            continue
        result[name] = repo
    return sorted(result.values(), key=lambda item: (item["name"].casefold(), item["name"]))


def fetch_snapshot(owner: str, token: str | None = None) -> dict:
    repositories = []
    page = 1
    while True:
        batch = request_json(f"/users/{quote(owner)}/repos?type=owner&per_page=100&page={page}", token)
        if not isinstance(batch, list):
            raise AtlasError("Unexpected repository response; no files were changed.")
        repositories.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    languages_by_repo = {}
    for repo in scoped_repositories(owner, repositories):
        languages_by_repo[repo["name"]] = request_json(
            f"/repos/{quote(owner)}/{quote(repo['name'], safe='')}/languages", token
        )
    return {"repositories": repositories, "languages_by_repo": languages_by_repo}


def build_model(owner: str, snapshot: dict) -> dict:
    """Normalize each repository first; large repositories get no extra weight."""
    validate_owner(owner)
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("languages_by_repo"), dict):
        raise AtlasError("Snapshot requires repositories and languages_by_repo.")
    scoped = scoped_repositories(owner, snapshot.get("repositories"))
    repos = []
    for source in scoped:
        name = source["name"]
        raw = snapshot["languages_by_repo"].get(name)
        if not isinstance(raw, dict):
            raise AtlasError(f"Missing or invalid language data for {name}.")
        for lang, count in raw.items():
            if not isinstance(lang, str) or not lang or len(lang) > 100:
                raise AtlasError("Invalid language name.")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise AtlasError("Language bytes must be nonnegative integers.")
        languages = {lang: raw[lang] for lang in sorted(raw) if raw[lang] > 0}
        if not languages:
            continue
        repos.append({
            "name": name,
            "url": f"https://github.com/{quote(owner)}/{quote(name, safe='')}",
            "archived": source.get("archived", False) is True,
            "languages": languages,
        })
    language_repos = {}
    weighted = {}
    edges = {}
    for repo in repos:
        total = sum(repo["languages"].values())
        for lang, count in repo["languages"].items():
            language_repos.setdefault(lang, []).append(repo)
            weighted[lang] = weighted.get(lang, 0.0) + count / total
        for pair in itertools.combinations(sorted(repo["languages"]), 2):
            edges[pair] = edges.get(pair, 0) + 1
    summaries = []
    for lang, matching in language_repos.items():
        # An example is evidence of presence, not a recommendation or skill score.
        example = min(matching, key=lambda r: (
            -r["languages"][lang] / sum(r["languages"].values()),
            -r["languages"][lang], r["name"].casefold(), r["name"],
        ))
        summaries.append({
            "name": lang,
            "repository_count": len(matching),
            "repository_names": [repo["name"] for repo in matching],
            "normalized_share_percent": round(weighted[lang] / len(repos) * 100, 6),
            "example": {"name": example["name"], "url": example["url"], "archived": example["archived"]},
        })
    summaries.sort(key=lambda lang: (-lang["repository_count"], -lang["normalized_share_percent"], lang["name"]))
    connections = [
        {"languages": list(pair), "repository_count": count}
        for pair, count in sorted(edges.items(), key=lambda entry: (-entry[1], entry[0]))
    ]
    return {
        "schema_version": 1,
        "owner": owner,
        "scope": "Owned public non-fork repositories, excluding the profile repository and empty language maps. Archived repositories are included and labeled.",
        "source": "GitHub REST API /repos/{owner}/{repo}/languages (GitHub Linguist byte counts)",
        "interpretation": "Repository presence and language co-occurrence, not proficiency. Normalized shares are the average per-repository byte shares, not total-byte shares.",
        "scoped_repository_count": len(scoped),
        "repository_count": len(repos),
        "archived_repository_count": sum(repo["archived"] for repo in repos),
        "language_count": len(summaries),
        "repositories": repos,
        "languages": summaries,
        "connections": connections,
    }


def xml(value) -> str:
    return html.escape(str(value), quote=True)


def short(value: str, maximum: int = 23) -> str:
    return value if len(value) <= maximum else value[:maximum - 1] + "…"


def render_svg(model: dict) -> str:
    languages = model["languages"][:6]
    selected = {lang["name"] for lang in languages}
    connections = [edge for edge in model["connections"] if set(edge["languages"]) <= selected]
    positions = {}
    # Fixed equal-size nodes: location and size encode no ranking or proficiency.
    for index, lang in enumerate(languages):
        angle = -math.pi / 2 + index * 2 * math.pi / max(len(languages), 1)
        positions[lang["name"]] = (380 + 245 * math.cos(angle), 268 + 118 * math.sin(angle))
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="760" height="480" viewBox="0 0 760 480" role="img" aria-labelledby="title desc">',
        '<title id="title">Language Atlas</title>',
        '<desc id="desc">Equal-size language nodes show repository counts. Lines connect languages found together in repositories; thicker lines mean more shared repositories. Not a proficiency score.</desc>',
        '<defs><linearGradient id="bg" x2="1" y2="1"><stop stop-color="#0e1933"/><stop offset="1" stop-color="#15102b"/></linearGradient><linearGradient id="line" gradientUnits="userSpaceOnUse" x1="0" y1="100" x2="760" y2="480"><stop stop-color="#6fe8ff"/><stop offset="1" stop-color="#b59bff"/></linearGradient></defs>',
        '<rect x="1" y="1" width="758" height="478" rx="22" fill="url(#bg)" stroke="#334362"/>',
        '<g font-family="system-ui, sans-serif">',
        '<text x="32" y="44" fill="#f1f6ff" font-size="29" font-weight="750" letter-spacing="3">LANGUAGE ATLAS</text>',
        f'<text x="33" y="77" fill="#a8b9d5" font-size="20">{model["repository_count"]} repos · {model["language_count"]} detected languages · top {len(languages)} shown</text>',
        '<path d="M33 97 H727" stroke="#2b3655"/>',
    ]
    if not languages:
        out.append('<text x="380" y="266" text-anchor="middle" fill="#a8b9d5" font-size="24">No public language data yet</text>')
    for edge in connections:
        x1, y1 = positions[edge["languages"][0]]
        x2, y2 = positions[edge["languages"][1]]
        width = 3 + 4 * edge["repository_count"] / max(e["repository_count"] for e in connections)
        title = f'{edge["languages"][0]} + {edge["languages"][1]}: {edge["repository_count"]} shared repositories'
        out.append(f'<path d="M{x1:.1f} {y1:.1f} L{x2:.1f} {y2:.1f}" stroke="url(#line)" stroke-width="{width:.2f}" opacity="0.42"><title>{xml(title)}</title></path>')
    for index, lang in enumerate(languages):
        x, y = positions[lang["name"]]
        color = COLORS[index]
        out.extend([
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="42" fill="#111b33" stroke="{color}" stroke-width="3"/>',
            f'<text x="{x:.1f}" y="{y + 11:.1f}" fill="{color}" font-size="30" font-weight="750" text-anchor="middle">{lang["repository_count"]}</text>',
            f'<text x="{x:.1f}" y="{y + 66:.1f}" fill="#e9f0ff" stroke="#11162e" stroke-width="7" stroke-linejoin="round" paint-order="stroke" font-size="24" font-weight="600" text-anchor="middle">{xml(short(lang["name"], 18))}</text>',
        ])
    out.append('</g></svg>')
    return "\n".join(out) + "\n"


def markdown(value: str) -> str:
    # Avoid Markdown/HTML structure injection from API-controlled names.
    value = html.escape(value, quote=False).replace("\r", " ").replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]()#+.!|])", r"\\\1", value)


def render_table(model: dict, japanese: bool = False) -> str:
    if not model["languages"]:
        return "公開リポジトリの言語データはまだありません。" if japanese else "No public repository language data yet."
    heading = "| 言語 | リポジトリ数 | コードの例 |" if japanese else "| Language | Repositories | Example |"
    lines = [heading, "| :-- | --: | :-- |"]
    for lang in model["languages"][:6]:
        example = lang["example"]
        archived = " · archived" if example["archived"] else ""
        lines.append(f'| {markdown(lang["name"])} | {lang["repository_count"]} | [{markdown(example["name"])}]({example["url"]}){archived} |')
    summary = f'全 {model["language_count"]} 言語のリポジトリを見る' if japanese else f'Explore all {model["language_count"]} languages'
    lines.extend(["", "<details>", f"<summary>{summary}</summary>", ""])
    by_name = {repo["name"]: repo for repo in model["repositories"]}
    for lang in model["languages"]:
        links = []
        for name in lang["repository_names"]:
            repo = by_name[name]
            archived = (" · アーカイブ" if japanese else " · archived") if repo["archived"] else ""
            links.append(f'[{markdown(name)}]({repo["url"]}){archived}')
        count = f'{lang["repository_count"]} リポジトリ' if japanese else f'{lang["repository_count"]} repos'
        lines.append(f'- **{markdown(lang["name"])}** ({count}): ' + ", ".join(links))
    lines.extend(["", "</details>"])
    return "\n".join(lines)


def replace_block(original: str, body: str) -> str:
    if original.count(START) != 1 or original.count(END) != 1:
        raise AtlasError("Each README must contain exactly one LANGUAGE_ATLAS marker pair.")
    start = original.index(START) + len(START)
    end = original.index(END)
    if end < start:
        raise AtlasError("README language atlas markers are reversed.")
    return original[:start] + "\n\n" + body + "\n\n" + original[end:]


def generate_outputs(root: Path, model: dict) -> dict[Path, str]:
    outputs = {
        root / "data/languages.json": json.dumps(model, ensure_ascii=False, indent=2) + "\n",
        root / "assets/language-atlas.svg": render_svg(model),
    }
    for name, japanese in (("README.md", False), ("README.ja.md", True)):
        path = root / name
        try:
            original = path.read_text(encoding="utf-8")
        except OSError:
            raise AtlasError(f"Could not read {name}; no files were changed.") from None
        outputs[path] = replace_block(original, render_table(model, japanese))
    return outputs


def write_outputs(outputs: dict[Path, str], check: bool = False) -> bool:
    changes = [path for path, content in outputs.items() if not path.exists() or path.read_bytes() != content.encode("utf-8")]
    if check:
        return not changes
    for path in changes:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(outputs[path])
            os.replace(temporary, path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default="jang-sw")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--offline", type=Path, help="Use a JSON snapshot containing repositories and languages_by_repo.")
    parser.add_argument("--check", action="store_true", help="Check freshness without writing files; exit 1 if stale.")
    args = parser.parse_args(argv)
    try:
        validate_owner(args.owner)
        snapshot = json.loads(args.offline.read_text(encoding="utf-8")) if args.offline else fetch_snapshot(args.owner, os.environ.get("GITHUB_TOKEN"))
        model = build_model(args.owner, snapshot)
        outputs = generate_outputs(args.root, model)
        fresh = write_outputs(outputs, args.check)
    except (AtlasError, OSError, ValueError) as exc:
        print(f"Language atlas: {exc}", file=sys.stderr)
        return 2
    if not fresh:
        print("Language atlas generated files are out of date.")
        return 1
    print(f'Language atlas: {model["repository_count"]} repositories, {model["language_count"]} languages. {"Verified." if args.check else "Updated."}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
