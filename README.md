# Sprotile

Sprotile maps tiles from a texture atlas onto meshes in Blender 4.4 and later. Paint individual faces, fit one tile across a patch, or blend two tiles along a path or seam.

## Install

1. Choose the installable ZIP attached to the release. To build this version from source with Python 3.11 or later, run `python build.py` from the repository root. The output is `dist/Sprotile-6.0.0.zip`. [Releases](https://github.com/QTDerg/sprotile/releases)
2. If you're upgrading from the original single-file add-on, including upstream 5.3, disable and remove it in **Edit > Preferences > Add-ons**, then restart Blender. If you already use the Sprotile Extension, install the new ZIP over it.
3. Drag the ZIP into Blender and follow the installation prompt. You can also select **Install from Disk** from the menu in **Edit > Preferences > Get Extensions**.

## Map your first tile

1. Select a mesh, press **Tab** for Edit Mode, switch to Face Select, and select the faces to texture.
2. Press **N** in the 3D Viewport and open **Sprotile > Map Tiles**. If the faces need an atlas, click **Set Up Atlas…** and choose your image. This assigns a nearest-filtered material to the selected faces. You can also use faces that already have an atlas material.
3. Set **Tile Size** to the atlas cell size in pixels, choose its tile shape, then click **Open Picker**. Click a tile and choose **Map Per Face**. Switch the viewport to **Material Preview** to see the result.

The guide covers the picker, painting, patch mapping, blends, paths, seams, overlays, and Unreal export. [User guide](docs/Guide.md) · [6.0.0 release notes](docs/Sprotile-6.0.0-Notes.txt) · [Blender workflow checks](tests/README.md)

## Credits

Sprotile was created by **QTDerg** for the Spaghetti Spyro community, inspired by **Sprytile**. Its name combines Spro and Sprytile. **CrystalFissure** contributed fixes and the 6.0 update. [Spro](https://www.reddit.com/r/Spyro/comments/132t3ac/spro/) · [Sprytile](https://github.com/Sprytile/Sprytile)

Released under the MIT License. [License](LICENSE)
