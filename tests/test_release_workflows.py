import ast
import re
import urllib.request
import zipfile
from functools import cache
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
REUSABLE_REPOSITORY = "Serph91P/repository.serph91p"
PACKAGE_PIN = "7adff881ab5d0a7fc63f7474a78b2688e2e6eee4"
NOTIFIER_PIN = "5afd718564c0d55a914978e43aafd34c92a53029"
PACKAGE_WORKFLOW = (
    f"{REUSABLE_REPOSITORY}/.github/workflows/"
    f"reusable-addon-package.yml@{PACKAGE_PIN}"
)
NOTIFIER_WORKFLOW = (
    f"{REUSABLE_REPOSITORY}/.github/workflows/"
    f"reusable-notify-repository.yml@{NOTIFIER_PIN}"
)


def load_workflow(path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


@cache
def pinned_text(path, pin):
    url = (
        f"https://raw.githubusercontent.com/{REUSABLE_REPOSITORY}/"
        f"{pin}/{path}"
    )
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode("utf-8")


def load_pinned_workflow(path, pin):
    return yaml.load(pinned_text(path, pin), Loader=yaml.BaseLoader)


def assigned_literal(source, name):
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"No assignment for {name}")


def workflow_step(workflow, name):
    marker = f"      - name: {name}\n"
    start = workflow.index(marker)
    end = workflow.find("\n      - name:", start + len(marker))
    return workflow[start:] if end == -1 else workflow[start:end]


def test_main_release_behavior_remains_available():
    make_release = (WORKFLOWS / "make-release.yml").read_text(encoding="utf-8")

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
    assert 'echo "asset_exists=true" >> "$GITHUB_OUTPUT"' in version
    assert 'echo "asset_exists=false" >> "$GITHUB_OUTPUT"' in version
    assert retry_condition in changelog
    assert retry_condition in create_zip
    assert "scripts/build_release.py" in create_zip
    assert "filename=${{ steps.version.outputs.filename }}" in create_zip
    assert retry_condition in create_release
    assert 'files: "${{ steps.create-zip.outputs.filename }}"' in create_release
    assert "fail_on_unmatched_files: true" in create_release


def test_release_validates_addon_before_version_or_filename_output():
    make_release = (WORKFLOWS / "make-release.yml").read_text(encoding="utf-8")
    version = workflow_step(make_release, "Get version and check tag")
    parser_call = "from scripts.build_release import parse_addon_xml"

    assert parser_call in version
    assert "string(/addon/@version)" not in version
    parser_index = version.index(parser_call)
    filename_index = version.index("filename=${{ github.event.repository.name }}-${version}.zip")
    version_output_index = version.index(
        'echo "version=$version" >> "$GITHUB_OUTPUT"'
    )
    filename_output_index = version.index(
        'echo "filename=$filename" >> "$GITHUB_OUTPUT"'
    )
    assert "$GITHUB_OUTPUT" not in version[:parser_index]
    assert parser_index < filename_index < version_output_index < filename_output_index


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
    assert "steps.create-zip.outputs.filename" not in verify
    assert make_release.rstrip().endswith(verify.rstrip())


def test_release_builds_zip_before_tag_creation():
    """Verify workflow order: Create Zip -> Verify Zip -> Check tag -> Create tag -> Create Release -> Verify asset."""
    make_release = (WORKFLOWS / "make-release.yml").read_text(encoding="utf-8")

    # Find step indices
    create_zip_idx = make_release.index("      - name: Create Zip\n")
    verify_zip_idx = make_release.index("      - name: Verify Zip\n")
    check_tag_idx = make_release.index("      - name: Check tag points to current commit (retry safety)\n")
    create_tag_idx = make_release.index("      - name: Create and push tag\n")
    create_release_idx = make_release.index("      - name: Create Release\n")
    verify_asset_idx = make_release.index("      - name: Verify release asset\n")

    # Order: Create Zip -> Verify Zip -> Check tag -> Create tag -> Create Release -> Verify asset
    assert create_zip_idx < verify_zip_idx, "Create Zip must come before Verify Zip"
    assert verify_zip_idx < check_tag_idx, "Verify Zip must come before Check tag"
    assert check_tag_idx < create_tag_idx, "Check tag must come before Create tag"
    assert create_tag_idx < create_release_idx, "Create tag must come before Create Release"
    assert create_release_idx < verify_asset_idx, "Create Release must come before Verify asset"


