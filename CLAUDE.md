# cfw_burn_scar_mapping — workflow notes

Sentinel-2 burn scar detection without a pre-existing fire polygon. Phase 1: AOI-scan mode.
Phase 2 (next session): FIRMS-guided mode using `firms_rep` ER event type.

Custom package: `burn-scar-tasks` (bundled into compiled dir after each compile).

---

## Task chain (current, 10-widget layout)

`set_workflow_details` → `set_er_connection` → `set_gee_connection` →
`set_aoi_group_name` (custom; AOI spatial feature group, required) →
`get_spatial_features_group` id=`aoi_features` (built-in) →
`set_overlay_group_name` (custom; optional, default "") →
`get_spatial_features_group` id=`overlay_features` (`skipif: any_dependency_is_empty_string`) →
`set_base_maps` →
`compute_burn_scar_s2` (custom; GEE S2 harmonised; partial: client + aoi; user params: fire_start_date, fire_end_date, pre_fire_days=90, post_fire_days=30, dnbr_threshold=0.2, scale=100) →
`create_burn_scar_layer` (custom; zoom=False — AOI is the zoom target) →
`create_styled_overlay_layer` id=`aoi_layer` (partial: zoom=true — always present, compact bounds → reliable zoom target) →
`create_styled_overlay_layer` id=`overlay_layer` (partial: zoom=false; `skipif: any_dependency_skipped, any_is_empty_df`) →
`combine_burn_scar_layers` (custom; `skipif: any_is_empty_df` ONLY — handles SkipSentinel) →
`draw_ecomap` → `persist_text` → `create_map_widget_single_view` (`skipif: never`) →
`persist_df` id=`burn_scar_file` (filetype: geojson → Desktop Files tab) →
stat chain:
  `count_burned_area_ha` → `format_area_ha` → widget_burned →
  `count_high_severity_area_ha` → `format_area_ha` → widget_high_severity →
  `get_aoi_area_ha` → `get_percent_burned` → `format_percent_burned` → widget_percent_burned →
  `count_burn_patches` → `format_patch_count` → widget_patches →
  `get_detection_mode` → widget_mode →
  `count_pre_images` → `format_image_count` → widget_pre_scenes →
  `count_post_images` → `format_image_count` → widget_post_scenes →
  `get_dnbr_threshold` → `format_dnbr_threshold` → widget_threshold →
  `get_fire_window` → widget_fire_window →
`gather_dashboard` (`time_range: ~`).

## Dashboard layout (10 widgets, 0-indexed)

| widget_id | Widget | x | w | y | h |
|---|---|---|---|---|---|
| 0 | Burned | 0 | 2 | 0 | 3 |
| 1 | High Sev | 2 | 2 | 0 | 3 |
| 2 | % Burned | 4 | 2 | 0 | 3 |
| 3 | Patches | 6 | 2 | 0 | 3 |
| 4 | Mode | 8 | 2 | 0 | 3 |
| 5 | Pre Imgs | 0 | 2 | 3 | 3 |
| 6 | Post Imgs | 2 | 2 | 3 | 3 |
| 7 | Threshold | 4 | 2 | 3 | 3 |
| 8 | Fire Window | 6 | 4 | 3 | 3 |
| 9 | Map | 0 | 10 | 6 | 16 |

Row 1 (y=0, h=3): 2+2+2+2+2 = 10. Row 2 (y=3, h=3): 2+2+2+4 = 10. Map full-width (y=6, h=16).

## Requirements (working pattern — see CLAUDE.md Trap 28)

```yaml
requirements:
  - name: "ecoscope-platform"
    version: ">=2.15.0,<2.16.0"
    channel: "https://repo.prefix.dev/ecoscope-workflows/"
  - name: pydeck
    version: "0.9.2"
  - name: "burn-scar-tasks"
    path: "/home/sam/Ecoscope_Projects/burn-scar-tasks"
    editable: true
```

## Post-compile patch (every recompile)

```bash
cp -r /home/sam/Ecoscope_Projects/burn-scar-tasks ecoscope-workflows-*-workflow/burn-scar-tasks
sed -i 's|path = "/home/sam/Ecoscope_Projects/burn-scar-tasks"|path = "./burn-scar-tasks"|' \
  ecoscope-workflows-*-workflow/pixi.toml
cd ecoscope-workflows-*-workflow && pixi install && cd ..
```

## Traps

**Map zooms out to global view / reserve appears tiny on open.**
Symptom: `draw_ecomap` opens at zoom=9 or lower, showing a 200–300 km wide region.
Cause: `zoom=True` on the burn scar layer makes `zoom_to_bounds` fit all scattered patches —
a 40,000 ha burn across a reserve produces zoom=9 (≈270 km view). Burn patches are also
absent when no fire is detected, causing fallback to all layers including the tile layer → world view.
Fix: set `zoom=True` on the AOI boundary layer (`create_styled_overlay_layer zoom: true`),
NOT on the burn scar layer. AOI is always present, geographically compact, and gives a
consistent tight initial view regardless of burn extent or whether any patches were detected.

**GEE OOM at fine scale over large AOI.**
Symptom: `EEException: User memory limit exceeded` during `stats.getInfo()`.
Cause: `reduceToVectors` + `connectedPixelCount` at 20m over 77k ha ≈ 1.9M pixels — exceeds GEE per-request limit.
Fix: default scale=100 in `compute_burn_scar_s2`. Pre-flight pixel count check raises
`ValueError` with a suggested scale if user tries a scale that would exceed ~500k pixels.

**Burn patches overflow AOI boundary.**
Symptom: patches extend beyond the reserve outline at 100m scale.
Cause: GEE includes any pixel whose centroid is inside AOI; the pixel square straddles the edge.
Fix: client-side `gdf.geometry.intersection(aoi_union_geom)` clip after GEE vectorisation.

---

## Phase 2 plan (FIRMS integration)

New tasks to add to `burn-scar-tasks`:
- `fetch_firms_from_er(client, aoi, start_date, end_date) -> GDF` — pulls `firms_rep` events
- `cluster_firms_events(firms_gdf) -> GDF` — DBSCAN over space+time, returns cluster metadata
  (cluster_id, date_start, date_end, search_geom with 2 km VIIRS / 5 km MODIS buffer)
- `select_detection_mode(firms_clusters_gdf, aoi_gdf) -> str` — 'firms_guided' or 'aoi_scan'

Spec changes:
- New step: `fetch_firms_from_er` (between er_connection and compute)
- New step: `cluster_firms_events`
- `compute_burn_scar_s2` gets `firms_clusters` as optional input
- When `firms_clusters` non-empty: run analysis per cluster (date window + buffered geom)
- `detection_mode` column in result GDF set dynamically
- New stat card: FIRMS Hotspots Used

FIRMS ER event type slug: `firms_rep`
VIIRS buffer: 2 km | MODIS buffer: 5 km
DBSCAN params (suggested): spatial epsilon = 5 km, time epsilon = 3 days, min_samples = 1

## GitHub

Repo: https://github.com/cllrssml/burn-scar-mapping (public)
Current published version: Desktop-verified, all Phase 1 features shipped.
