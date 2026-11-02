"""Shared fixtures and configuration for all tests."""

from __future__ import annotations

# Ensure pytest uses the package import mode (faster discovery).
# The tests/__init__.py and sub-package __init__.py files together
# with testpaths = ["tests"] in pyproject.toml prevent pytest from
# scanning the entire workspace (data/, models/, etc.).