def test_release_has_exact_head_check_for_retry():
    """On retry with existing tag but missing asset, fail unless tag points to current commit."""
    make_release = (WORKFLOWS / "make-release.yml").read_text(encoding="utf-8")

    # Check for the exact-head check step
    assert "Check tag points to current commit (retry safety)" in make_release
    assert "tag_sha=$(git rev-parse" in make_release
    assert 'if [ "$tag_sha" != "${{ github.sha }}" ]; then' in make_release
    assert "Refusing to attach asset built from different commit" in make_release
    assert "exit 1" in make_release


def test_release_quotes_output_path():
    """Output path in Create Zip must be quoted: \"${GITHUB_WORKSPACE}/${filename}\"."""
    make_release = (WORKFLOWS / "make-release.yml").read_text(encoding="utf-8")

    create_zip_step = workflow_step(make_release, "Create Zip")
    assert '"${GITHUB_WORKSPACE}/${filename}"' in create_zip_step


def test_validated_package_contains_only_runtime_files(tmp_path):
    from scripts.build_release import build_package, validate_package

    source = tmp_path / "source"
    (source / "resources" / "lib").mkdir(parents=True)
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.gronkhtv" version="1.2.3" name="GronkhTV" />',
        encoding="utf-8",
    )
    (source / "addon.py").write_text("print('runtime')\n", encoding="utf-8")
    (source / "resources" / "settings.xml").write_text(
        "<settings />\n", encoding="utf-8"
    )
    (source / "resources" / "lib" / "client.py").write_text(
        "CLIENT = True\n", encoding="utf-8"
    )

    output = tmp_path / "plugin.video.gronkhtv-1.2.3.zip"
    info = build_package(source, output)

    assert info["filename"] == output.name
    assert info["members"] == [
        "plugin.video.gronkhtv/addon.py",
        "plugin.video.gronkhtv/addon.xml",
        "plugin.video.gronkhtv/resources/lib/client.py",
        "plugin.video.gronkhtv/resources/settings.xml",
    ]
    assert validate_package(output) == []
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == info["members"]


def test_local_validate_release_module_is_dead_code():
    """GREEN: tools/validate_release.py is gone; production uses the pinned reusable workflow."""
    dead_module = ROOT / "tools" / "validate_release.py"
    assert not dead_module.exists(), (
        "tools/validate_release.py must not exist after dead-code removal"
    )


def test_evidence_fields_match_notifier_contract():
    source = pinned_text("addon-publication/notify_repository.py", NOTIFIER_PIN)
    assert "def validate_evidence(" in source
    for field in (
        "candidate_sha",
        "validation_run_id",
        "validation_head_sha",
        "addon_id",
        "addon_version",
        "asset_name",
        "artifact_sha256",
        "publication_id",
    ):
        assert f'"{field}"' in source or f"'{field}'" in source, (
            f"pinned notifier must validate field {field}"
        )


def test_evidence_json_roundtrip_matches_notifier_schema():
    """The evidence JSON schema is defined by the pinned reusable workflow, not a local helper."""
    source = pinned_text("addon-publication/notify_repository.py", NOTIFIER_PIN)
    assert '"tag"' in source or "'tag'" in source, (
        "pinned notifier must handle the tag field"
    )
    assert "candidate SHA must be 40 lowercase hexadecimal characters" in source
    assert "artifact SHA-256 must be 64 lowercase hexadecimal characters" in source
    assert "publication ID does not match configured identity" in source
    assert "package filename does not match configured identity" in source


