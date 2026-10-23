"""The deploy workflow and its one-time setup: what goes live is exactly what CI tested, only images change, the job
moves before the control room, and only deploy.yml on the default branch may act as the deployer."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BRANCH = "claude/telugu-ai-movie-generator-yhb6po"


def _workflow(name):
    data = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    return data, data.get("on", data.get(True))                 # YAML 1.1 reads a bare `on:` key as True


def test_deploy_follows_a_passing_ci_run_on_the_default_branch_and_can_be_run_by_hand():
    data, on = _workflow("deploy.yml")
    assert on["workflow_run"] == {"workflows": ["ci"], "types": ["completed"], "branches": [BRANCH]}
    assert "workflow_dispatch" in on and "push" not in on and "pull_request" not in on
    plan = data["jobs"]["plan"]["if"]
    assert "workflow_run.conclusion == 'success'" in plan and "workflow_run.event == 'push'" in plan
    assert data["concurrency"] == {"group": "deploy-production", "cancel-in-progress": False}
    pick = next(s["run"] for s in data["jobs"]["plan"]["steps"] if s.get("id") == "pick")
    assert 'git merge-base --is-ancestor "$sha" "origin/$BRANCH"' in pick          # merged commits only
    assert '[ "$sha" != "$(git rev-parse "origin/$BRANCH")" ]' in pick             # never an older one over newer
    assert "check-runs?check_name=test" in pick and 'select(.conclusion == "success")' in pick   # by hand: CI passed
    assert 'git rev-list --first-parent "origin/$BRANCH" | grep -qx "$sha"' in pick                # ...and was a tip
    assert data["jobs"]["plan"]["permissions"] == {"contents": "read", "checks": "read"}
    assert data["permissions"] == {"contents": "read"}
    ci, ci_on = _workflow("ci.yml")
    assert ci["name"] == "ci"                                       # the name deploy.yml waits for


def test_only_images_change_and_the_job_moves_before_the_control_room():
    text = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert "--set-env-vars" not in text and "--set-secrets" not in text and "gcloud run deploy" not in text
    steps = _workflow("deploy.yml")[0]["jobs"]["deploy"]["steps"]
    runs = [s.get("run", "") for s in steps]
    job = next(i for i, r in enumerate(runs) if "gcloud run jobs update nraiorbit-runner" in r)
    service = next(i for i, r in enumerate(runs) if "gcloud run services update nraiorbit-ui" in r)
    assert job < service and '--image "$REPO/runner:$SHA"' in runs[job] and '--image "$REPO/ui:$SHA"' in runs[service]


def test_images_are_stamped_with_the_commit_they_were_built_from():
    for df in ("deploy/ui/Dockerfile", "deploy/service/Dockerfile"):
        text = (ROOT / df).read_text(encoding="utf-8")
        assert "ARG GIT_SHA=unknown" in text and "NRAIORBIT_VERSION=$GIT_SHA" in text
    build = _workflow("deploy.yml")[0]["jobs"]["build"]
    script = next(s["run"] for s in build["steps"] if s.get("name") == "Build, check and push")
    assert '--build-arg "GIT_SHA=$SHA"' in script and 'docker push "$IMAGE"' in script
    assert script.index("${{ matrix.smoke }}") < script.index("docker push")    # never pushes an image that won't start
    for cb in ("deploy/ui/cloudbuild.yaml", "deploy/service/cloudbuild.yaml"):
        data = yaml.safe_load((ROOT / cb).read_text(encoding="utf-8"))
        assert "GIT_SHA=${_GIT_SHA}" in data["steps"][0]["args"] and data["substitutions"]["_GIT_SHA"] == "unknown"


def test_only_deploy_yml_on_the_default_branch_may_act_as_the_deployer():
    text = (ROOT / "deploy" / "github" / "setup.sh").read_text(encoding="utf-8")
    assert ("assertion.workflow_ref=='${GITHUB_REPO}/.github/workflows/deploy.yml@refs/heads/${BRANCH}'" in text)
    assert "startsWith" not in text                       # a prefix would let "...-yhb6po2" act as the deployer
    assert f'BRANCH="${{BRANCH:-{BRANCH}}}"' in text and "set -euo pipefail" in text
    assert "keys create" not in text and "--set-secrets" not in text                  # no stored key, no settings


def test_the_footer_shows_what_is_live(monkeypatch):
    import pytest
    pytest.importorskip("fastapi")
    from nraiorbit.ui import runner as runner_mod
    assert runner_mod.short_image("us-central1-docker.pkg.dev/p/nraiorbit/runner:4fd4905fac2f9a5b") == "4fd4905"
    assert runner_mod.short_image("x/runner@sha256:1d4282b13a5657a57a46790ae4") == "sha256:1d4282b13a56"
    assert runner_mod.short_image("x/runner:latest") == "latest"
    from fastapi.testclient import TestClient

    from nraiorbit.ui.app import Settings, create_app
    from nraiorbit.ui.store import Store
    monkeypatch.setattr(runner_mod, "job_image", lambda p, r, j: "r/nraiorbit/runner:4fd4905fac2f9a5b")
    monkeypatch.setattr(runner_mod, "executions", lambda *a, **k: [])
    monkeypatch.setenv("NRAIORBIT_VERSION", "b31a6204a0e82c6d")
    import tempfile
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / "queue").mkdir()
        app = create_app(Store(root), Settings(auth=False, project="p", job="nraiorbit-runner"))
        page = TestClient(app).get("/").text
    assert "VERSION · CONTROL ROOM b31a620 · RUNNER 4fd4905" in page


def test_every_report_says_which_runner_made_it(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from nraiorbit.service import produce
    monkeypatch.setenv("NRAIORBIT_VERSION", "b31a6204a0e82c6d")
    reports = tmp_path / "reports"

    def run(until=None):
        raise produce.GatePending("story", tmp_path)
    p = SimpleNamespace(p=SimpleNamespace(root=tmp_path, reports=reports, reviews=tmp_path / "reviews"), run=run,
                        log=lambda *_: None, cfg={"policy": {"gates": "ask"}}, _voiceless=lambda: [])
    report = produce.produce(p, log=lambda *_: None)
    assert report["version"] == "b31a6204a0e82c6d"
