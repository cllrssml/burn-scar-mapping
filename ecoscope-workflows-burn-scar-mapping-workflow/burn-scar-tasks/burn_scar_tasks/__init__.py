"""
Custom tasks for the Ecoscope Platform burn scar detection workflow.

Science:
  - Compositing:     Parks et al. 2018 (doi:10.3390/rs10060879) — Sentinel-2 mean compositing.
  - Thresholds:      Key & Benson 2006 (USGS standard severity classes).
  - Sentinel-2 burn: Roteta et al. 2019 (doi:10.1016/j.rse.2018.12.011).
  - MIRBI index:     Trigg & Flasse 2001 (Int. J. Remote Sensing 22:13).
  - Dual-index:      Bastarrika et al. 2024 (ISPRS J. Photogramm. Remote Sens. 218).
"""

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Optional

import geopandas as gpd
import numpy as np
from pydantic import Field
from pydantic.json_schema import WithJsonSchema
from wt_registry import register

# Type shims — no ecoscope imports at module level (compiler task discovery runs before ecoscope
# is on sys.path). Pattern identical to dnbr-tasks/__init__.py and hex-tasks/__init__.py.
_GDF = Annotated[Any, WithJsonSchema({"type": "ecoscope.platform.annotations.DataFrame"})]
_GEE = Annotated[str, WithJsonSchema({"type": "string", "description": "A named Google Earth Engine connection."})]
_ER  = Annotated[str, WithJsonSchema({"type": "string", "description": "A named EarthRanger data source."})]


# ── Severity class table ─────────────────────────────────────────────────────
# Key & Benson 2006 thresholds, ×1000 convention. Identical to dnbr-tasks so
# outputs are numerically comparable between the two workflows (spec §9, §13).
SEVERITY_CLASSES = [
    ("Enhanced Regrowth", -np.inf,  -100, [0,   102,  0,   220], "#006600"),
    ("Unburned",          -100,      100, [200, 200,  200, 200], "#C8C8C8"),
    ("Low",                100,      270, [255, 255,  0,   220], "#FFFF00"),
    ("Moderate-Low",       270,      440, [255, 165,  0,   220], "#FFA500"),
    ("Moderate-High",      440,      660, [220,  50,  0,   220], "#DC3200"),
    ("High",               660, np.inf,  [153,   0,  0,   220], "#990000"),
]

_CLASS_NAMES = [c[0] for c in SEVERITY_CLASSES]
_CLASS_HEX   = [c[4] for c in SEVERITY_CLASSES]


def _classify(dnbr_val: float):
    """Return (name, index, rgba, hex) for a dNBR value (×1000 scale)."""
    for i, (name, lo, hi, rgba, hex_) in enumerate(SEVERITY_CLASSES):
        if lo <= dnbr_val < hi:
            return name, i, rgba, hex_
    return SEVERITY_CLASSES[-1][0], len(SEVERITY_CLASSES) - 1, SEVERITY_CLASSES[-1][3], SEVERITY_CLASSES[-1][4]


# ── GEE helper functions (module-level, each with own lazy `import ee`) ──────
# Defined at module level so they can be passed as callbacks to
# ee.ImageCollection.map() without triggering Python's closure-scoping trap
# (see Trap 12 in CLAUDE.md).

def _mask_clouds_pre_s2(img):
    """SCL-based aggressive cloud mask for the pre-fire Sentinel-2 composite.

    Keeps only clear-sky vegetation (4), not-vegetated (5), and water (6) pixels.
    Safe to be aggressive here — there is no burn signal to protect in the pre-fire window.
    """
    import ee
    scl = img.select("SCL")
    mask = scl.eq(4).Or(scl.eq(5)).Or(scl.eq(6))
    return img.updateMask(mask)


def _mask_clouds_post_s2(img):
    """Cloud-probability mask for the post-fire Sentinel-2 composite.

    Uses MSK_CLDPRB (s2cloudless probability, included in S2_SR_HARMONIZED) rather than SCL.
    SCL is intentionally NOT used here: Sen2Cor frequently misclassifies fresh burn scars
    as SCL class 2 (dark area) or class 3 (cloud shadow), which would erase the burn signal.
    Threshold ≤ 40% follows DE Africa burn-mapping reference implementation.
    """
    import ee
    return img.updateMask(img.select("MSK_CLDPRB").lte(40))


