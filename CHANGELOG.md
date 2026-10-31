# Update log

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
