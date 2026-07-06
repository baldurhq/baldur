"""Package-native data resources.

Status: Internal

Holds ``V1_LAUNCH_MANIFEST.yaml`` — the authoritative v1.0 tier/default
contract — as a package resource. It ships with ``src/baldur`` in both
editable and wheel installs, so the feature_manifest loader and the
bootstrap tier-warning path resolve it uniformly via
``importlib.resources.files("baldur._data")``.
"""
