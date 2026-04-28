"""infra/fixtures.py — S3-cached replay fixtures. Spec FND-6.

Acceptance:
  - Historical data downloaded to S3 with content-addressed paths
  - Fixture loader validates checksums
  - Replays read ONLY from fixtures, never live API

TODO Sprint 2.
"""
from __future__ import annotations


def load_fixture(path: str): raise NotImplementedError("FND-6 — TODO Sprint 2")
def cache_to_s3(local_path: str, s3_uri: str) -> str: raise NotImplementedError("FND-6")