def test_addon_validations_calls_pinned_package_only_for_develop_push():
    text = (WORKFLOWS / "addon-validations.yml").read_text(encoding="utf-8")
    workflow = load_workflow(WORKFLOWS / "addon-validations.yml")
    package = workflow["jobs"]["package"]

    assert workflow["on"] == {
        "push": {"branches": ["main", "develop"]},
        "pull_request": {"branches": ["main", "develop"]},
    }
    assert package["needs"] == "addon-validations"
    assert package["if"] == (
        "github.event_name == 'push' && github.ref == 'refs/heads/develop'"
    )
    assert package["uses"] == PACKAGE_WORKFLOW
    assert package["permissions"] == {"contents": "read", "id-token": "write"}
    assert package["with"] == {
        "addon_id": "plugin.video.gronkhtv",
        "runtime_entries_json": '["addon.xml","addon.py","resources/"]',
    }
    assert "runs-on" not in package
    assert "steps" not in package
    assert "actions/upload-artifact" not in text
    assert "from scripts.build_release import build_package" not in text
    assert "from tools.validate_release" not in text


def test_addon_validations_has_no_publication_job():
    workflow = load_workflow(WORKFLOWS / "addon-validations.yml")
    assert "publication" not in workflow["jobs"]


def test_local_notifier_is_a_strict_pinned_reusable_wrapper():
    path = WORKFLOWS / "notify-repository.yml"
    text = path.read_text(encoding="utf-8")
    workflow = load_workflow(path)
    call = workflow["on"]["workflow_call"]
    job = workflow["jobs"]["notify-repository"]
    expected_inputs = {
        "validation_run_id",
        "validation_head_sha",
        "validation_event",
        "addon_id",
        "addon_version",
        "asset_name",
        "artifact_sha256",
        "publication_id",
    }

    assert set(call["inputs"]) == expected_inputs
    assert all(
        spec in (
            {"required": "true", "type": "string"},
            {"required": "true", "type": "number"},
        )
        for spec in call["inputs"].values()
    )
    assert call["inputs"]["validation_run_id"]["type"] == "number"
    assert call["inputs"]["validation_head_sha"]["type"] == "string"
    assert call["inputs"]["validation_event"]["type"] == "string"
    assert call["secrets"] == {
        "REPO_DISPATCH_TOKEN": {"required": "true"}
    }
    assert job["uses"] == NOTIFIER_WORKFLOW
    assert job["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
    }
    assert job["with"] == {
        "source_repository": "${{ github.repository }}",
        "candidate_sha": "${{ inputs.validation_head_sha }}",
        "validation_run_id": "${{ inputs.validation_run_id }}",
        "validation_workflow": "Add-on Validations",
        "validation_workflow_path": ".github/workflows/addon-validations.yml",
        "validation_event": "${{ inputs.validation_event }}",
        "expected_branch": "develop",
        "addon_id": "${{ inputs.addon_id }}",
        "addon_version": "${{ inputs.addon_version }}",
        "asset_name": "${{ inputs.asset_name }}",
        "artifact_sha256": "${{ inputs.artifact_sha256 }}",
        "publication_id": "${{ inputs.publication_id }}",
    }
    assert all(job["with"].values())
    assert job["secrets"] == {
        "REPO_DISPATCH_TOKEN": "${{ secrets.REPO_DISPATCH_TOKEN }}"
    }
    assert "runs-on" not in job
    assert "steps" not in job
    assert text.count("${{ secrets.") == 1
    assert "repository-dispatch" not in text
    assert "client-payload" not in text


def test_pinned_package_contract_and_retention_are_exact():
    workflow = load_pinned_workflow(
        ".github/workflows/reusable-addon-package.yml", PACKAGE_PIN
    )
    call = workflow["on"]["workflow_call"]
    steps = workflow["jobs"]["package"]["steps"]
    uploads = [
        step
        for step in steps
        if step.get("uses", "").startswith("actions/upload-artifact@")
    ]

    assert set(call["inputs"]) == {"addon_id", "runtime_entries_json"}
    assert all(spec["required"] == "true" for spec in call["inputs"].values())
    assert set(call["outputs"]) == {
        "addon_version",
        "asset_name",
        "artifact_sha256",
        "publication_id",
    }
    assert all(spec["value"] for spec in call["outputs"].values())
    assert [step["with"]["name"] for step in uploads] == [
        "addon-package",
        "validation-evidence",
    ]
    assert [step["with"]["retention-days"] for step in uploads] == ["30", "30"]
    assert [step["with"]["if-no-files-found"] for step in uploads] == [
        "error",
        "error",
    ]


