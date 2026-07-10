from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_repository_notification_follows_successful_main_release():
    make_release = (WORKFLOWS / "make-release.yml").read_text(encoding="utf-8")
    notify_repository = (WORKFLOWS / "notify-repository.yml").read_text(
        encoding="utf-8"
    )

    assert "name: Make Release" in make_release
    assert "branches: [main]" in make_release
    assert "paths:" in make_release
    assert "      - 'addon.xml'" in make_release
    assert "      - 'addon.py'" in make_release
    assert "      - 'resources/**'" in make_release
    assert "permissions:\n  contents: write" in make_release

    assert "workflow_run:" in notify_repository
    assert 'workflows: ["Make Release"]' in notify_repository
    assert "types: [completed]" in notify_repository
    assert "\n  push:" not in notify_repository
    assert "permissions:\n  contents: read" in notify_repository
    assert (
        "if: github.event.workflow_run.conclusion == 'success' && "
        "github.event.workflow_run.head_branch == 'main'"
    ) in notify_repository
    assert notify_repository.count("${{ secrets.") == 1
    assert "${{ secrets.REPO_DISPATCH_TOKEN }}" in notify_repository
    assert '"ref": "${{ github.event.workflow_run.head_branch }}"' in notify_repository
    assert '"sha": "${{ github.event.workflow_run.head_sha }}"' in notify_repository
    assert "github.ref" not in notify_repository
    assert "github.sha" not in notify_repository

    assert not (WORKFLOWS / "build-repo.yml").exists()
