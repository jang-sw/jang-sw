# Language Atlas

A repository-connected profile, without contribution streaks or image-widget services.

## What the map means

- Source: GitHub REST API public repository list and each repository's `/languages` endpoint.
- Scope: repositories owned by `jang-sw`, excluding forks, private repositories and `jang-sw/jang-sw` itself. Repositories without detected language bytes do not contribute to the map.
- A language's count is the number of included repositories where GitHub detects that language. It is not a skill rating, a commit count, or a measure of personally authored code.
- An edge connects two languages found together in one or more repositories. Its width reflects the shared repository count. The diagram shows the leading languages; the source JSON preserves the full inventory.
- Animated lights follow only actual co-occurrence edges. Floating cards, slowly pulsing neon borders, twinkling stars, a breathing background glow and orbiting particles are decorative, not live traffic or current development activity. Equal-size language cards do not encode proficiency.
- At the profile owner's explicit request for visible animation, `prefers-reduced-motion: reduce` now selects a slower variant rather than a completely static image. This is not a no-motion fallback. It changes only this SVG, never browser or operating-system preferences. The previous blanket animation stop made the profile appear entirely static in browsers reporting reduced motion.
- Expand the all-languages section below the diagram to browse every matching repository. The redundant examples table and explanatory README section are intentionally omitted. The SVG displayed inside a README is not an interactive application.

## Refresh

`Refresh Language Atlas` runs weekly, after generator/workflow changes, or through **Actions → Refresh Language Atlas → Run workflow**. It uses GitHub's temporary built-in token; no personal token or external account is required.

The generator completes all API reads before updating any files. API failures leave the last published snapshot intact. If the underlying data is unchanged, no commit is created. The generator does not add timestamps just to manufacture activity.

GitHub may automatically disable scheduled workflows after 60 days without repository activity. The last committed map remains visible. If automatic updates have paused, re-enable the workflow in Actions and run it manually. No keep-alive commits are generated.

## Local development

Requires Python 3.11 or newer; standard library only.

```sh
python3 -m unittest discover -s tests -v
python3 scripts/language_atlas.py --owner jang-sw --root .
```

`GITHUB_TOKEN` is optional for public data but recommended when API rate limits matter. Never commit tokens. Use `--offline <fixture.json>` for a saved input fixture and `--check` to compare generated outputs without writing them.

Generated files: `assets/language-atlas.svg`, `data/languages.json`, and the marked sections in both profile READMEs. Original project code is never modified.

Sources: [Repository languages](https://docs.github.com/en/rest/repos/repos#list-repository-languages), [scheduled workflow behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
