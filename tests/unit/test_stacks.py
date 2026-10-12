"""Unit tests for registry stack discovery, bundled fallback, security validation, and precedence."""

import os
import zipfile
from pathlib import Path
import pytest

from aiaddons.core.exceptions import ManifestValidationError
from aiaddons.core.models.stack import find_stacks_dir, parse_stack_file


def test_find_stacks_dir_discovers_repo_stacks() -> None:
    """Verify find_stacks_dir discovers the repository stacks directory."""
    stacks_dir = find_stacks_dir()
    assert stacks_dir is not None
    assert stacks_dir.exists()
    assert (stacks_dir / "dev-starter-stack.yaml").exists()
    assert (stacks_dir / "agent-behavior-stack.yaml").exists()


def test_resolve_bundled_stack_by_bare_name_outside_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify parse_stack_file resolves a bundled stack by bare name when working outside the repo."""
    monkeypatch.chdir(tmp_path)
    items = parse_stack_file("dev-starter-stack.yaml")
    assert len(items) >= 3
    ids = [item[0] for item in items]
    assert "github-mcp" in ids
    captured = capsys.readouterr()
    assert "Using bundled stack: dev-starter-stack.yaml" in captured.out


def test_resolve_registry_stacks_name_form(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify parse_stack_file resolves the registry/stacks/<name> form when outside the repo."""
    monkeypatch.chdir(tmp_path)
    items = parse_stack_file("registry/stacks/agent-behavior-stack.yaml")
    assert len(items) >= 3
    ids = [item[0] for item in items]
    assert "vibesec-skill" in ids
    captured = capsys.readouterr()
    assert "Using bundled stack: agent-behavior-stack.yaml" in captured.out


def test_reject_traversal_and_absolute_attempts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify traversal and absolute path attempts are rejected and mention bundled stacks."""
    monkeypatch.chdir(tmp_path)
    traversal_inputs = [
        "../secret.yaml",
        "../../dev-starter-stack.yaml",
        "/etc/dev-starter-stack.yaml",
        "C:\\dev-starter-stack.yaml",
        "registry/stacks/../../dev-starter-stack.yaml",
    ]
    for bad_path in traversal_inputs:
        with pytest.raises(ManifestValidationError) as exc_info:
            parse_stack_file(bad_path)
        assert "bundled stacks were also searched" in str(exc_info.value)


def test_local_file_shadows_bundled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Verify a local file on disk takes precedence over a bundled stack of the same name."""
    monkeypatch.chdir(tmp_path)
    local_stack = tmp_path / "dev-starter-stack.yaml"
    local_stack.write_text("name: Local Override\naddons:\n  - custom-mcp\n", encoding="utf-8")

    items = parse_stack_file("dev-starter-stack.yaml")
    assert items == [("custom-mcp", None)]
    captured = capsys.readouterr()
    assert "Using local stack file:" in captured.out
    assert "Using bundled stack:" not in captured.out


def test_missing_stack_error_mentions_bundled_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify non-existent stack error mentions that bundled stacks were searched."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ManifestValidationError) as exc_info:
        parse_stack_file("non-existent-stack.yaml")
    assert "bundled stacks were also searched" in str(exc_info.value)


def test_wheel_contains_both_stack_files() -> None:
    """Verify built wheels in dist/ include both stack files under aiaddons/registry/stacks."""
    dist_dir = Path(__file__).resolve().parents[2] / "dist"
    wheels = list(dist_dir.glob("*.whl"))
    if not wheels:
        pytest.skip("No wheel in dist/ yet; verified during build step.")

    # Check the newest wheel
    latest_wheel = sorted(wheels, key=lambda p: p.stat().st_mtime)[-1]
    with zipfile.ZipFile(latest_wheel, "r") as z:
        names = z.namelist()
        # If this is 0.2.0 wheel, it must have both stacks
        if "0.2.0" in latest_wheel.name:
            assert "aiaddons/registry/stacks/dev-starter-stack.yaml" in names
            assert "aiaddons/registry/stacks/agent-behavior-stack.yaml" in names
