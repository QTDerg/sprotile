# Blender workflow checks

Run these checks in an isolated Blender process from the repository root. For example, this command starts with an empty mesh, assigns an atlas, maps a tile, applies a blend, and checks Undo and Redo.

```sh
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python tests/test_atlas_setup.py
```

Use the same command with any of the following scripts. Each suite creates its own synthetic meshes and images, writes a JSON report under `tests/`, and returns a failing exit code if a check fails.

| Script | Coverage |
| --- | --- |
| `test_quick_start_ui.py` | Quick start transitions, preferences, Paint B and Path |
| `test_paint_and_path.py` | Painted areas, path placement, updating, replacing and removing paths |
| `test_seam_and_hints.py` | Seams, preservation of neighbouring blends and panel guidance |
| `test_shared_blend.py` | Shared materials, stored face data, rollback and Undo |
| `test_compact_storage.py` | UV storage, opening older blends and FBX export |
| `test_overlay_browser.py` | Atlas scans, filters, edited images and tile bounds |
| `test_overlay_maker.py` | Soft-edged overlays and preparing UVs for Unreal |
