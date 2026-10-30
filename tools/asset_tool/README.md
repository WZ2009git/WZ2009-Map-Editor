# Standalone converter adapter

These are the editor-specific offline entrypoint and exporter adapter sources.
The compiled release tool uses the existing Apache-2.0 asset-reader workspace.
Its transitive source closure is recorded by hash in SOURCE_PROVENANCE.json;
unrelated game-project runtime source is deliberately not vendored here.
This directory is not a standalone Cargo workspace. End users use the pinned
release converter and do not need Rust. Rebuilding the converter requires the
matching reader workspace; its full transitive source is not vendored here.
