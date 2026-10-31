# Update log

## 0.10.5 - October 31, 2026

- Fixed map switching after editing original outskirts: core and outskirts now save, reload and evict their own edit records and retained source baselines. A failure saving the previous map no longer leaves users guessing why the selected map stayed in overview mode.
- Source baselines are resolved from the layer containing their persistent source/part IDs. Another layer or cached scene cannot supply the baseline accidentally.
- Saved workshop views receive a corrected viewport clipping range, preventing map geometry from disappearing at the former 1,000-unit drawing limit. Existing wider ranges and the user's viewpoint are preserved.

Original source workspaces remain unchanged. Saves create versioned detail libraries; portable reconstruction remains under verification.

Validation: 54 focused checks passed. Live normal switching loaded Skidrow (8,348 visible non-overlay meshes) and Rundown (13,119), with single-prop Edit Mode and unchanged shared copies verified in each. The full 1,195-test suite still reports legacy German-message expectation failures and missing native-aircraft provenance fixtures; these unrelated checks are not claimed as passing.

## 0.10.4 - October 31, 2026

### Consistent editable map parts

- Load map details prepares disconnected core geometry as separate parts and restores native prop placements as individual objects. Existing parts and saved edits are retained.
- Prepared versioned libraries are reused by map-instance ID and source-library SHA-256. Loading a prepared library does not repeat the split. Original workspaces and source libraries are preserved.
- Repeated props keep shared mesh data for memory efficiency. Edit selected mesh faces targets the chosen prepared part and makes a shared prop mesh independent before editing that placement.
- Preparing all prop groups uses one batch removal of the old instance hosts and avoids redundant per-placement material-slot assignments.
- New preparation data receives residency tokens so it can be released when unloading a map. Repeated props remain omitted from lightweight overview baking, matching their former instanced-preview behaviour.
- The sidebar reports prepared geometry and mesh counts, and permits preparation of an already loaded legacy map.
- Each newly serialized assembly wrapper receives its own object ID. Coincident nested empty wrappers with a legacy ID collision can be repaired in the derived copy; map, mesh, part and prop identities are preserved. Other identity collisions are rejected.

### Local preparation verified

- Prepared and reopened versioned libraries for all 23 current map instances, containing 34,800 tracked geometry parts and 111,953 individual prop placements. These counts exclude some already-separate untracked geometry; total library object counts also include assembly/helper objects.
- Preserved source-library hashes, existing part geometry/IDs, map-instance IDs and map placement. Saved resident Invasion edits were checkpointed before preparing other maps.
- Headless fixtures cover automatic preparation, source-bound cache reuse without repeat splitting, shared meshes, independent selected-prop editing, object material overrides, saved-library reopening and unique IDs after repeated saves.
- Existing regression suite: 1,195 tests, six existing skips, no failures or errors. Eleven focused parts/cache/identity checks plus the existing preview, copy, editor-repair and portable-setup checks passed.

Core separation uses geometric connectivity, not inferred building semantics. Connected structures can still require face editing. Original outskirts remain a separately loaded layer. Geometry changes still require export/runtime validation; the existing Favela full-city restore duplicate-source safeguard is unchanged. Portable project reconstruction remains under verification, and private prepared Blender libraries are not distributed in this repository.

## 0.10.3 - October 31, 2026

### Map switching and consistent previews

- Fixed loading another map after details are already loaded. The save/unload preview rebuild now accepts mixed native `Color` formats instead of stopping before the target map loads.
- Fixed a second switching failure caused by reading Blender objects after eviction had deleted them. The loader now snapshots only persistent map roots before unloading.
- Preview baking supports mirrored static props with corrected winding, UV, colour and normal corner order. Singular transforms still produce an explicit error. Original editable geometry is unchanged.
- Compatible vertex/corner colour layers are promoted to linear floating-point corner colours in the derived preview. Incompatible non-colour attributes retain separate derived channels.
- Preview reduction keeps the unreduced derived mesh if decimation changes its bounds excessively or removes all faces.

### Local workspace repair

Rebuilt and reopened all 23 map-instance previews from their existing detail libraries in a new workspace. Persistent map IDs, placements, detail geometry and original library files were preserved. Highrise's stale overview is replaced with geometry derived from its correct detail assembly. The original workspace remains available.

These are lightweight geometry previews. Fine instanced props and full textured appearance still belong to the loaded detail assembly. Private Blender workspaces and game assets are not distributed with this source update. The pre-existing Favela duplicate-ID restriction on full-city restoration remains unchanged.

