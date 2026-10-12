"""Pydantic domain models for Add-on Stack files used in batch installations."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from aiaddons.core.exceptions import ManifestValidationError


class StackAddonItem(BaseModel):
    """An individual add-on entry in a stack file with optional pinned version."""

    model_config = ConfigDict(extra="forbid")

    id: str
    version: str | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        clean_id = v.strip().lower()
        if not clean_id:
            raise ValueError("Add-on ID cannot be empty.")
        if not re.match(r"^[a-z0-9-_]+$", clean_id):
            raise ValueError(f"Invalid add-on ID '{v}'. Must be lowercase alphanumeric or dashes.")
        return clean_id


class AddonStack(BaseModel):
    """Stack file specification for batch installations."""

    model_config = ConfigDict(extra="forbid")

    version: str | None = "1.0"
    name: str | None = None
    description: str | None = None
    addons: list[str | StackAddonItem] = Field(default_factory=list)

    @field_validator("addons")
    @classmethod
    def validate_addons_not_empty(cls, v: list[str | StackAddonItem]) -> list[str | StackAddonItem]:
        if not v:
            raise ValueError("Stack file must contain at least one add-on entry in 'addons'.")
        return v


def find_stacks_dir(registry_dir: Path | None = None) -> Path | None:
    """Locate registry stacks directory.

    Searches candidates:
    1. Explicit registry_dir and its sibling/sub 'stacks' directory.
    2. Bundled package directory in site-packages (aiaddons/registry/stacks).
    3. Repo-checkout path for development (<repo_root>/registry/stacks).
    """
    candidates: list[Path] = []
    if registry_dir is not None:
        candidates.append(registry_dir / "stacks")
        candidates.append(registry_dir.parent / "stacks")

    pkg_dir = Path(__file__).resolve().parents[2]
    candidates.append(pkg_dir / "registry" / "stacks")

    if len(pkg_dir.parents) >= 2:
        candidates.append(pkg_dir.parents[1] / "registry" / "stacks")

    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate
    return None


def parse_stack_file(
    file_path: Path | str, registry_dir: Path | None = None
) -> list[tuple[str, str | None]]:
    """Parse and validate a stack YAML or JSON file.

    Returns a list of (addon_id, pinned_version) tuples.
    Raises ManifestValidationError if the file does not exist, cannot be read, or fails schema validation.
    """
    path_obj = Path(file_path) if isinstance(file_path, str) else file_path
    raw_str = str(file_path).strip()

    # 1. Local file on disk takes precedence
    resolved_path = path_obj.expanduser().resolve()
    target_path: Path | None = None

    if resolved_path.is_file():
        print(f"Using local stack file: {resolved_path}")
        target_path = resolved_path
    else:
        # 2. Bundled fallback
        # Strict validation: accept only a bare filename or a registry/stacks/<name> form.
        # Reject absolute paths, "..", and any other path separators.
        is_abs = (
            path_obj.is_absolute()
            or Path(raw_str).is_absolute()
            or bool(re.match(r"^[a-zA-Z]:[/\\]", raw_str))
            or raw_str.startswith("/")
            or raw_str.startswith("\\")
        )
        has_traversal = ".." in raw_str

        norm_str = raw_str.replace("\\", "/")
        bare_name: str | None = None

        if not is_abs and not has_traversal:
            if norm_str.startswith("registry/stacks/"):
                remainder = norm_str[len("registry/stacks/") :]
                if "/" not in remainder and remainder:
                    bare_name = remainder
            elif "/" not in norm_str and norm_str:
                bare_name = norm_str

        if bare_name is not None:
            stacks_dir = find_stacks_dir(registry_dir)
            if stacks_dir is not None:
                candidates = [
                    stacks_dir / bare_name,
                    stacks_dir / f"{bare_name}.yaml",
                    stacks_dir / f"{bare_name}.yml",
                    stacks_dir / f"{bare_name}.json",
                ]
                for cand in candidates:
                    if cand.is_file():
                        print(f"Using bundled stack: {cand.name}")
                        target_path = cand
                        break

        if target_path is None:
            raise ManifestValidationError(
                file_path=path_obj,
                message=f"Stack file '{file_path}' does not exist or is not a file (bundled stacks were also searched).",
            )

    try:
        content = target_path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        raise ManifestValidationError(
            file_path=path_obj,
            message=f"Failed to read stack file '{file_path}': {exc}",
        ) from exc

    if not content:
        raise ManifestValidationError(
            file_path=path_obj,
            message=f"Stack file '{file_path}' is empty.",
        )

    try:
        raw_data = yaml.safe_load(content)
    except Exception as exc:
        raise ManifestValidationError(
            file_path=path_obj,
            message=f"Failed to parse YAML/JSON in stack file '{file_path}': {exc}",
        ) from exc

    if not isinstance(raw_data, dict):
        raise ManifestValidationError(
            file_path=path_obj,
            message=f"Invalid stack file structure in '{file_path}': root element must be a dictionary/object.",
        )

    try:
        stack = AddonStack.model_validate(raw_data)
    except ValidationError as exc:
        error_details = "; ".join(
            f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ManifestValidationError(
            file_path=path_obj,
            message=f"Stack file schema validation failed: {error_details}",
        ) from exc

    results: list[tuple[str, str | None]] = []
    for entry in stack.addons:
        if isinstance(entry, str):
            clean_id = entry.strip().lower()
            if not clean_id or not re.match(r"^[a-z0-9-_]+$", clean_id):
                raise ManifestValidationError(
                    file_path=path_obj,
                    message=f"Invalid add-on ID '{entry}' in stack file. Must be lowercase alphanumeric or dashes.",
                )
            results.append((clean_id, None))
        elif isinstance(entry, StackAddonItem):
            results.append((entry.id, entry.version))

    return results
