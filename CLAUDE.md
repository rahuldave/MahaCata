# Instructions for agents working on MahaCata

## Document each feature in the commit that builds it

The agent that builds a feature also documents it. No other agent cleans up after you.

The site at https://rahuldave.com/MahaCata/ is built from the `docs/` folder by smalldocs. A workflow, `.github/workflows/pages.yml`, rebuilds and deploys it on every push to `main`. Its Reference section is generated at each build from the docstrings and types in `provcat` and `provrun`. The root README only links to the site, so user-facing material goes in `docs/`, not the README.

In each commit that changes behaviour, do these things:

- Update every page in `docs/` that describes the behaviour you changed. This includes notes that say a thing is not built yet, and plan steps you have now finished.
- Update `docs/reference.md` when you change a command, a URI form, a store, a configuration setting or the cache.
- Add a step with real output to the tour, `docs/tour.md` or `docs/provrun-tour.md`, for a new driver or feature.
- Add a page to `docs/`, and to the sidebar in `docs/_smalldocs.yml`, when the feature needs one.
- Write a docstring for each new public module, class and function, because it appears on the site. Update `docs/api/index.md`, which says what each package covers, when you add a module.
- Check each summary and each example on the pages you change against the code. Check the whole page, including lines you did not change.
- Check the site:

  ```bash
  uv run --group docs smalldocs render docs --strict
  uv run --group docs smalldocs check docs
  ```

  Both must exit 0. The check prints `0 thing(s) to look at` when it passes. The workflow runs only the render, so a failed render stops every agent's pages from deploying, and only you run the check.
- After you push, find the deploy run for your own commit, and wait for it to finish:

  ```bash
  gh run list --workflow pages.yml --commit "$(git rev-parse HEAD)"
  gh run watch RUN_ID --exit-status
  ```

  Here RUN_ID is the number in the run's line. Another agent's later push can cancel your run, and its run then deploys your pages too. If the run fails, read its log with `gh run view RUN_ID --log-failed`, fix the cause and push again.

## Keep private names out

The repository is public. Do not put the names of private projects, account IDs, keys or local paths in docs, code or commit messages.
