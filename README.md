# Burn Scar Mapping

An [Ecoscope Desktop](https://github.com/wildlife-dynamics/ecoscope) workflow that automatically detects and maps fire burn scars within a reserve or area of interest using free satellite imagery, without requiring a pre-existing fire report.

---

## What it does

After a fire, the workflow analyses satellite images taken before and after the event and highlights areas where vegetation was burned. It produces a colour-coded map showing how severely different parts of the landscape were affected, alongside summary statistics such as total area burned and the number of distinct burn patches detected.

---

## How it works

### Satellite data

The workflow uses imagery from **Sentinel-2**, a pair of satellites operated by the European Space Agency that revisit any point on Earth every 2–5 days at 10–20 metre resolution. Specifically, it uses the **short-wave infrared (SWIR)** and **near-infrared (NIR)** bands, which are highly sensitive to the charred vegetation and bare soil left after a fire — changes that are often invisible in standard colour photography.

### Before-and-after comparison

The workflow builds two cloud-free composite images by averaging all available satellite scenes:

- **Pre-fire composite** — imagery from the weeks or months *before* the fire, representing healthy vegetation
- **Post-fire composite** — imagery from the weeks *after* the fire, representing burned or recovering vegetation

### Burn detection indices

Two spectral indices are calculated to detect burn scars:

**dNBR (differenced Normalised Burn Ratio)**
The NBR index uses the ratio of NIR to SWIR reflectance. Healthy vegetation has a high NBR; burned areas have a sharply lower NBR. Subtracting the post-fire NBR from the pre-fire NBR gives the dNBR — the larger the value, the more severely burned the area (Key & Benson 2006).

**ΔMIRBI (change in Mid-Infrared Burn Index)**
The MIRBI index (Trigg & Flasse 2001) was specifically designed for African savannas and is particularly sensitive to the bare soil and ash exposed after fire. A positive ΔMIRBI (post-fire minus pre-fire) independently confirms a burn signal. Patches where both dNBR and ΔMIRBI indicate burning are classified as **Confirmed**; patches detected by dNBR alone are classified as **Probable**.

### Minimum patch size

To remove noise and isolated misclassified pixels, only contiguous burn areas of at least **0.5 hectares** (a connected group of pixels) are retained. Smaller isolated patches are discarded.

### Severity classification

Each burn patch is assigned a severity class based on its dNBR value, following the standard USGS thresholds (Key & Benson 2006):

| Severity Class | dNBR Range | Interpretation |
|---|---|---|
| Unburned | < 0.10 | No detectable change |
| Low | 0.10 – 0.26 | Surface fire; most canopy intact |
| Moderate–Low | 0.27 – 0.43 | Partial canopy scorch or loss |
| Moderate–High | 0.44 – 0.65 | Significant canopy loss |
| High | > 0.66 | Near-complete canopy/vegetation loss |

---

## Dashboard outputs

| Stat Card | What it shows |
|---|---|
| **Burned** | Total area classified as Low severity or above |
| **High Sev** | Area classified as Moderate–High or High severity |
| **% Burned** | Burned area as a percentage of the total AOI |
| **Patches** | Number of distinct, spatially separate burn patches |
| **Mode** | Detection mode used (AOI Scan in Phase 1) |
| **Pre Imgs** | Number of Sentinel-2 scenes in the pre-fire composite |
| **Post Imgs** | Number of Sentinel-2 scenes in the post-fire composite |
| **dNBR Threshold** | The detection threshold applied in this run |
| **Fire Window** | The fire start and end dates entered by the user |
| **Map** | Burn patches colour-coded by severity, overlaid on satellite imagery |

The **Files** tab provides a downloadable GeoJSON file of all burn patches including their area, severity class, mean dNBR, ΔMIRBI value, and confidence tier.

---

## How to use

### Requirements

- An [Ecoscope Desktop](https://github.com/wildlife-dynamics/ecoscope) account
- An EarthRanger connection with a spatial feature group defining the area of interest
- A Google Earth Engine (GEE) connection

### Parameters

| Parameter | Description | Default |
|---|---|---|
| **AOI Name** | Spatial feature group from EarthRanger defining the area to scan | — |
| **Fire Start Date** | The date the fire began (or the start of the period to scan) | — |
| **Fire End Date** | The date the fire ended (or the end of the period to scan) | — |
| **Pre-Fire Window (days)** | How many days before the fire start date to use for the pre-fire composite. 90 days is recommended; increase to 120–180 days for fires late in the dry season to keep the baseline anchored in the wet season | 90 |
| **Post-Fire Window (days)** | How many days after the fire end date to use for the post-fire composite. 14–30 days is typical; increase to 60 days for dry-season fires where recovery is slow | 30 |
| **dNBR Detection Threshold** | Minimum dNBR for a pixel to be classified as burned. Lower values detect more area but increase false positives from vegetation drying. 0.20 is appropriate for most applications | 0.20 |
| **Analysis Scale (metres)** | Pixel resolution for the computation. 100 m is the default and works for reserves up to ~500,000 ha. Use 30–50 m for small AOIs (< 5,000 ha) where patch detail matters | 100 |

### Tips for a full fire season

Analysing an entire fire season (e.g. May–October) in a single run can exceed satellite data processing limits. Instead, run the workflow once per month and adjust `Pre-Fire Window` to keep the baseline in the wet season:

| Month | Fire Start | Fire End | Pre-Fire Window |
|---|---|---|---|
| May | YYYY-05-01 | YYYY-05-31 | 90 days |
| June | YYYY-06-01 | YYYY-06-30 | 90 days |
| July | YYYY-07-01 | YYYY-07-31 | 90 days |
| August | YYYY-08-01 | YYYY-08-31 | 120 days |
| September | YYYY-09-01 | YYYY-09-30 | 150 days |
| October | YYYY-10-01 | YYYY-10-31 | 180 days |

### Cloud masking

Clouds can obscure burn scars, so the workflow applies different cloud-masking strategies for the two composites:

- **Pre-fire**: Uses the Sentinel-2 Scene Classification Layer (SCL) to include only clear vegetation and soil pixels. This is safe because there are no burn scars to protect in the pre-fire image.
- **Post-fire**: Uses a cloud probability mask (≤ 40% cloud probability) rather than the SCL. This is important because fresh burn scars can be misclassified as cloud shadow by the SCL, causing them to be incorrectly removed from the composite.

---

## Limitations

- **Cloud cover**: If a fire occurred during a prolonged cloudy period, there may be insufficient cloud-free imagery to produce a reliable composite. The Post Imgs stat card shows how many scenes were available.
- **Seasonal vegetation change**: The dNBR compares vegetation state before and after the fire. In areas with strong seasonal variation (e.g. wet vs. dry season), some signal may come from normal browning rather than fire. Using a pre-fire baseline anchored in the wet season minimises this.
- **Resolution**: At the default 100 m analysis scale, features smaller than ~1 ha may not be resolved accurately. Reduce the scale for finer detail, noting that smaller scales require more processing time and may fail for large areas.
- **Savanna vs. forest**: Severity thresholds calibrated for temperate forests (Key & Benson 2006) may underestimate severity in African savannas where fire behaviour differs. The MIRBI confidence tier provides a savanna-specific second opinion.

---

## References

Key, C.H. & Benson, N.C. (2006). Landscape Assessment (LA): Sampling and Analysis Methods. In: Lutes, D.C. et al. (eds.) *FIREMON: Fire Effects Monitoring and Inventory System*. USDA Forest Service, Rocky Mountain Research Station, General Technical Report RMRS-GTR-164-CD.

Trigg, S. & Flasse, S. (2001). An evaluation of different bi-spectral spaces for discriminating burned shrub-savanna. *International Journal of Remote Sensing*, 22(13), 2641–2647.

Parks, S.A., Holsinger, L.M., Voss, M.A., Loehman, R.A. & Robinson, N.J. (2018). Mean composite fire severity metrics computed with Google Earth Engine offer improved accuracy and expanded mapping potential. *Remote Sensing*, 10(6), 879.

European Space Agency (2022). *Sentinel-2 MSI: MultiSpectral Instrument, Level-2A*. Available via Google Earth Engine: `COPERNICUS/S2_SR_HARMONIZED`.

---

## Workflow development

Built on the [Ecoscope Platform](https://github.com/wildlife-dynamics/ecoscope) using Google Earth Engine for satellite data processing. Source code and version history: [github.com/cllrssml/burn-scar-mapping](https://github.com/cllrssml/burn-scar-mapping).