### Validation

- Eight focused regressions for native attribute compatibility, missing buffers and deleted-object handling.
- Existing regression suite: 1,195 tests, six existing skips, no failures or errors.
- Headless Blender: original split/cache fixtures, mixed colour formats, mirrored winding/UV/colour data, preserved source geometry, all 23 rebuilt previews and saved-workspace reopening.
- No live game tests or game-project changes.

## 0.10.2 — October 31, 2026

### Faster editable map loading

- Static map details now use a temporary library link followed by scoped local copies instead of Blender's global append/remapping path. Loaded objects, meshes, materials, textures and instance prototypes remain locally editable.
- Sparse per-piece material slots share a retained full palette. Geometry, UVs, persistent face IDs, shared mesh instancing and material assignments are preserved; geometry is not decimated.
- Core details and surrounding native terrain use the same loader. Existing animated or unsupported libraries retain the compatibility append path and may still load slowly.
- Added loading phases in Blender's status bar and the last completed load duration in the sidebar. Loading remains synchronous and is not instantaneous.
- Scoped retained undo baselines to the current resident map so another scene's cached baseline cannot be selected accidentally. Blender-generated library/name suffixes no longer invalidate geometry fingerprints.

### Validation

Local measured full-detail loads were approximately 25 seconds for Favela, 23 seconds for Highrise and 28 seconds for Terminal, including extra geometry verification. These headless runs used warmed OS caches and exclude saving/unloading the previous map; interactive and fresh-disk results may differ. Favela outskirts loaded in approximately four seconds.

Local Blender 5.2 headless checks cover all 23 current map instances, source/copy geometry fingerprints, local editability, data eviction and preservation of original libraries and unrelated scenes. Additional fixtures cover packed images, object material overrides, instanced prototypes, saved compact material palettes and animated compatibility imports. Vertex, material and transform edits survived saving to a new library and reloading; surrounding Favela terrain loaded as local editable data.

Existing Favela detail data contains two empty source objects with the same tracked source ID. Full-city restoration remains refused by the existing duplicate-ID guard; no restore history was guessed or rewritten. This does not prevent the verified load/edit/save workflow.

No Rust extension was added: the measured bottleneck was Blender's native append/remapping and repeated material-slot updates. Portable project reconstruction remains under verification. This source update does not rewrite existing Blender workspaces or establish game-runtime support.

## 0.10.1 — October 31, 2026

This source update fixes the WZ2009 sidebar crash and map-name display in composition workspaces.

### Fixed

- Removed runtime replacement of registered Blender panel visibility callbacks. The old wrapper could recurse through Blender's RNA callback and crash while drawing the sidebar.
- Made repeated registration of the same enabled add-on safe.
- Matched map labels to geometry through persistent map IDs. Labels follow the current preview transform and use a cached median geometry anchor to reduce displacement from oversized props.
- Suppressed duplicate labels on copied map fragments.
- Disabled the old static POI-name preview when initializing workshop labels. Authored POI markers remain available.
- Hid legacy helper names and parent relationship lines in workshop views, including saved views already marked as initialized.
- Recovered an existing Blender window when file opening leaves the immediate context without a window.

### Local workspace repair verified

- Preserved all 23 map-instance IDs and existing visible geometry placements while aligning six stale map-root frames with their previews.
- Preserved the original landscape and created preview terrain openings for the 16 canonical map cores at their current placements.
- Confirmed Favela's full detail library: 487,911 faces and 2,880 editable local mesh objects.
- Saved the repair to a new Blender file and verified reopening with 16 current map labels. Original source files were preserved.

These are local project-data repairs. Game-containing Blender files are not distributed in this repository, and this source update does not automatically rewrite existing projects.

### Validation

- Existing regression suite: 1,195 tests run, six existing skips, no failures or errors.
- Six focused regression checks for label identity, copied fragments, preview selection, panel registration and saved/opened window contexts.
- Headless Blender: 22 panel polls, repeated registration, saved-file reopening and Favela editability checked.
- Live editor: repaired workspace opens and the WZ2009 sidebar draws without the reported crash.
- No live game tests or game-project changes.

### Known limitations

- Full-detail library append remains slow.
- Portable reconstruction of the complete current project remains under verification; this is a development source update, not a finished portable release.
- Changed map frames and terrain previews require export revalidation. Preview visibility checks do not establish collision clearance or runtime playability.