def _nbr_s2(img):
    """NBR from Sentinel-2 B8A (865 nm, 20 m) and B12 (2190 nm, 20 m).

    Both bands are native 20 m — no resampling needed. B8A is preferred over B8 (10 m)
    to maintain consistent 20 m resolution with SWIR bands (spec §3.1).
    normalizedDifference handles float conversion; ÷10000 cancels in the ratio.
    """
    import ee
    return img.normalizedDifference(["B8A", "B12"]).rename("NBR")


def _mirbi_s2(img):
    """MIRBI = 10×B12 − 9.8×B11 + 2 (Trigg & Flasse 2001).

    Savanna-specific index using both SWIR bands. Less affected by post-fire
    vegetation regrowth than NBR. Used as a secondary burn-confirmation signal.
    Divide by 10000 to convert DN to surface reflectance before applying coefficients.
    """
    import ee
    b11 = img.select("B11").toFloat().divide(10000)
    b12 = img.select("B12").toFloat().divide(10000)
    return b12.multiply(10).subtract(b11.multiply(9.8)).add(2).rename("MIRBI")


# ── Input / connection helper tasks ──────────────────────────────────────────

@register()
def set_aoi_group_name(
    group_name: Annotated[
        str,
        Field(
            title="AOI Group Name",
            description=(
                "Name of an EarthRanger spatial features group that defines the area to scan "
                "for burn scars (e.g. 'Reserve Boundary', 'North Block'). "
                "Find it in ER under Admin → Map Layers → Feature Groups. "
                "The burn detection will be run within this boundary."
            ),
        ),
    ],
) -> str:
    """Return the AOI group name; exists to expose a well-labelled form field."""
    return group_name


@register()
def set_overlay_group_name(
    group_name: Annotated[
        str,
        Field(
            title="Overlay Layer Group Name",
            description=(
                "Optional: name of an EarthRanger spatial features group to display as an "
                "extra layer on the map (e.g. 'Roads', 'Fencelines', 'Water Sources'). "
                "Leave blank to add no overlay."
            ),
            default="",
        ),
    ] = "",
) -> str:
    """Return the overlay group name as-is; exists to expose a labelled form field."""
    return group_name


@register()
def format_optional_name(name: str = "") -> str:
    """Return name if set, otherwise 'Not set'. Used for optional overlay widget."""
    return name if name else "Not set"


# ── Main GEE computation task ─────────────────────────────────────────────────

