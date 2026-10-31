> Development source: the portable current-map release is still undergoing full-project reconstruction checks. No ready-to-use release is published yet.

# WZ2009 Map Editor

A Blender workshop for the WZ2009 project. Arrange maps in a lightweight composition, open individual maps for detailed editing, load their surrounding terrain, and browse assets from your own MW2009 installation.

Project updates: [@WZ_2009](https://x.com/WZ_2009).

Current source version: **0.10.1**. See the [update log](CHANGELOG.md) for fixes, validation and known limitations.

## Requirements

Windows 64-bit, Blender 5.2.2 or compatible, and your own original MW2009 installation. The current project requires exact original FastFile revisions. Setup checks the files before conversion and reports missing or incompatible files. Other language editions are not yet verified.

## First start

1. Extract the editor into its own writable folder, outside your game installation.
2. Run `tools/public_start.ps1` in PowerShell. If Blender cannot be found, pass `-BlenderPath "C:\path\to\blender.exe"`.
3. In Blender's WZ2009 sidebar, choose **Select MW2009 folder** and select the installation containing `zone` and `main`.
4. Choose **Prepare local assets**. The standalone converter reads your original files and creates an editor-local cache. First setup can take several minutes and considerable disk space. Completed conversions are reused after a retry.
5. Choose **Open prepared workspace** when setup finishes.

No game assets are downloaded. A source download may fetch the checksum-pinned standalone converter from this project's GitHub release. The game is never launched by setup, and the installation is read-only.

## Editing

The composition uses simplified overview geometry. Select a map and load its details when you need to edit individual elements. Save detail edits before unloading them. Static detail libraries use the fast editable loader; Blender shows loading phases and the last completed load duration. Large maps still take time, and animated/custom libraries may use the slower compatibility path. Load the map's outskirts separately when you need its surrounding native terrain. Keep unused maps unloaded to reduce memory and viewport work.

Detail loading prepares disconnected map geometry as separate parts and restores native prop placements as individual objects. The prepared private libraries are reused on later loads. Connected architecture may remain one piece; use face editing for finer changes. **Edit selected mesh faces** makes a repeated prop's mesh independent before editing that placement. Entering Edit Mode directly through Blender retains Blender's normal linked-mesh behaviour.

**Browse local assets** opens the catalog. Geometry and textures are loaded from the verified local cache when needed. Large assets can still be expensive; the catalog itself does not require loading every model into the scene.

Map instances and editable elements have persistent IDs. Names are labels. Navigation connections remain proposed until validated; displayed connections do not prove traversability or collision clearance.

## Files and recovery

`.local/` contains your installation selection, converted assets, generated Blender workspace and detail libraries. Keep this folder private. Back up your edited workspace and its detail libraries together. Do not delete `.local/` after editing unless you have a complete backup: preparing again recreates the distributed baseline, not your unsaved edits.

Setup logs are `.local/reconstruction.log` and the per-conversion logs inside `.local/`. If preparation fails, fix the reported issue and retry. Already verified native conversions are retained.

## Scope

This is an editor and visual reconstruction workflow. It does not establish that exported edits are playable, collision-safe or supported by a game runtime. Existing source registration is retained; independent physical scale calibration remains unverified. The distributed project starts a fresh editing history; private source-workspace undo history is not included.

The repository contains editor code, project placements, native face-selection references and explicitly editor-authored landscape geometry. Original game meshes, textures, sound files and private Blender projects are excluded. Game content remains subject to its original owners' rights.
