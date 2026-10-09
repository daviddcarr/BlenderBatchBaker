# Auto Bake

A Blender add-on that automates baking node-shaded models into game-ready textures
(BaseColor, Roughness, and optionally Metallic, Alpha and Emission), then batch-exports them as `.glb`.

Requires Blender 4.2+ (developed on 5.2) and uses Cycles for baking.

## Install

1. Zip the contents of the `auto_bake/` folder, or use a release zip.
2. In Blender, go to **Edit → Preferences → Add-ons → ⌄ → Install from Disk** and pick the zip.
3. Open the 3D View sidebar (**N**) and go to the **Auto Bake** tab. The **?** button in the panel header shows a quick how-to.

## How it works

Each model exists several times, one copy per collection:

| Collection  | Object name        | What goes on it                                                                 |
|-------------|--------------------|---------------------------------------------------------------------------------|
| `Final`     | `Crate`            | The export model. Auto Bake gives it one material with the baked textures.      |
| `Materials` | `Crate.Mat`        | Any node materials, as many as you like: color, roughness, emission.            |
| `Metallic`  | `Crate.Met`        | Only `Metallic` (white) and `NonMetallic` (black).                              |
| `Alpha`     | `Crate.Alpha`      | Only `Opaque` (white) and `Transparent` (black). Grey gives partial transparency. |

Each texture is baked from the matching copy into the Final object's UV layout:

| Texture        | Baked from | Bake type     | Color space |
|----------------|------------|---------------|-------------|
| `_BaseColor`   | `.Mat`     | Diffuse color | sRGB        |
| `_Roughness`   | `.Mat`     | Roughness     | Non-Color   |
| `_Metallic`    | `.Met`     | Diffuse color | Non-Color   |
| `_Alpha`       | `.Alpha`   | Diffuse color | Non-Color   |
| `_Emission`    | `.Mat`     | Emit          | sRGB        |

Textures are saved as `Textures/<Name>_<Type>.png` next to the `.blend`. Dots in names become
underscores, so `Tomato.Bud` produces `Tomato_Bud_BaseColor.png`.

## Workflow

1. **Setup Collections** creates `Final`, `Materials`, and `Metallic` / `Alpha` if they're ticked under *Include*.
2. Model and UV-unwrap in `Final`.
3. **Copy Final** makes the suffixed copies.
   - Sub-collections are mirrored (`Final/Tomato` → `Materials/Tomato.Mat`).
   - Parented children are parented to their parent's copy.
   - Existing copies are never overwritten.
4. Paint the copies. You can add geometry that only exists for texturing, such as an inset for a window.
   Keep the Final object's UVs intact in every copy.
5. **Validate**, then **Bake Textures**. Bake stays locked until validation passes, and locks again if
   objects, names, meshes or materials change afterwards.
6. **Batch Export glTF** and pick a folder.
   - You get one `.glb` per top-level Final object, including its children.
   - Objects that aren't parented export individually, even inside sub-collections.

## Options

| Option              | What it does |
|---------------------|--------------|
| Metallic / Alpha / Emission | Which extra textures to bake. Emission is skipped for objects whose materials don't emit. |
| Pack into BaseColor | Store alpha in the BaseColor PNG's alpha channel instead of a separate `_Alpha.png`. This is what the glTF exporter expects. |
| Copy Spacing        | Offsets copies on X (`.Mat` −1×, `.Met` −2×, `.Alpha` −3×) so they don't overlap the Final objects. |
| Resolution          | Texture size, 512 by default. |
| Bake Samples        | Cycles samples used while baking, 16 by default. Your scene setting is restored afterwards. |
| Margin              | Pixels each UV island is extended by, to hide seams. |
| Selected Only       | Bake or export only the selected objects. A selected `.Mat` / `.Met` / `.Alpha` copy counts for its Final object. |
| Center at Origin    | Export each top-level object at 0,0,0. Rotation and scale are kept. |
| Embed Textures      | Embed the baked PNGs in each `.glb`. |

## Sharing textures between variants

For variants that share a UV layout, such as a plant's flowering and fruiting stages:

1. Select the variant(s), then select the source last so it's active.
2. Click **Share From Active**, in the sidebar's *Active Object* section.

The variant then reuses the source's material and textures. It needs no `.Mat` / `.Met` / `.Alpha` copies,
and its UVs are checked against the source's. Clear sharing with the ✕ button.

## Validation

**Errors block baking:**
- missing collections or copies;
- an empty material slot;
- a copy that uses a Final material;
- mesh data shared between a Final object and its copy;
- an unsaved `.blend`.

**Warnings are reported, but you can still bake:**
- a copy is missing UV points from the Final object, which usually means a wrong unwrap;
- face counts differ;
- an object in a source collection doesn't match any Final object.

## Notes

- Lighting is excluded from bakes, and Principled specular is zeroed during diffuse bakes,
  so baked colors match the authored Base Color exactly.
- Hidden and excluded collections and objects are revealed temporarily during bake and export, then restored.
- Blender is unresponsive while baking. Progress is printed to the system console
  (**Window → Toggle System Console** on Windows).
