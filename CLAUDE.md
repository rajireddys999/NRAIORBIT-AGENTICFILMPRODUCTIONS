# NRAIORBIT: notes for coding agents

An AI Telugu film pipeline. The runner (`nraiorbit service tick`, a Cloud Run job) makes films from the queue sheet;
the control room (`nraiorbit ui`, a Cloud Run service, `src/nraiorbit/ui`) shows them and takes the owner's
decisions. Both run from this repository, deployed as two images.

## Set up and check

```bash
pip install -e ".[dev,google,ui]" ruff      # ffmpeg is needed too: the media tests skip without it
ruff check src tests
pytest -q -n auto                            # every core; about half a minute without ffmpeg
```

CI (`.github/workflows/ci.yml`) runs the same on every pull request and on the default branch, with ffmpeg. Merge
only when it is green: nothing in GitHub enforces that yet.

## Rules

- Agent and LLM tests use `nraiorbit.llm.ReplayLLM` fixtures. Never call a real API in a test.
- The runner and the control room are deployed separately and briefly run different versions. The queue sheets
  (`queue/stories.csv`, `queue/references.csv`) only ever gain columns, and the request files the control room
  writes for the runner (`stories/<slug>.rework.json`, `stories/<slug>.voices.yaml`) must stay readable by both the
  old and the new code.
- Money and releases are the owner's: never auto-approve the `cost` or `publish` gates
  (`service/produce.OWNER_GATES`), and keep paid media (`inbox/`, `cache/`, `06_keyframes/`) out of anything that
  deletes.
- Do not edit `deploy/*/setup.sh` or `.github/` unless the task asks for it. The setup scripts replace a service's
  whole environment (`--set-env-vars`, `--set-secrets`); deploys change the image only.
- No AI attribution anywhere: no `Co-Authored-By` lines, no "Generated with" text and no emoji in commit messages or
  pull request bodies. `.claude/settings.json` turns Claude Code's own attribution off.
- Write for the owner, who does not read code: plain English in commit messages, PR descriptions and anything the
  control room shows.
