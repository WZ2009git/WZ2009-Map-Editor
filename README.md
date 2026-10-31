> Development source: the portable current-map release is still undergoing full-project reconstruction checks. No ready-to-use release is published yet.

# WZ2009 Map Editor

A Blender workshop for the WZ2009 project. Arrange maps in a lightweight composition, open individual maps for detailed editing, load their surrounding terrain, and browse assets from your own MW2009 installation.

Project updates: [@WZ_2009](https://x.com/WZ_2009).

Current source version: **0.10.6**. See the [changelog](CHANGELOG.md) for fixes, validation and known limitations.

## What the editor can do

- Arrange maps together using lightweight overview geometry, then load one map's editable details when needed.
- Prepare disconnected map geometry as individual parts and native props as separate placements. Prepared libraries are cached for later loads.
- Move or rotate a whole map after splitting it, and adjust its world height using absolute Z values or raise/lower steps.
- Load original surrounding terrain as editable outskirts, save changes and hide that layer independently.
- Create an adjustable shared water reference to help align map heights and shorelines.
- Browse assets resolved from your own MW2009 installation, without loading every asset into the scene.

The current composition has 23 map instances, including copied fragments. This is not a claim of 23 different original maps. Private prepared Blender libraries and game assets are not included in the repository.

## Current distribution status

This is development source. The editor features have been checked in local workspaces, but full reconstruction of the portable current-map project is still under verification. A source download is not yet a verified ready-to-use distribution of the author's current edited map. The setup workflow below describes the available installation tools; it does not remove that limitation.

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

### Load and edit a map

1. Select a map overview and choose **Load map details**. Other maps can remain lightweight previews.
2. Select an individual part or prop. Use Blender's **G**, **R** and **S** to move, rotate or scale it.
3. For vertex/edge/face editing, choose **Edit selected mesh faces**. This makes a shared prop mesh independent before editing that placement. Direct **Tab** entry retains Blender's normal linked-mesh behaviour.
4. Use **Save detail version** to write a new detail library, or **Save + unload map** to save and release its loaded layers. Save the Blender composition afterward to retain the updated library references.

Preparation separates geometry by connectivity, not by inferred buildings. Physically connected structures can remain one piece. Repeated props share mesh data to save memory until edited independently. Loading reuses prepared files; it is not instantaneous. Blender displays loading phases and the last completed duration. Animated/custom libraries may use the slower compatibility path.

### Move a whole map or change its height

In **Map height and shared water**, choose the map from the list or select one of its parts and click **Use map from selected part**. Choose **Select whole map - G / R** to move or rotate its parent, including split parts, props and loaded outskirts.

Set **Target root Z** and click **Apply map height**, or use **Height step** with **Raise map / Lower map**. These values use Blender world units. The map root is a placement pivot, not a detected ground height; original-source physical scale remains unverified.

### Edit surrounding terrain

Choose **Load outskirts as parts**. Disconnected outskirts geometry and prop placements are prepared once and cached. After editing, choose **Save + hide outskirts** to save a new version and unload only that layer. Loading it again restores the saved version. Save the composition to keep its updated references.

### Align against shared water

Set **Water Z** and **Water width / length**, then choose **Create / update water reference**. **Fit water around current maps** fits the surface around the current overview bounds. Apply again after changing size, level or **Show water preview**.

The surface is a flat, non-selectable editor reference. It does not recreate removed native water, carve terrain or move maps automatically. **Set root to water + offset** explicitly places the chosen map's pivot at the water level plus your offset; adjust the offset visually for its shoreline.

Ultra-Water's FFT waves, foam, underwater rendering and buoyancy are **not integrated**. The current preview does not simulate water physics.

See the [water and map-alignment guide](docs/WATER_ALIGNMENT.md) for the workflow and Ultra-Water integration assessment.

### Browse assets and manage performance

**Browse local assets** opens the catalog. Geometry and textures are loaded from the verified local cache when needed. Keep unused map details and outskirts unloaded to reduce memory and viewport work. Large maps and assets can still be expensive; a cached library avoids repeated preparation but still needs to be loaded into Blender.

Map instances and editable elements have persistent IDs. Names are labels. Navigation connections remain proposed until validated; displayed connections do not prove traversability or collision clearance.

## Files and recovery

`.local/` contains your installation selection, converted assets, generated Blender workspace and detail libraries. Keep this folder private. Back up your edited workspace and its detail libraries together. Do not delete `.local/` after editing unless you have a complete backup: preparing again recreates the distributed baseline, not your unsaved edits.

Setup logs are `.local/reconstruction.log` and the per-conversion logs inside `.local/`. If preparation fails, fix the reported issue and retry. Already verified native conversions are retained.

## Scope

This is an editor and visual reconstruction workflow. It does not establish that exported edits are playable, collision-safe or supported by a game runtime. Existing source registration is retained; independent physical scale calibration remains unverified. The distributed project starts a fresh editing history; private source-workspace undo history is not included.

The v0.10.6 work passed 54 focused checks and local headless checks for map movement, water reference settings and edited outskirts save/unload/reload. These checks are not a full-suite or portable-release certification. The changelog records outstanding legacy translation expectations, missing aircraft fixtures and reconstruction limitations.

The repository contains editor code, project placements, native face-selection references and explicitly editor-authored landscape geometry. Original game meshes, textures, sound files and private Blender projects are excluded. Game content remains subject to its original owners' rights.
