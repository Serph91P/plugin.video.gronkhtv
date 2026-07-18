from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def workflow_step(workflow, name):
    marker = f"      - name: {name}\n"
    start = workflow.index(marker)
    end = workflow.find("\n      - name:", start + len(marker))
    return workflow[start:] if end == -1 else workflow[start:end]


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
    assert "Set up Python" in make_release
    assert "python-version: '3.11'" in make_release
    assert "scripts/build_release.py" in make_release
    assert "rsync" not in make_release
    assert "zip -r" not in make_release

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


def test_addon_validations_has_minimal_workflow_permissions():
    addon_validations = (WORKFLOWS / "addon-validations.yml").read_text(
        encoding="utf-8"
    )

    assert "permissions:\n  contents: read\n\njobs:" in addon_validations


def test_addon_validations_runs_full_pytest_suite():
    addon_validations = (WORKFLOWS / "addon-validations.yml").read_text(
        encoding="utf-8"
    )
    install = workflow_step(addon_validations, "Install dependencies")
    pytest = workflow_step(addon_validations, "Pytest regression suite")

    assert "python -m pip install pytest" in install
    assert "working-directory: ${{ github.event.repository.name }}" in pytest
    assert "python -m pytest -q" in pytest


def test_release_retries_when_exact_version_asset_is_missing():
    make_release = (WORKFLOWS / "make-release.yml").read_text(encoding="utf-8")
    version = workflow_step(make_release, "Get version and check tag")
    changelog = workflow_step(make_release, "Get Changelog")
    create_zip = workflow_step(make_release, "Create Zip")
    create_release = workflow_step(make_release, "Create Release")
    retry_condition = (
        "if: steps.version.outputs.tag_exists == 'false' || "
        "steps.version.outputs.asset_exists == 'false'"
    )

    assert "filename=${{ github.event.repository.name }}-${version}.zip" in version
    assert 'gh api "repos/${{ github.repository }}/releases/tags/v$version"' in version
    assert 'grep -Fxq -- "$filename"' in version
    assert 'echo "asset_exists=true" >> $GITHUB_OUTPUT' in version
    assert 'echo "asset_exists=false" >> $GITHUB_OUTPUT' in version
    assert retry_condition in changelog
    assert retry_condition in create_zip
    assert "scripts/build_release.py" in create_zip
    assert "filename=${{ steps.version.outputs.filename }}" in create_zip
    assert retry_condition in create_release
    assert "files: ${{ steps.version.outputs.filename }}" in create_release
    assert "fail_on_unmatched_files: true" in create_release


def test_release_finishes_with_exact_asset_verification():
    make_release = (WORKFLOWS / "make-release.yml").read_text(encoding="utf-8")
    create_release = workflow_step(make_release, "Create Release")
    verify = workflow_step(make_release, "Verify release asset")

    assert make_release.index(create_release) < make_release.index(verify)
    assert "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in verify
    assert (
        'gh api "repos/${{ github.repository }}/releases/tags/'
        'v${{ steps.version.outputs.version }}"' in verify
    )
    assert "--jq '.assets[].name'" in verify
    assert 'grep -Fxq -- "${{ steps.version.outputs.filename }}"' in verify
    assert make_release.rstrip().endswith(verify.rstrip())