def test_pinned_notifier_requires_complete_fail_closed_identity():
    workflow = load_pinned_workflow(
        ".github/workflows/reusable-notify-repository.yml", NOTIFIER_PIN
    )
    call = workflow["on"]["workflow_call"]
    required_inputs = {
        "source_repository",
        "candidate_sha",
        "validation_run_id",
        "validation_workflow",
        "validation_workflow_path",
        "validation_event",
        "expected_branch",
        "addon_id",
        "addon_version",
        "asset_name",
        "artifact_sha256",
        "publication_id",
    }

    assert set(call["inputs"]) == required_inputs
    assert all(spec["required"] == "true" for spec in call["inputs"].values())
    assert call["inputs"]["validation_run_id"]["type"] == "number"
    assert call["secrets"] == {
        "REPO_DISPATCH_TOKEN": {
            "description": "Token limited to dispatching repository.serph91p.",
            "required": "true",
        }
    }
    assert workflow["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
    }
    steps = workflow["jobs"]["notify"]["steps"]
    validation = next(step for step in steps if step.get("id") == "validate")
    dispatch = next(step for step in steps if step.get("id") == "dispatch")
    assert validation["env"]["GITHUB_TOKEN"] == "${{ github.token }}"
    assert "REPO_DISPATCH_TOKEN" not in validation["env"]
    assert dispatch["env"] == {
        "REPO_DISPATCH_TOKEN": "${{ secrets.REPO_DISPATCH_TOKEN }}",
        "CLIENT_PAYLOAD": "${{ steps.validate.outputs.client_payload }}",
    }


def test_pinned_notifier_owns_pagination_identity_and_metadata_only_payload():
    source = pinned_text("addon-publication/notify_repository.py", NOTIFIER_PIN)

    assert assigned_literal(source, "INPUT_FIELDS") == (
        "source_repository",
        "candidate_sha",
        "validation_run_id",
        "validation_workflow",
        "validation_workflow_path",
        "validation_event",
        "expected_branch",
        "addon_id",
        "addon_version",
        "asset_name",
        "artifact_sha256",
        "publication_id",
    )
    assert assigned_literal(source, "DISPATCH_FIELDS") == (
        "source_repo",
        "candidate_sha",
        "validation_run_id",
        "validation_head_sha",
        "validation_workflow",
        "validation_workflow_path",
        "expected_branch",
        "publication_id",
    )
    assert assigned_literal(source, "MAX_ARTIFACT_PAGES") == 100
    assert 'f"?per_page=100&page={page}"' in source
    assert "def _next_link(" in source
    assert "def find_required_artifacts(" in source
    assert "must occur exactly once" in source
    assert "artifact retention is outside the accepted range" in source
    assert "def validate_inputs(" in source
    assert "def validate_run(" in source
    assert "def validate_evidence(" in source
    assert "def _require_nonempty_string(" in source
    assert 'evidence["tag"] != ""' in source
    assert "candidate SHA must be 40 lowercase hexadecimal characters" in source
    assert (
        '_require_positive_integer(values["validation_run_id"], "validation run ID")'
        in source
    )
    assert "validation workflow path must be a canonical API path" in source
    assert "validation event must be push or workflow_dispatch" in source
    assert "expected branch must be develop" in source
    assert "configured add-on ID is invalid" in source
    assert "configured add-on version is invalid" in source
    assert "package filename does not match configured identity" in source
    assert "artifact SHA-256 must be 64 lowercase hexadecimal characters" in source
    assert "publication ID does not match configured identity" in source
    assert "does not match immutable notifier input" in source