@register(tags=["gee", "fire"])
def compute_burn_scar_s2(
    client: _GEE,
    aoi: _GDF,
    fire_start_date: Annotated[
        str,
        Field(
            title="Fire Start Date",
            description=(
                "Start date of the fire event or scan window (YYYY-MM-DD). "
                "The pre-fire composite uses imagery before this date."
            ),
            pattern=r"^\d{4}-\d{2}-\d{2}$",
        ),
    ],
    fire_end_date: Annotated[
        str,
        Field(
            title="Fire End Date",
            description=(
                "End date of the fire event or scan window (YYYY-MM-DD). "
                "Set the same as Fire Start Date for a single-day event. "
                "The post-fire composite uses imagery after this date."
            ),
            pattern=r"^\d{4}-\d{2}-\d{2}$",
        ),
    ],
    pre_fire_days: Annotated[
        int,
        Field(
            title="Pre-Fire Window (days)",
            description=(
                "Days before Fire Start Date for the pre-fire Sentinel-2 mean composite. "
                "90–120 days captures stable vegetation state before fire season onset."
            ),
            ge=30,
            le=365,
        ),
    ] = 90,
    post_fire_days: Annotated[
        int,
        Field(
            title="Post-Fire Window (days)",
            description=(
                "Days after Fire End Date for the post-fire Sentinel-2 mean composite. "
                "14–30 days recommended — C4 grasses in the Lowveld can recover within "
                "2–4 weeks of rainfall. Use up to 60 days for dry-season fires where "
                "regrowth is slow."
            ),
            ge=7,
            le=90,
        ),
    ] = 30,
    dnbr_threshold: Annotated[
        float,
        Field(
            title="dNBR Detection Threshold",
            description=(
                "Minimum dNBR to classify a pixel as burned. Default 0.2 (200 on ×1000 scale). "
                "0.1 detects more area but increases false positives from vegetation drying. "
                "0.27–0.3 gives high-confidence detections but may miss low-severity burns."
            ),
            ge=0.05,
            le=0.5,
        ),
    ] = 0.2,
    scale: Annotated[
        int,
        Field(
            title="Analysis Scale (metres)",
            description=(
                "Pixel resolution for dNBR computation and burn scar vectorisation. "
                "Default 100 m works for reserves up to ~500,000 ha. "
                "Use 30–50 m for small AOIs (<5,000 ha) where patch detail matters. "
                "Native Sentinel-2 SWIR resolution is 20 m — only use this for very "
                "small AOIs (<500 ha) otherwise GEE memory limits will be exceeded."
            ),
            ge=10,
            le=500,
        ),
    ] = 100,
) -> _GDF:
    """
    Detect and map burn scars from Sentinel-2 imagery using dual-index spectral change detection.

    Uses COPERNICUS/S2_SR_HARMONIZED with mean compositing (Parks et al. 2018):
        NBR    = (B8A − B12) / (B8A + B12)          — both bands native 20 m
        dNBR   = (NBR_pre − NBR_post) × 1000          — positive ⇒ burn signal
        MIRBI  = 10×B12 − 9.8×B11 + 2               — Trigg & Flasse 2001
        ΔMIRBI = MIRBI_post − MIRBI_pre               — positive ⇒ burn (OPPOSITE sign to dNBR)

    Cloud masking split (spec §4 — prevents SCL from erasing fresh burn scars):
        Pre-fire:  SCL classes 4/5/6 only (aggressive is safe, no burn signal to protect)
        Post-fire: MSK_CLDPRB ≤ 40 (cloud probability — SCL class 2/3 not used post-fire)

    Confidence classification (spec §6.2):
        'confirmed': dNBR ≥ threshold AND ΔMIRBI > 0
        'probable':  dNBR ≥ threshold AND ΔMIRBI ≤ 0

    Minimum patch size: 0.5 ha via connected-components filter (spec §6.3).

    Returns a GeoDataFrame of vectorised burn patches (one row per patch):
        dNBR            — mean dNBR within patch (×1000 scale)
        DELTA_MIRBI     — mean ΔMIRBI within patch
        severity_class  — USGS label (Key & Benson 2006)
        severity_index  — class index 0…5
        fill_color      — RGBA uint8 list for map colouring
        fill_color_hex  — hex colour string for legend
        confidence      — 'confirmed' or 'probable'
        detection_mode  — 'aoi_scan' (Phase 1; 'firms_guided' added in Phase 2)
        area_ha         — patch area in hectares
        pre_image_count — S2 scenes in pre-fire composite
        post_image_count— S2 scenes in post-fire composite
    """
    import ee
    from shapely.geometry import mapping
    from shapely.geometry.polygon import orient
    from ecoscope.platform.connections import EarthEngineConnection

    if isinstance(client, str):
        EarthEngineConnection.client_from_named_connection(client)

    # Parse dates
    fire_start_dt = datetime.strptime(fire_start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    fire_end_dt   = datetime.strptime(fire_end_date,   "%Y-%m-%d").replace(tzinfo=timezone.utc)
    pre_start  = (fire_start_dt - timedelta(days=pre_fire_days)).strftime("%Y-%m-%d")
    pre_end    = fire_start_date   # filterDate is [start, end) — ends just before fire
    post_start = fire_end_date     # starts on fire end date
    post_end   = (fire_end_dt + timedelta(days=post_fire_days)).strftime("%Y-%m-%d")

    # Pre-flight: check pixel count at the requested scale won't exceed GEE memory limit.
    # GEE operations (connectedPixelCount, reduceToVectors, reduceRegions) OOM above ~500k pixels.
    aoi_utm = aoi.to_crs(aoi.estimate_utm_crs())
    area_m2 = float(aoi_utm.geometry.union_all().area)
    est_pixels = area_m2 / (scale ** 2)
    if est_pixels > 500_000:
        suggest = int((area_m2 / 250_000) ** 0.5) + 5
        raise ValueError(
            f"AOI ({area_m2 / 10_000:.0f} ha) at {scale} m scale would require "
            f"~{est_pixels:,.0f} pixels — GEE memory limit is ~500,000. "
            f"Increase 'Analysis Scale' to at least {suggest} m."
        )

    # Build GEE geometry from AOI
    aoi = aoi.set_geometry(aoi.geometry.make_valid())
    aoi_4326 = aoi.to_crs("EPSG:4326")
    union_geom = aoi_4326.geometry.unary_union
    if hasattr(union_geom, "geoms"):
        union_geom = union_geom.convex_hull
    union_geom = orient(union_geom, sign=1.0)  # CCW exterior required by GEE
    roi_geom = ee.Geometry(mapping(union_geom))

    S2 = "COPERNICUS/S2_SR_HARMONIZED"

    # Scene counts (on unfiltered collection — before cloud masking alters timestamps)
    pre_count = int(
        ee.ImageCollection(S2).filterBounds(roi_geom).filterDate(pre_start, pre_end).size().getInfo()
    )
    post_count = int(
        ee.ImageCollection(S2).filterBounds(roi_geom).filterDate(post_start, post_end).size().getInfo()
    )
    if pre_count == 0:
        raise ValueError(
            f"No Sentinel-2 imagery for pre-fire window ({pre_start} – {pre_end}). "
            "Try increasing 'Pre-Fire Window (days)' or check the AOI overlaps a S2 scene."
        )
    if post_count == 0:
        raise ValueError(
            f"No Sentinel-2 imagery for post-fire window ({post_start} – {post_end}). "
            "Try increasing 'Post-Fire Window (days)' or use a more recent fire date."
        )

    # Pre-fire mean NBR composite — aggressive SCL mask (safe, no burn to protect)
    pre_nbr = (
        ee.ImageCollection(S2)
        .filterBounds(roi_geom)
        .filterDate(pre_start, pre_end)
        .map(_mask_clouds_pre_s2)
        .map(_nbr_s2)
        .mean()
    )

    # Post-fire mean NBR composite — cloud-prob mask only (preserves burn-scar pixels)
    post_nbr = (
        ee.ImageCollection(S2)
        .filterBounds(roi_geom)
        .filterDate(post_start, post_end)
        .map(_mask_clouds_post_s2)
        .map(_nbr_s2)
        .mean()
    )

    # dNBR = (pre − post) × 1000; positive values indicate burn (Parks et al. 2018)
    dnbr = pre_nbr.subtract(post_nbr).multiply(1000).rename("dNBR")

    # MIRBI composites for secondary burn confirmation
    pre_mirbi = (
        ee.ImageCollection(S2)
        .filterBounds(roi_geom)
        .filterDate(pre_start, pre_end)
        .map(_mask_clouds_pre_s2)
        .map(_mirbi_s2)
        .mean()
    )
    post_mirbi = (
        ee.ImageCollection(S2)
        .filterBounds(roi_geom)
        .filterDate(post_start, post_end)
        .map(_mask_clouds_post_s2)
        .map(_mirbi_s2)
        .mean()
    )

    # ΔMIRBI = post − pre; positive = burn. NOTE: opposite subtraction order to dNBR.
    # If this is accidentally written as pre − post the confidence tier silently inverts.
    delta_mirbi = post_mirbi.subtract(pre_mirbi).rename("DELTA_MIRBI")

    # Minimum patch size: 0.5 ha connected-component filter (Roteta et al. 2019, spec §6.3)
    pixel_area_m2 = scale ** 2
    min_pixels = max(1, int(np.ceil(5000.0 / pixel_area_m2)))  # 0.5 ha = 5000 m²

    # Burn detection: dNBR ≥ threshold, remove speckle via connected-component size
    threshold_scaled = dnbr_threshold * 1000
    burned_pixels = dnbr.gte(threshold_scaled).selfMask()
    connected = burned_pixels.connectedPixelCount(maxSize=1000, eightConnected=True)
    patch_mask = connected.gte(min_pixels)

    # Vectorise burn patches (binary mask → polygon features).
    # bestEffort=True lets GEE coarsen scale automatically if memory limit is hit.
    burn_polys = (
        patch_mask
        .selfMask()
        .reduceToVectors(
            geometry=roi_geom,
            scale=scale,
            geometryType="polygon",
            eightConnected=True,
            maxPixels=int(1e8),
            bestEffort=True,
        )
    )

    # Per-polygon mean dNBR and ΔMIRBI.
    # tileScale=4 divides the computation into smaller tiles to stay within the
    # GEE per-request memory limit (prevents "User memory limit exceeded" on large AOIs).
    data_img = dnbr.rename("dNBR").addBands(delta_mirbi.rename("DELTA_MIRBI"))
    stats = data_img.reduceRegions(
        collection=burn_polys,
        reducer=ee.Reducer.mean(),
        scale=scale,
        tileScale=4,
    )

    features = stats.getInfo()["features"]

    _empty_cols = [
        "geometry", "dNBR", "DELTA_MIRBI", "severity_class", "severity_index",
        "fill_color", "fill_color_hex", "confidence", "detection_mode",
        "area_ha", "pre_image_count", "post_image_count",
    ]
    if not features:
        return gpd.GeoDataFrame(columns=_empty_cols, crs="EPSG:4326")

    gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")

    # Classify each patch by its mean dNBR
    classified = gdf["dNBR"].apply(_classify)
    gdf["severity_class"] = [c[0] for c in classified]
    gdf["severity_index"] = [c[1] for c in classified]
    gdf["fill_color"]     = [c[2] for c in classified]
    gdf["fill_color_hex"] = [c[3] for c in classified]

    # Confidence: confirmed if ΔMIRBI also indicates burn (Bastarrika et al. 2024)
    gdf["confidence"] = gdf["DELTA_MIRBI"].apply(lambda v: "confirmed" if v > 0 else "probable")

    # Patch area
    gdf_utm = gdf.to_crs(gdf.estimate_utm_crs())
    gdf["area_ha"] = gdf_utm.geometry.area / 10_000.0

    # Observability metadata
    gdf["detection_mode"]    = "aoi_scan"
    gdf["pre_image_count"]   = pre_count
    gdf["post_image_count"]  = post_count

    return gdf


# ── Layer / visualisation tasks ───────────────────────────────────────────────

@register(tags=["fire"])
def create_burn_scar_layer(
    geodataframe: _GDF,
    opacity: Annotated[
        float,
        Field(
            title="Layer Opacity",
            description="Transparency of the burn scar layer (0 = transparent, 1 = fully opaque).",
            ge=0.0,
            le=1.0,
        ),
    ] = 0.85,
) -> Any:
    """
    Build a lonboard polygon LayerDefinition for burn scar severity visualisation.

    Colours each patch by severity class using the USGS standard palette.
    Tooltip shows area, mean dNBR, severity class, and MIRBI confidence tier.
    """
    from ecoscope.platform.tasks.results._ecomap import (
        LayerDefinition,
        LegendDefinition,
        PolygonLayerStyle,
    )

    style = PolygonLayerStyle(
        filled=True,
        stroked=True,
        fill_color_column="fill_color",
        get_line_color="#333333",
        get_line_width=1,
        line_width_units="pixels",
        opacity=opacity,
    )

    legend = LegendDefinition(
        labels=_CLASS_NAMES,
        colors=_CLASS_HEX,
    )

    return LayerDefinition(
        geodataframe=geodataframe,
        layer_style=style,
        legend=legend,
        tooltip_columns=["area_ha", "dNBR", "severity_class", "confidence"],
        zoom=True,
    )


@register(tags=["fire"])
def combine_burn_scar_layers(
    burn_scar_layer: Any,
    aoi_layer: Any,
    overlay_layer: Any = None,
) -> Any:
    """Combine burn scar layer + AOI boundary outline + optional user overlay for draw_ecomap.

    aoi_layer is always shown (AOI boundary outline).
    overlay_layer is optional (fencelines, roads, etc.) — pass SkipSentinel or None to omit.
    Handles SkipSentinel internally so the map renders even when overlay is blank.
    """
    from wt_task.skip import SkipSentinel

    if isinstance(burn_scar_layer, SkipSentinel):
        return burn_scar_layer
    layers = [burn_scar_layer]
    if not isinstance(aoi_layer, SkipSentinel) and aoi_layer is not None:
        if isinstance(aoi_layer, list):
            layers.extend(aoi_layer)
        else:
            layers.append(aoi_layer)
    if not isinstance(overlay_layer, SkipSentinel) and overlay_layer is not None:
        if isinstance(overlay_layer, list):
            layers.extend(overlay_layer)
        else:
            layers.append(overlay_layer)
    return layers


@register(tags=["fire", "overlay"])
def create_styled_overlay_layer(
    geodataframe: _GDF,
) -> Any:
    """
    Overlay layer for ER spatial features (AOI boundary, roads, fencelines, etc.).

    Splits by geometry type so lonboard never receives mixed types:
        LineString/MultiLineString → PolylineLayerStyle
        Polygon/MultiPolygon       → PolygonLayerStyle (outline only)
        Point/MultiPoint           → PointLayerStyle
    """
    from ecoscope.platform.tasks.results._ecomap import (
        LayerDefinition,
        PointLayerStyle,
        PolygonLayerStyle,
        PolylineLayerStyle,
    )

    gdf = geodataframe.copy()
    geom_col = gdf.geometry.geom_type
    color = "#FF8C00"
    width = 2.0
    layers = []

    line_gdf = gdf[geom_col.isin({"LineString", "MultiLineString"})].copy()
    if not line_gdf.empty:
        layers.append(LayerDefinition(
            geodataframe=line_gdf,
            layer_style=PolylineLayerStyle(
                get_color=color,
                get_width=width,
                width_units="pixels",
                cap_rounded=True,
            ),
            legend=None,
            tooltip_columns=[],
        ))

    polygon_gdf = gdf[geom_col.isin({"Polygon", "MultiPolygon"})].copy()
    if not polygon_gdf.empty:
        layers.append(LayerDefinition(
            geodataframe=polygon_gdf,
            layer_style=PolygonLayerStyle(
                filled=False,
                stroked=True,
                get_line_color=color,
                get_line_width=width,
                line_width_units="pixels",
            ),
            legend=None,
            tooltip_columns=[],
        ))

    point_gdf = gdf[geom_col.isin({"Point", "MultiPoint"})].copy()
    if not point_gdf.empty:
        layers.append(LayerDefinition(
            geodataframe=point_gdf,
            layer_style=PointLayerStyle(
                get_fill_color=color,
                get_radius=5,
                radius_units="pixels",
            ),
            legend=None,
            tooltip_columns=[],
        ))

    return layers


# ── Stat tasks ────────────────────────────────────────────────────────────────

@register(tags=["fire", "stats"])
def count_burned_area_ha(geodataframe: _GDF) -> float:
    """Total area of burn patches classified Low severity or higher (severity_index ≥ 2)."""
    burned = geodataframe[geodataframe["severity_index"] >= 2]
    if burned.empty:
        return 0.0
    return float(burned.to_crs(burned.estimate_utm_crs()).geometry.area.sum()) / 10_000.0


@register(tags=["fire", "stats"])
def count_high_severity_area_ha(geodataframe: _GDF) -> float:
    """Total area of burn patches classified Moderate-High or High (severity_index ≥ 4)."""
    high = geodataframe[geodataframe["severity_index"] >= 4]
    if high.empty:
        return 0.0
    return float(high.to_crs(high.estimate_utm_crs()).geometry.area.sum()) / 10_000.0


@register(tags=["fire", "stats"])
def count_burn_patches(geodataframe: _GDF) -> int:
    """Number of distinct burn patches detected above the dNBR threshold."""
    return len(geodataframe)


@register(tags=["fire", "stats"])
def get_detection_mode(geodataframe: _GDF) -> str:
    """Extract detection mode label from result GeoDataFrame.

    Returns 'FIRMS Guided' or 'AOI Scan'. Phase 1 always returns 'AOI Scan';
    Phase 2 will set 'firms_guided' in the detection_mode column when FIRMS events are used.
    """
    if geodataframe.empty or "detection_mode" not in geodataframe.columns:
        return "AOI Scan"
    mode = geodataframe["detection_mode"].iloc[0]
    return "FIRMS Guided" if mode == "firms_guided" else "AOI Scan"


@register(tags=["fire", "stats"])
def format_patch_count(
    count: Annotated[int, Field(description="Number of burn patches.")],
) -> str:
    """Format patch count for dashboard display."""
    return f"{count} patch{'es' if count != 1 else ''}"


@register(tags=["fire", "stats"])
def format_area_ha(
    area_ha: Annotated[float, Field(description="Area in hectares to format for display.")],
) -> str:
    """Format an area value as a human-readable string (m², ha, or km²)."""
    if area_ha >= 10_000:
        return f"{area_ha / 10_000:.1f} km²"
    elif area_ha >= 1:
        return f"{int(round(area_ha)):,} ha"
    else:
        return f"{int(round(area_ha * 10_000)):,} m²"


@register(tags=["fire", "stats"])
def count_pre_images(geodataframe: _GDF) -> int:
    """Sentinel-2 scenes used in the pre-fire mean composite."""
    if geodataframe.empty or "pre_image_count" not in geodataframe.columns:
        return 0
    return int(geodataframe["pre_image_count"].iloc[0])


@register(tags=["fire", "stats"])
def count_post_images(geodataframe: _GDF) -> int:
    """Sentinel-2 scenes used in the post-fire mean composite."""
    if geodataframe.empty or "post_image_count" not in geodataframe.columns:
        return 0
    return int(geodataframe["post_image_count"].iloc[0])


@register(tags=["fire", "stats"])
def format_image_count(
    count: Annotated[int, Field(description="Number of Sentinel-2 scenes.")],
) -> str:
    """Format a scene count as 'N scene(s)' for dashboard display."""
    return f"{count} scene{'s' if count != 1 else ''}"
