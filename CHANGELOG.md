# Update log

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