def test_publication_decoupled_from_validations_run():
    publication_wf = (WORKFLOWS / "addon-publication.yml").read_text(
        encoding="utf-8"
    )
    publication = load_workflow(WORKFLOWS / "addon-publication.yml")
    assert publication["on"]["workflow_run"]["workflows"] == [
        "Add-on Validations"
    ]
    assert (
        "github.event.workflow_run.conclusion == 'success'" in publication_wf
    )
    assert (
        "github.event.workflow_run.event == 'push'" in publication_wf
    )
    assert "github.event.workflow_run.head_branch == 'develop'" in publication_wf
    assert "workflow_run" not in publication["on"].get("push", {})
    assert "workflow_run" not in publication["on"].get("pull_request", {})

    notify_call = publication["jobs"]["notify"]
    assert notify_call["uses"] == "./.github/workflows/notify-repository.yml"
    assert notify_call["with"]["validation_run_id"] == (
        "${{ github.event.workflow_run.id }}"
    )
    assert notify_call["with"]["validation_head_sha"] == (
        "${{ github.event.workflow_run.head_sha }}"
    )
    assert "github.run_id" not in str(notify_call["with"].values())
    assert "github.sha" not in str(notify_call["with"].values())

    addon_validations = load_workflow(WORKFLOWS / "addon-validations.yml")
    assert "publication" not in addon_validations["jobs"]


def test_addon_publication_pins_third_party_actions_to_immutable_sha():
    """Every third-party action reference in addon-publication.yml must use a full 40-char SHA."""
    text = (WORKFLOWS / "addon-publication.yml").read_text(encoding="utf-8")
    mutable = re.compile(r"uses:\s+\S+@v\d+\b")
    match = mutable.search(text)
    assert match is None, f"mutable tag reference found: {match.group()}"
    assert "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" in text


def test_local_notifier_accepts_and_forwards_validation_identity():
    path = WORKFLOWS / "notify-repository.yml"
    text = path.read_text(encoding="utf-8")
    workflow = load_workflow(path)
    call = workflow["on"]["workflow_call"]
    job = workflow["jobs"]["notify-repository"]
    assert "validation_run_id" in call["inputs"]
    assert "validation_head_sha" in call["inputs"]
    assert "validation_event" in call["inputs"]
    assert call["inputs"]["validation_run_id"]["type"] == "number"
    assert call["inputs"]["validation_head_sha"]["type"] == "string"
    assert call["inputs"]["validation_event"]["type"] == "string"
    assert "github.run_id" not in text
    assert "github.sha" not in text
    assert job["with"]["validation_run_id"] == "${{ inputs.validation_run_id }}"
    assert job["with"]["candidate_sha"] == "${{ inputs.validation_head_sha }}"
    assert job["with"]["validation_event"] == "${{ inputs.validation_event }}"
    assert "github.event.workflow_run" not in text


def test_local_callers_keep_dispatch_and_package_bytes_target_owned():
    local = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            WORKFLOWS / "addon-publication.yml",
            WORKFLOWS / "notify-repository.yml",
        )
    )

    for forbidden in (
        "client-payload",
        "repository-dispatch",
        "archive_download_url",
        "listWorkflowRunArtifacts",
        "downloadArtifact",
        "actions/upload-artifact",
        "build_package(",
        "validation-evidence.json",
        "secrets: inherit",
    ):
        assert forbidden not in local


def test_local_workflows_forbid_secrets_inherit_and_forward_only_dispatch_token():
    publication = (WORKFLOWS / "addon-publication.yml").read_text(encoding="utf-8")
    notifier = (WORKFLOWS / "notify-repository.yml").read_text(encoding="utf-8")

    assert "secrets: inherit" not in publication
    assert "secrets: inherit" not in notifier

    pub_workflow = load_workflow(WORKFLOWS / "addon-publication.yml")
    notify_call = pub_workflow["jobs"]["notify"]
    assert "secrets" in notify_call
    assert set(notify_call["secrets"]) == {"REPO_DISPATCH_TOKEN"}
    assert notify_call["secrets"]["REPO_DISPATCH_TOKEN"] == (
        "${{ secrets.REPO_DISPATCH_TOKEN }}"
    )

    notifier_workflow = load_workflow(WORKFLOWS / "notify-repository.yml")
    notifier_job = notifier_workflow["jobs"]["notify-repository"]
    assert "secrets" in notifier_job
    assert set(notifier_job["secrets"]) == {"REPO_DISPATCH_TOKEN"}
    assert notifier_job["secrets"]["REPO_DISPATCH_TOKEN"] == (
        "${{ secrets.REPO_DISPATCH_TOKEN }}"
    )

    assert publication.count("${{ secrets.") == 1
    assert notifier.count("${{ secrets.") == 1
