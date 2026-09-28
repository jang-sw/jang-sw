#!/usr/bin/env python3
"""Build a profile language atlas from GitHub's public repository languages API.

No dependencies. Tokens are optional, are sent only to api.github.com, and are
never logged. README marker blocks must already exist. All fetching and
validation complete before any generated file is replaced.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import itertools
import json
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
    # Equal-size cards; position and motion are decoration, never a skill score.
    layout = [(180, 205), (623, 193), (676, 357), (539, 483), (172, 461), (126, 332)]
    positions = {lang["name"]: layout[index] for index, lang in enumerate(languages)}
    palette = {"Java": "#ffae78", "JavaScript": "#f8de7e", "HTML": "#ff8fad", "CSS": "#a69aff", "TypeScript": "#73d4ff", "Vue": "#73efc5"}
    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="820" height="580" viewBox="0 0 820 580" role="img" aria-labelledby="title desc">',
        '<title id="title">Language Atlas — a constellation of public code</title>',
        '<desc id="desc">Language cards show actual repository counts. Curved connections join languages used in the same repositories; wider lines mean more shared repositories. Moving lights are decorative, not live traffic or recent activity.</desc>',
        '<defs>',
        '<linearGradient id="bg" x2="1" y2="1"><stop stop-color="#07142b"/><stop offset=".48" stop-color="#11132f"/><stop offset="1" stop-color="#251336"/></linearGradient>',
        '<radialGradient id="aura"><stop stop-color="#7c5cff" stop-opacity=".30"/><stop offset="1" stop-color="#7c5cff" stop-opacity="0"/></radialGradient>',
        '<linearGradient id="line" gradientUnits="userSpaceOnUse" x1="60" y1="160" x2="730" y2="500"><stop stop-color="#61e4ff"/><stop offset=".52" stop-color="#ab8bff"/><stop offset="1" stop-color="#ff8ba7"/></linearGradient>',
        '<linearGradient id="rim" x2="1" y2="1"><stop stop-color="#61e4ff" stop-opacity=".65"/><stop offset=".48" stop-color="#8679df" stop-opacity=".13"/><stop offset="1" stop-color="#ff8ba7" stop-opacity=".6"/></linearGradient>',
        '<filter id="glow" x="-200%" y="-200%" width="500%" height="500%"><feGaussianBlur stdDeviation="2.4"/></filter>',
        '<pattern id="grid" width="28" height="28" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".8" fill="#8dbde6" opacity=".12"/></pattern>',
        '<clipPath id="frame"><rect x="1" y="1" width="818" height="578" rx="28"/></clipPath>',
        '</defs>',
        '''<style>
.flow{animation:flow 8s linear infinite}
@keyframes flow{from{stroke-dashoffset:0}to{stroke-dashoffset:-1000}}
.orbit,.satellite{transform-origin:410px 335px;animation:orbit 22s linear infinite}
.satellite{animation-duration:12s}
@keyframes orbit{to{transform:rotate(360deg)}}
.card{animation:float 6s ease-in-out infinite}
@keyframes float{0%,100%{transform:translateY(-6px)}50%{transform:translateY(6px)}}
.card-rim{animation:neon 4s ease-in-out infinite}
@keyframes neon{0%,100%{stroke-opacity:.3}50%{stroke-opacity:1}}
.halo{animation:breathe 7s ease-in-out infinite}
@keyframes breathe{0%,100%{opacity:.45}50%{opacity:1}}
.sparkle{animation:twinkle 5s ease-in-out infinite}
@keyframes twinkle{0%,100%{opacity:.15}50%{opacity:.95}}
@media(prefers-reduced-motion:reduce){*{animation:none!important}.flow{display:none}}
</style>''',
        '<g clip-path="url(#frame)">',
        '<rect width="820" height="580" fill="url(#bg)"/>',
        '<rect width="820" height="580" fill="url(#grid)"/>',
        '<ellipse class="halo" cx="440" cy="322" rx="290" ry="240" fill="url(#aura)"/>',
        '<g fill="#b7cfff" aria-hidden="true">',
        *[f'<circle class="sparkle" cx="{sx}" cy="{sy}" r="{1.5 + (index % 3) * .6}" style="animation-delay:-{index * .61:.2f}s"/>'
          for index, (sx, sy) in enumerate(((63,150),(335,167),(466,145),(758,153),(730,257),(770,466),(640,532),(405,491),(287,534),(60,443),(306,277),(489,402)))],
        '</g>',
        '<path d="M-30 530 Q300 590 850 180M-40 550 Q350 590 860 230" fill="none" stroke="#b57ee7" stroke-opacity=".1"/>',
        '<g font-family="Segoe UI, Arial, sans-serif">',
        f'<text x="36" y="36" fill="#7cdaed" font-size="13" font-weight="600" letter-spacing="3">{xml(model["owner"].upper())} / CODE CONSTELLATION</text>',
        '<text x="34" y="86" fill="#f3f4ff" font-size="43" font-weight="750" letter-spacing="-1.5">Language <tspan fill="#bc9dff">Atlas.</tspan></text>',
        '<rect x="603" y="35" width="181" height="67" rx="16" fill="#101b34" stroke="#485178"/>',
        f'<text x="626" y="65" fill="#f0f7ff" font-size="25" font-weight="700">{model["repository_count"]}<tspan font-size="15" fill="#a9bbd9"> repos</tspan><tspan fill="#586783"> / </tspan>{model["language_count"]}</text>',
        '<text x="626" y="85" fill="#9bb2d3" font-size="12" letter-spacing="1">DETECTED LANGUAGES</text>',
        '<path d="M36 116H784" stroke="url(#rim)"/>',
        '<g fill="none" stroke="#af91fa" stroke-opacity=".12"><circle cx="410" cy="335" r="96"/><circle cx="410" cy="335" r="110" stroke-dasharray="3 13" class="orbit"/><ellipse cx="410" cy="335" rx="167" ry="70" transform="rotate(-28 410 335)"/></g>',
    ]
    if not languages:
        out.append('<text x="410" y="336" text-anchor="middle" fill="#b7c7de" font-size="24">Your next idea starts here.</text>')
    maximum = max((edge["repository_count"] for edge in connections), default=1)
    for index, edge in enumerate(connections):
        first, second = edge["languages"]
        x1, y1 = positions[first]
        x2, y2 = positions[second]
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        cx, cy = mx + (410 - mx) * .38, my + (335 - my) * .38
        path = f'M{x1} {y1} Q{cx:.1f} {cy:.1f} {x2} {y2}'
        width = 1.4 + 3.6 * edge["repository_count"] / maximum
        title = f'{first} + {second}: {edge["repository_count"]} shared repositories'
        out.extend([
            f'<g data-language-a="{xml(first)}" data-language-b="{xml(second)}" data-repositories="{edge["repository_count"]}"><title>{xml(title)}</title>',
            f'<path d="{path}" fill="none" stroke="url(#line)" stroke-width="{width:.2f}" opacity=".38"/>',
            f'<path class="flow" d="{path}" pathLength="1000" fill="none" stroke="#d8f4ff" stroke-width="3.5" stroke-linecap="round" stroke-dasharray="24 976" stroke-dashoffset="{-67 * (index + 1)}" style="animation-delay:-{index * .93:.2f}s" opacity=".95"/>',
            '</g>',
        ])
    if languages:
        out.extend([
            '<circle cx="410" cy="335" r="76" fill="none" stroke="#9081ed" stroke-opacity=".3"/>',
            '<g class="satellite" aria-hidden="true"><circle cx="486" cy="335" r="9" fill="#98ecff" opacity=".6" filter="url(#glow)"/><circle cx="486" cy="335" r="4.5" fill="#d0f8ff"/><circle cx="372" cy="269.2" r="3" fill="#caa4ff"/><circle cx="372" cy="400.8" r="3" fill="#ffadce"/></g>',
            '<circle cx="410" cy="335" r="54" fill="#121a35" stroke="#4d4b7b"/>',
            '<circle cx="410" cy="335" r="49" fill="none" stroke="#8a8bd6" stroke-opacity=".17"/>',
            '<text x="410" y="344" text-anchor="middle" fill="#c5c8ff" font-family="Consolas, monospace" font-size="30" font-weight="700">&lt;/&gt;</text>',
        ])
    for index, lang in enumerate(languages):
        x, y = positions[lang["name"]]
        color = palette.get(lang["name"], COLORS[index])
        out.extend([
            f'<g class="card" style="animation-delay:-{index * 1.1:.2f}s;animation-duration:{6 + (index % 3) * .7:.1f}s">',
            f'<rect x="{x - 81}" y="{y - 39}" width="162" height="86" rx="17" fill="#030815" opacity=".5"/>',
            f'<rect class="card-rim" x="{x - 81}" y="{y - 43}" width="162" height="86" rx="17" fill="#111a32" stroke="{color}" stroke-opacity=".58" stroke-width="1.8" style="animation-delay:-{index * .8:.2f}s"/>',
            f'<path d="M{x - 61} {y - 43}H{x + 42}" stroke="{color}" stroke-width="2.8" stroke-linecap="round"/>',
            f'<circle cx="{x + 61}" cy="{y - 23}" r="3" fill="{color}" filter="url(#glow)"/>',
            f'<text x="{x - 63}" y="{y - 9}" fill="{color}" font-size="22" font-weight="700">{xml(short(lang["name"], 11))}</text>',
            f'<text x="{x - 63}" y="{y + 27}" fill="#f4f6ff" font-size="31" font-weight="700">{lang["repository_count"]}<tspan fill="#9aaeca" font-size="14" font-weight="400"> repos</tspan></text>',
            '</g>',
        ])
    out.extend([
        f'<text x="36" y="555" fill="#99aacc" font-size="14">TOP {len(languages)} LANGUAGES <tspan fill="#4d607f"> / </tspan> SHARED REPOSITORIES CONNECT THE CARDS</text>',
        '</g></g><rect x="1" y="1" width="818" height="578" rx="28" fill="none" stroke="url(#rim)" stroke-width="1.5"/></svg>',
    ])
    return "\n".join(out) + "\n"


def markdown(value: str) -> str:
    # Avoid Markdown/HTML structure injection from API-controlled names.
    value = html.escape(value, quote=False).replace("\r", " ").replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]()#+.!|])", r"\\\1", value)


def render_table(model: dict, japanese: bool = False) -> str:
    if not model["languages"]:
        return "公開リポジトリの言語データはまだありません。" if japanese else "No public repository language data yet."
    summary = f'全 {model["language_count"]} 言語のリポジトリを見る' if japanese else f'Explore all {model["language_count"]} languages'
    lines = ["<details>", f"<summary>{summary}</summary>", ""]
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
    svg = render_svg(model)
    image_version = hashlib.sha256(svg.encode("utf-8")).hexdigest()[:12]
    outputs = {
        root / "data/languages.json": json.dumps(model, ensure_ascii=False, indent=2) + "\n",
        root / "assets/language-atlas.svg": svg,
    }
    for name, japanese in (("README.md", False), ("README.ja.md", True)):
        path = root / name
        try:
            original = path.read_text(encoding="utf-8")
        except OSError:
            raise AtlasError(f"Could not read {name}; no files were changed.") from None
        # Give GitHub's image cache a new URL only when the diagram actually changes.
        original = re.sub(r"\./assets/language-atlas\.svg(?:\?v=[0-9a-f]+)?",
                          f"./assets/language-atlas.svg?v={image_version}", original)
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
