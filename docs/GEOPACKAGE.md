# Acalc model GeoPackage — schema 15

This is the model authoring contract. It describes the file written by the
current Acalc plugin. Engines must not
infer evaluation semantics from QGIS styling, visibility, feature order, or FIDs.

## Container and coordinates

- SQLite GeoPackage with application ID `0x47504B47` and normal `gpkg_*` tables.
- Exactly one `acalc_project` row; `schema_version = 15`. Readers reject unsupported
  schema versions and unknown feature types. Schemas 1–5 upgrade as described below.
- All feature tables use the same projected CRS, recorded as `crs_wkt` and, when
  available, `crs_authid`. Its SRS is also registered by normal GeoPackage geometry
  metadata. XY uses the CRS's linear unit (`horizontal_unit`); multiplying by
  `horizontal_to_m` converts it to metres. The plugin derives that positive finite
  factor from the authoritative WKT and verifies it on opening a schema 15 model.
  Geographic / longitude-latitude CRS is not accepted.
- Schema 3 writers serialize `acalc_project.crs_wkt` using GDAL/OSR
  `SpatialReference.ExportToWkt()` (canonical WKT1), consistently with each
  feature layer's `gpkg_spatial_ref_sys.definition`. For an EPSG-identified CRS,
  the plugin first verifies full semantic equivalence against the local EPSG
  registry, then exports that canonical definition; this preserves nested
  identifiers that a WKT2 input may omit. Custom definitions are exported directly.
  Creation and migration
  normalize the project WKT without changing its CRS meaning. The engine requires
  these definitions to match, allowing whitespace differences outside quoted
  strings; a differently serialized or inconsistent external definition must be
  corrected before calculation. An authority code alone is not proof of a match.
- **Every Z is ground elevation in metres** relative to `vertical_datum`.
  `Unspecified` means the user has not declared the datum; no transformation to a
  named vertical datum is implied.
- **Every M is height above that ground in metres**, non-negative. Absolute source,
  receiver, barrier-top, or building-top elevation at a vertex is `Z + M`.
- Ground absorption areas and Trees store XY only, with neither Z nor M. Other geometry
  stores XY and explicit Z/M ordinates. Inferred Z and shared building
  heights are resolved from the object options at calculation time; saved ordinates
  are placeholders when those options override them.
  Area receiver perimeters retain Z/M for file compatibility only; those ordinates
  are ignored, and `height_m` sets every generated receiver's terrain clearance.
- Missing, NaN, and infinite calculation coordinates are invalid; zero is a real coordinate.
- Single Point, LineString and Polygon only. No curves, collections, or multipart
  geometries. A line must have at least two distinct XY positions. Polygon perimeters
  must be closed and topologically valid, with positive area. Closing coordinates
  repeat all stored ordinates. New polygon objects have one perimeter, without interior boundaries. Legacy interiors survive migration unchanged.

## Identity and time

Project, layer, spectrum and variant identities are UUID strings, generated as UUIDv7
by this plugin. Feature `object_id` is also a UUID: coordinate entry generates
UUIDv7; QGIS's normal `uuid()` field default can generate UUIDv4. Readers accept
valid UUIDs, including QGIS's brace-wrapped text representation, and compare their
normalized values. Feature identity must be unique within its layer. Together,
`project_id`, `layer_id`, and `object_id` identify an object in a model.

GeoPackage integer `fid` values are local storage keys, not portable object
identifiers. Timestamp fields are UTC ISO 8601 text with millisecond precision.
Project modification timestamps are updated for plugin changes. SQLite triggers
also track feature, layer, spectrum and variant changes made by external
writers. Timestamps advance by at least one millisecond for each change, including
multiple changes in one clock tick, so rapid edits cannot leave results marked
current. During bulk edits this modification marker can run ahead of wall time.

## Metadata tables

These are registered as `attributes` in `gpkg_contents` so that standard tools can
discover them. They are not loaded into the model's QGIS layer group.

### `acalc_project`

| Column | Type / meaning |
| --- | --- |
| `singleton` | INTEGER PRIMARY KEY, exactly 1 |
| `schema_version` | INTEGER NOT NULL, 12 |
| `project_id` | TEXT NOT NULL UNIQUE, UUID |
| `name` | TEXT NOT NULL, nonblank display name |
| `description` | TEXT NOT NULL, free text |
| `author` | TEXT NOT NULL, free text |
| `crs_wkt` | TEXT NOT NULL, authoritative horizontal projected CRS |
| `crs_authid` | TEXT NOT NULL, e.g. `EPSG:26913`; may be empty for custom CRS |
| `horizontal_unit` | TEXT NOT NULL, CRS linear unit name |
| `horizontal_to_m` | REAL NOT NULL, positive conversion from CRS XY units to metres |
| `vertical_unit` | TEXT NOT NULL, always `m` |
| `vertical_datum` | TEXT NOT NULL, user-declared datum / `Unspecified` |
| `created_at`, `modified_at` | TEXT NOT NULL, UTC timestamps |
| `metadata_json` | TEXT NOT NULL, arbitrary JSON object; default `{}` |

The project metadata may contain the manually entered optional boolean
`keep_integration_reports`. To enable it, follow the plugin's
[full integration report instructions](USER_GUIDE.md#full-integration-reports-optional)
for **Project → Project details → Other metadata (JSON)** and click
**Save project details** before running. Only JSON `true` requests full extended-source
integration reports; absent, `false`, or any other value leaves the request field
omitted. The plugin never inserts, defaults, or pre-fills this key. It is intended
for compliance evidence or debugging on a specific run: result size and memory
use grow with the number of integrations. Remove it or set it to `false` afterward.
When enabled, the API adapter requires the `integration_reports` feature in the
engine's fresh `/v1/capabilities` response and adds only
`"integration_reports": "full"` to the normalized request's `output` object,
preserving any existing output options. An unsupported engine causes the run to
fail before submission. The completed run's saved request retains this choice.

### `acalc_layers`

| Column | Type / meaning |
| --- | --- |
| `layer_id` | TEXT PRIMARY KEY, UUID |
| `table_name` | TEXT NOT NULL UNIQUE, `model_` followed by 32 lowercase hex characters |
| `name` | TEXT NOT NULL, nonblank display name; not a SQL identifier |
| `feature_type` | TEXT NOT NULL, one of the keys below |
| `sort_order` | INTEGER NOT NULL, display order |
| `archived` | INTEGER NOT NULL, 0 or 1 |
| `created_at` | TEXT NOT NULL, UTC timestamp |

Only rows with `archived = 0` belong to the active model. Archiving is transactional
with removal of that layer ID from all variants. Its feature table remains
readable. Restoring sets `archived = 0`; it does not recreate membership. Renaming
only changes the display name, preserving layer IDs and variant references.

| `feature_type` | `gpkg_geometry_columns.geometry_type_name` | `z` | `m` |
| --- | --- | --- | --- |
| `point_source` | POINT | 1 | 1 |
| `line_source` | LINESTRING | 1 | 1 |
| `area_source` | POLYGON | 1 | 1 |
| `source_building` | POLYGON | 1 | 1 |
| `ground_absorption` | POLYGON | 0 | 0 |
| `trees` | POLYGON | 0 | 0 |
| `point_receiver` | POINT | 1 | 1 |
| `measurement` | POINT | 1 | 1 |
| `area_receiver` | POLYGON | 1 | 1 |
| `barrier` | LINESTRING | 1 | 1 |
| `building` | POLYGON | 1 | 1 |
| `terrain_point` | POINT | 1 | 0 |
| `terrain_line` | LINESTRING | 1 | 0 |

The geometry column is named `geom`; Z and M are mandatory where marked 1.
Feature tables have a normal GeoPackage spatial index.

### `acalc_variants`

| Column | Type / meaning |
| --- | --- |
| `variant_id` | TEXT PRIMARY KEY, UUID |
| `name` | TEXT NOT NULL UNIQUE COLLATE NOCASE, nonblank display name |
| `definition_json` | TEXT NOT NULL, variant definition below |
| `created_at`, `modified_at` | TEXT NOT NULL, UTC timestamps |

There is always at least one variant. A new project contains `Default` with
no layers. Each variant is evaluated independently; it includes exactly its
listed active layers. Layer IDs cannot repeat or refer to missing/archived layers.
Newly created layers are not implicitly added to variants.

```json
{
  "schema_version": 12,
  "layer_ids": ["019…"]
}
```

Variants contain object-layer membership only; other definition keys are rejected.
Their included object layers provide both acoustic objects and terrain ground
points. There is no independent terrain selection or layer priority.

### Elevation raster sources and generated terrain

The legacy `acalc_elevation_rasters` table contains `raster_id` (TEXT primary key), `name` (nonblank
TEXT), `path` (absolute source filename), and `band` (positive INTEGER, default 1).
It stores references only: no density, samples, source fingerprints, groups or
surface caches. Adding/removing a reference does not change the calculation
revision. The engine does not read this table or its external files. Missing files
do not prevent opening, validation or calculation; only sampling needs access.

The Generated terrain page lists saved terrain layers, not raster references.
Generation selects a file-backed GDAL raster from disk or QGIS for that import
only, using band 1. It does not add a source reference or load the raster into
QGIS. Existing reference rows and their band selections remain readable for
compatibility. Raster authoring choosers also accept files directly.

The optional `acalc_raster_terrain` attributes table is created on the first
successful generation. It stores `layer_id` (TEXT primary key, referencing
`acalc_layers`), nullable `raster_id` (referencing `acalc_elevation_rasters`,
`ON DELETE SET NULL`), and `generation_json` (TEXT). New imports leave `raster_id`
NULL; this column is retained only for older imports. The list joins generations
to active layers by `layer_id`, independently of source references. Each generation creates a new
`terrain_line` layer for triangle-edge storage and engine transport. The provenance
record identifies it as a generated mesh in the UI: it is excluded from the Lines
editor, listed in the Generated terrain layer selector, and grouped separately in
variant membership. No storage-type migration is required for existing meshes.
Layer registration and provenance are committed
in one transaction after all geometry is written; failure removes the new table.
No variant membership changes automatically. Removed sources do not remove saved
terrain or provenance, and archived layers retain their generation record.

`generation_json` records algorithm `adaptive-geos-tin-v1`, source path/band,
source raster CRS/geotransform/unit/scale/offset, model CRS, bounds
`[xmin,ymin,xmax,ymax]` in model coordinates, `tolerance_m`, `max_error_m`, candidate,
vertex and triangle counts, and `source_sample_sha256`. That fingerprint hashes
the ordered sampled XYZ values as little-endian float64, before deduplication;
it identifies the data used, not the entire external raster file. The report is
historical and does not claim accuracy after manual geometry edits or reprojection.

Generation uses native valid pixel centres within the box plus bilinear perimeter
samples, rejects missing-data coverage, and refines a GEOS Delaunay surface by
inserting the worst residuals (up to 128 per iteration). The maximum checked
sample error must meet tolerance before saving. Limits of 1,000,000 candidates
and 20,000 selected vertices fail explicitly instead of silently thinning data.
The box perimeter is sampled at approximately half-pixel spacing; the error
bound applies to checked samples, not the continuous raster interpolant or sound
levels. All triangle edges are retained as breakline constraints, encoded as
connected mesh walks in LineStringZ features of at most 1,000 vertices each.
Walks may retrace edges; the engine deduplicates constraints. This preserves the
checked topology even if the solver uses different cocircular Delaunay tie rules.
Additional included terrain observations still follow the normal conflict and
interpolation rules. The engine does not read the provenance table. Outside-box
terrain follows the existing nearest-hull extension, not a new coverage mask.

Sampling transforms object X/Y from the model CRS to raster CRS, inverts the affine
geotransform (including rotation), and uses bilinear interpolation between native
pixel centres. Queries in the outer half-pixel clamp to the nearest centre. Queries
outside the raster extent fail. GDAL masks, NoData and nonfinite cells are omitted;
remaining positive weights are renormalized. No valid weighted neighbours is an
error, not a zero or extrapolation across a hole. Band scale/offset is applied,
then declared vertical units convert to metres (unspecified units mean metres).
There is no vertical datum transformation. Only the needed pixel windows are read.

Every point must sample successfully before any Z is changed. The action selects
specified per-point ground (`ground_mode = per_point`) and changes
only ground coordinates. Heights remain relative heights or fixed roof elevations
according to the existing building height mode. Apply, Cancel and undo have their
usual edit-buffer behavior. Once saved, Z has no live link to the reference.

### `acalc_calculation`

One row holds calculation parameters shared by every variant. The Calculation
page edits these parameters and selects the variant to evaluate.

| Column | Type / meaning |
| --- | --- |
| `singleton` | INTEGER PRIMARY KEY, exactly 1 |
| `engine` | TEXT, `iso9613-2:2024`; null only when migrated settings need review |
| `settings_json` | TEXT NOT NULL, complete validated settings object below |
| `legacy_settings_json` | TEXT NOT NULL, array of previous parameter sets when migration found differing settings; entries have `variant_id`, `name`, and `settings` |

Calculation settings version 2 is the native calculation API format. Version 1
settings and retired engine fields are not accepted; no runtime legacy
parameter conversion is performed.

New models start with these defaults:

```json
{
  "schema_version": 2,
  "temperature_c": 10.0,
  "relative_humidity_percent": 70.0,
  "pressure_kpa": 101.325,
  "ground_factor": 0.0,
  "meteorology": "downwind",
  "ground_method": "general",
  "c0_db": 0.0,
  "reflection_order": 3,
  "building_reflection_coefficient": 0.9,
  "barrier_reflection_coefficient": 0.9,
  "minimum_contribution_db": null,
  "pruning_values": {
    "minimum_contribution_db": 0.0
  },
  "area_raster_factor": 0.5,
  "area_max_extent_m": null,
  "area_relative_tolerance": 0.001,
  "area_absolute_tolerance": 1e-12,
  "area_max_depth": 16
}
```

All keys except the optional editor memory `pruning_values` are required;
unknown keys are rejected. Numbers must be finite JSON
numbers, not booleans. Reflection order is an integer 0–5 and must fit the
selected API's capability. Temperature is −20 to 50 °C, humidity 10–100%, pressure
50–120 kPa, default G and reflection coefficients 0–1, and C₀ 0–5 dB.
`meteorology` is `downwind` or `long_term`; `ground_method` is `general` or
`mean_path_height`. The selected ground method is retained on irregular terrain;
applicability qualifications are recorded in the engine documentation/results.
The UI displays absorption α while storing energy reflection coefficient 1−α.

The pruning and integration fields map directly to the [API settings](API.md#settings).
Explicit null disables a pruning control; an absolute cutoff of numeric zero
means 0 dB. The retired `receiver_margin_db` and its editor memory are discarded
when loading old projects and removed on their next save. Unknown solver bounds
retain work. Integration
limits never authorize truncating unresolved work. These values live in the
model, not in a named API connection, so switching engines preserves parameters.
Each pruning checkbox enables the displayed value; unchecked controls retain
their values in `pruning_values` while the corresponding solver field is null.
This editor-only dictionary is never sent to the APIs. When no saved value exists,
the editor offers a 0 dB absolute cutoff. This is an application preset, not an
ISO-prescribed tolerance. An enabled value takes precedence over editor memory.
Parameter changes advance the project revision and make saved results stale.
Completed runs retain the submitted settings, exact API request and result.

The implemented propagation scope is point, line and area sources over a
terrain surface with mixed acoustic ground properties, using ISO 9613-2:2024
octave ground attenuation with the 2024 Kgeo term, atmospheric absorption from
ISO 9613-1, and terrain/opaque building/barrier shielding. The variant's included object layers
supply ground elevations and terrain profiles. Terrain,
building roofs and barrier tops share one top diffraction path. Terrain contributions
exclude lateral diffraction; otherwise eligible obstacle side paths must clear terrain.
Applying the general ground and long-term meteorological equations to nonplanar
terrain remains an implementation extension pending independent verification.
Buildings in the selected variant are solid obstacles, with a base vertex
at `(X,Y,Z)` and a top vertex at `(X,Y,Z+M)`. Z always denotes ground, so sound
cannot pass underneath a building; its footprint is occupied below the roof.
The engine derives wall and roof edges
for screening after explicit coordinate validation and shared roof resolution. Simple
concave footprints are supported. Roof interiors use a deterministic
triangulation independent of perimeter start vertex and winding; arbitrary
interior ridges cannot be inferred from the boundary. Every resolved height must
be positive. Holes, self-intersections and self-touching perimeters are rejected.
Sources and receivers inside solid interiors are rejected. Other unsupported
geometry/path configurations fail readiness validation; see the engine's
documented geometry limits.
Barriers use their resolved `LineStringZM` vertices as zero-thickness,
ground-anchored screens. Each segment linearly interpolates ground Z and top Z+M;
no footprint or thickness is manufactured. Heights must be strictly positive and
the polyline must be simple and open, without self-crossings, self-touching or
retracing. Sound cannot pass beneath the authored ground Z. Barrier face/edge
contact is permitted; emitting lines and areas that pass through the screen are
rejected. End-to-end objects with matching ground and top elevations are joined
for screening and wavelength eligibility; their combined chain must remain
simple and open. Elevation discontinuities at joined endpoints are rejected.
Complex branching networks remain unverified. Joining does not change saved
objects or the reported authored `barrier_count`.
Buildings and barriers are assumed suitable for opaque screening;
surface mass and openings are not represented. Planar facade/wall reflections
use the settings above and optional object coefficients. Transmission through
buildings or barriers is not implemented.
This scoped engine is not a complete implementation of every ISO 9613-2:2024
propagation case.

Line and area spectra accept total power or the matching power density. Total
power is distributed uniformly over acoustic length or emitting area. Density
levels gain `10 log10(length_m)` or `10 log10(area_m2)` before integration. Integration subdivides each line adaptively so each
segment satisfies both the configured length limit and the distance criterion
(segment length no greater than half its distance to the receiver). Long-term
line calculations require a constant source height and apply the correction
using the line's centre. Unknown emission bands, invalid geometry and unsupported
conditions fail validation before any result is stored.

### `acalc_spectra`

Nonspatial OGR table with integer `fid`, unique `spectrum_id` TEXT, `name` TEXT
and `spectrum_json` TEXT. Existing SQL columns remain nullable for compatibility
with the original container, but schema 2 and 3 readers/writers require a canonical
UUID spectrum ID, a nonblank name, and a valid typed JSON definition for every
record. Names are case-insensitively unique within each kind. Names may repeat
across the sound power and absorption catalogs. New projects have empty catalogs.

Both kinds use eight octave bands, in order: **63, 125, 250, 500, 1000, 2000,
4000, 8000 Hz**. The nested spectrum definition has its own version, currently 2:

```json
{
  "schema_version": 2,
  "kind": "sound_power",
  "bandwidth": "octave",
  "frequencies_hz": [63, 125, 250, 500, 1000, 2000, 4000, 8000],
  "quantity": "sound_power_level",
  "unit": "dB re 1 pW",
  "weighting": "Z",
  "power_basis": "total",
  "back_calculation": "locked",
  "values": [0, -5, 0, null, 75, 70, 60, 50],
  "provenance": "Synthetic example only"
}
```

- Sound power uses `quantity = "sound_power_level"`, `unit = "dB re 1 pW"`
  and Z weighting. **Numeric 0 is reserved for no emitted energy**, by the model's
  entry convention; negative nonzero dB levels are valid. Linear watt entry and
  `zero_energy_bands_hz` are not part of spectrum version 2.
  `power_basis = "total"` is accepted by every source type; `per_metre` only by
  line sources and `per_square_metre` only by area sources. The reference for
  density levels is 1 pW per metre or square metre, respectively.
- `values` always has eight entries. `null` means unknown. In the example,
  250 Hz is silent and 500 Hz is unknown. The engine converts zero to negative
  infinity before length/area normalization. NaN/infinity are never serialized.
- Sound power `back_calculation` is `locked` or `adjustable`; older definitions
  without this field are treated as locked. Adjustable definitions also carry
  `initial_values`, a separate eight-band array with the same dB/silence/unknown
  convention. Ordinary propagation uses `values`. Inversion uses
  `initial_values` as its starting guess and writes fitted bands only to
  `values`, leaving the initial array and authored Measurements unchanged.
  Current `values` may be unknown before the first inverse fit; a normal forward
  calculation still requires complete current values. Inversion requires all
  eight initial values to be known.
  This is an optional spectrum-version-2 extension; schema 10 needs no table migration.
- Absorption uses `kind = "absorption"`, `quantity = "absorption_coefficient"`,
  `unit = "1"`, and `weighting = "none"`. Each value is a finite coefficient
  between 0 and 1 inclusive, or null for unknown. Power-only fields are absent.
  Zero means no absorption. Definition versions 1 and 2 have identical absorption
  semantics and are readable; the editor writes version 2.
- `provenance` is text for the data source, measurement conditions, equipment
  state, material reference, or assumptions. It may be empty. It is not an
  immutable revision history or uncertainty model.

Spectrum type and identity are stable during editing. Values, names and
provenance can change; changes save transactionally and affect all assigned objects using
the same ID across variants. Catalogs belong to the GeoPackage, not to
the QGIS application or a global library. Deletion is refused while saved object
references remain, including archived layers. The plugin also requires source
edit sessions to end before deleting sound power records, and building/wall
edit sessions to end before deleting absorption records, because undo could
restore a reference. Object assignment uses QGIS's edit buffer.

Incomplete catalog records are permitted as drafts and labeled with unknown-band
counts. **Validate model** reports missing assignments and unknown bands for
included sources. Geometry saves and definition exports still permit drafts;
they do not certify calculation readiness. Buildings and walls can assign an absorption record using
`absorption_spectrum_id`. All eight bands must be known for calculation, including
when reflection order is zero. Missing or wrong-kind references are errors;
unknown bands never silently inherit a scalar. Unassigned objects retain scalar
reflection overrides/defaults. Catalog deletion protection includes reflecting
objects and removed layers; the UI also guards edit sessions and undo history.

### `acalc_directivities`

Schema 9 adds a project-local directionality catalog. Columns are `directivity_id`
(canonical UUID TEXT primary key), `name` (nonblank, case-insensitively unique TEXT),
`definition_json` (TEXT), and `revision` (positive INTEGER incremented on save).
The editor compares its loaded revision on save to prevent overwriting a concurrent
edit. Catalog writes advance project freshness through SQLite triggers.

The version-1 definition contains:

- `frequencies_hz`: 1–64 unique, positive, increasing finite band centres. Native
  imported frequencies are retained; no octave conversion is inferred.
- `directions`: individual `azimuth_deg`, `elevation_deg`, `levels_db` rows. Local
  +X is front, +Y left, +Z top. Azimuth is [0,360), elevation [-90,90]. Duplicate
  directions, including multiple azimuths at a pole, are invalid. Each row has
  one finite number or JSON null per frequency; null means unknown, 0 is a real
  dB value. A definition may be empty or incomplete as an authoring draft.
- `principal`: local azimuth/elevation of the reference used to aim the source;
  this is not an assertion about the direction of maximum radiation.
- `notes`, `level_reference` (`relative_power` or `correction`), and
  `interpolation` (`nearest_direction`). Relative patterns declare the intention
  to preserve total source power when later evaluated; this release performs no
  normalization. The current preview uses nearest-direction sampling only.
- `generator`: null or the template kind, version, original parameters, rules,
  and `modified` flag. Generated samples are the editable authority. Original
  inputs remain available when reopening a template; regeneration explicitly
  replaces the current draft. Stack drafts retain their upright and downwind
  curved-ray rules and `numerical_status: not_implemented`.
- `attachment`: null or original CF1/CF2 filename, format, SHA-256, base64 bytes,
  and `decoded: false`. Original files are retained, not parsed. Maximum 10 MiB;
  checksums are verified when reading. Attachments travel with JSON exports.

Point-source tables add nullable TEXT `directivity_id` and
`directivity_orientation`. Null/empty assignment is omnidirectional. An assigned
ID refers to this project's catalog. Orientation is JSON containing a proper
orthonormal 3×3 local-to-world matrix; null means identity. World +X/+Y/+Z are
grid east/north/up. Incremental world-axis rotations multiply on the left;
principal shortcuts use a deterministic shortest rotation, resetting roll.
The source stores its matrix independently of the shared principal; editing
that reference changes the aim of existing assignments and is disclosed in the
library editor. Stack drafts require the transformed principal Up.

Definitions used in saved or archived point-source layers cannot be deleted.
The UI also blocks deletion while a point-source edit session is open, including
an unmodified buffer with undo history. Definition exports contain the catalog;
source assignment/orientation remain in the referenced GeoPackage feature tables.
Model export is not a calculation-readiness assertion. Calculation supports
unassigned sources and explicitly rejects any assigned directional point
source in the selected variant, before publishing results.

## Upgrade from schemas 1–13

Opening a valid older model upgrades it transactionally to schema 15. SQLite's
backup API first writes `<stem>.schema-<old-version>-backup-<UUID>.gpkg` beside
the original, including committed WAL data. Backups are never overwritten.
Failure rolls back all changes and retains the backup.

Schema 14 introduces `source_building`. Legacy `building` layers containing any
nonempty `face_sources` array become Source Buildings, including archived layers.
Mixed layers move as a whole; layer/object IDs, geometry, component definitions,
and variant membership stay intact. Layers without attachments remain Buildings.
Both types share building height and surface material fields. The nullable
`face_sources` column is retained on both for schema continuity, but regular
Buildings must have NULL or an empty array; only Source Buildings can emit sound.
Source Buildings appear with point/line/area sources and count once in source
summaries, while each component is evaluated separately by the engine. Both
building types remain physical solids and contribute ground vertices.

Schema 13 adds `mounting` to building face point sources that lack it, from the
distance rule that previously selected the convention: zero distance becomes
`"surface"` and a distance of at least 0.05 m `"standoff"`. A legacy distance
between 0 and 0.05 m is left undeclared for review. Other fields and records
are unchanged.

Schema 12 removed every legacy `infer_z` column. Former inferred ground-point
objects receive `ground_mode = unresolved`; their geometry and IDs are preserved,
but explicit ground Z must be supplied before use. Explicit objects preserve
per-point/uniform authoring. Area receivers lose both legacy ground fields;
their XY grid, spacing and height above the explicit terrain surface remain.
Close legacy layer edit sessions before migration because removing columns
changes field indices. Existing non-editing QGIS layers refresh their fields.

Schema 11 added nullable TEXT `face_sources` to building tables. Earlier upgrades
also apply when opening older projects.

The schema-7 upgrade adds nullable `absorption_spectrum_id` columns to building
and barrier tables and updates variant definition versions. Existing geometry,
scalar overrides, spectra, settings, raster references and results are preserved.
The schema-8 upgrade adds the directionality catalog and nullable point-source
fields; existing source assignments default to omnidirectional. It preserves
all geometry, spectra, settings, rasters and result rows.
Schema 10 adds the Measurement layer type without changing existing feature
tables or calculation rows. Older plugins/engines reject schema 10, preventing
silent omission of authored measurements. Earlier directionality and
spectral-material assignments retain their existing behavior. The following
older-schema conversions apply only to schemas 1–6.

For schemas 1–4, `acalc_configurations` becomes `acalc_variants`, its `configuration_id` becomes
`variant_id`, and the matching calculation columns become `variant_id` and
`variant_name`. IDs, membership, feature geometry and the latest results per variant are retained.
Shared calculation settings move into `acalc_calculation`. If all configured
variants had identical settings, those settings are retained. If they differed,
all named sets are preserved in `legacy_settings_json`, the engine is left
unassigned, and the Calculate window asks the user to review and save a set.
Models with no previous calculation settings receive the visible new-model defaults.

Spectrum definitions become version 2. Legacy watt values convert with
`10 log10(W) + 120`; zero watts and explicit silence markers become numeric zero.
Unknowns remain null. Numeric zero now means no energy, including in migrated dB
spectra. Spectrum IDs, power bases, notes and assignments are preserved. Old
results retain their original values and parameter snapshots, and are marked out of date.

The older upgrades still add missing CRS unit conversion and calculation result
tables, normalize CRS serialization, install change triggers, and add explicit ground authoring metadata,
`height_mode`, and `height_value` defaults. Feature Z/M coordinates are unchanged.
Schema 1's undefined spectrum records/references still prevent migration.
Schema 6 migration removes `acalc_terrain_groups`, `acalc_terrain_members`,
`acalc_terrain_cache`, their invalidation triggers and variant `terrain_group_ids`.
The raster table is reduced to its reference columns, retaining names, paths, IDs
and bands without opening external files. Coordinates and layer membership are
unchanged; raster samples are not silently baked into geometry. Review migrated
variants that depended on off-variant layers, raster surfaces or priority overlays.
The backup retains their previous definitions and caches. Schema-5 calculation
settings are retained.

Failed transactions retain the old schema and the backup. Opening schema 15 makes
no further backup. The optional `ground_mode` column and building/barrier
`reflection_coefficient` columns are appended when missing, leaving existing
values null and geometry unchanged. Older plugins can open the
backup; re-export variant JSON from the upgraded model.

Engine 0.10.0 builds terrain from variant object membership and ignores rasters.
Legacy `enabled` values, including zero, do not exclude objects or terrain inputs.
The legacy column remains for file compatibility. Earlier numeric engine versions'
results are marked out of date. The CLI accepts schemas 3–5 and 7–12; schema 6 must
first migrate through QGIS so changed terrain semantics are explicit and backed up.
The group refresh CLI options have been removed.

## Feature table attributes

| Column | Meaning |
| --- | --- |
| `fid` | INTEGER PRIMARY KEY generated by GeoPackage |
| `geom` | GeoPackage geometry BLOB of the registered type |
| `object_id` | UUID TEXT, non-null and unique within the layer |
| `name` | Nonblank TEXT |
| `notes` | Nullable TEXT, free text |
| `enabled` | Legacy compatibility column, default 1. Ignored by current engines and hidden in QGIS forms/tables. |
| `ground_mode` | Ground-point types only: authoring TEXT `per_point` (default) or `uniform`. Migration-only `unresolved` blocks use until explicit Z is supplied. |
| `absorption_spectrum_id` | Building/barrier only, required schema-8 column: nullable TEXT referencing a project absorption spectrum; null/empty uses the scalar fallback |
| `directivity_id` | Point-source only, required schema-9 nullable TEXT catalog reference; null/empty is omnidirectional |
| `directivity_orientation` | Point-source only, required schema-9 nullable TEXT JSON local-to-world rotation matrix; null is identity |
| `reflection_coefficient` | Building/barrier only: optional nullable REAL reflected energy fraction 0–1; NULL or an absent legacy column inherits the corresponding calculation default |
| `ground_factor` | Ground absorption areas only: finite REAL G, dimensionless, 0–1 inclusive; default 0 |
| `canopy_height` | Trees only: finite positive REAL height above local terrain, in metres; application default 10 |
| `height_mode` | Buildings: `average`, `highest`, `lowest`, `absolute`, `individual` (default), or `absolute_individual`. Barriers (schema 15): `average`, `individual` (default), or `absolute_individual` |
| `height_value` | Buildings: finite REAL H in metres, default 6; non-negative for shared relative modes, absolute elevation for `absolute`, ignored by building per-point modes. Barriers (schema 15): default 3; nominal height controls the shared top in `average` and fills editable per-point heights when changed in `individual`. Absolute per-point tops are Z + M. |
| `height_m` | Area receiver only: finite non-negative REAL in metres, default 1.5; height of every generated point above local ground |
| `spacing_m` | Area receiver only: finite positive REAL in metres, default 10; horizontal grid interval on both axes |
| `broadband_a_db` | Measurement only: nullable finite REAL A-weighted sound-pressure level in dBA; entered manually when no octaves are supplied, otherwise derived from entered octaves |
| `broadband_z_db` | Measurement only: legacy nullable REAL retained for file compatibility; no longer calculated or displayed |
| `level_63_db` … `level_8000_db` | Measurement only: eight nullable finite REAL unweighted octave sound-pressure levels in dB re 20 µPa |
| `spectrum_id` | Nullable TEXT, only on point/line/area source tables; exact ID of a project `sound_power` spectrum; null/empty means unassigned |

Measurement NULL means unknown, while numeric zero is a real 0 dB observation.
dBA is the energetic sum over the entered octave levels after applying the
engine's nominal octave A corrections, rounded to one decimal place. An
incomplete octave set is summed over the bands that have values. The properties
editor updates dBA as each octave changes, and Acalc's layer-save action
synchronizes it after native QGIS attribute edits. Readers derive the displayed total from entered bands,
so stale externally written broadband fields cannot override octave values.
The point uses the same ground Z and height M geometry as a point receiver.
Measurements are displayed beside calculated levels for variants that include
their layer, including variants with no calculation run. Included Measurements
also act as point receivers, with the same ground and propagation rules.
Calculated levels are stored separately in `acalc_receiver_results` for each
variant and appear in result map layers. Calculation never overwrites authored
measurement fields. Their properties compare both sets and show calculated minus
measured for dBA and each octave band; unknown observations leave differences blank.
Editing Measurement features marks saved calculations stale, like other receiver
edits. Notes can record measurement conditions and provenance.
The optional inverse workflow uses octave Measurements as fit targets and
evaluates source-to-Measurement propagation once. It proposes nonnegative
least-squares source power in linear energy, then atomically updates only
adjustable spectrum `values` after checking the model revision. A fit does not
establish agreement with field measurements or ISO conformity.

An assigned absorption spectrum takes precedence over scalar coefficients and
calculation defaults, with **ρ = 1 − α** independently in each octave. It applies
to every exterior vertical building face or both sides of every wall segment.
Per-face/per-side assignments remain unsupported. Properties show a Surface material tab
with spectrum selection, an octave α preview and reference notes. Scalar controls
are disabled while a spectrum is assigned; clearing it restores the saved fallback.
The spectrum ID and its complete definition participate in the calculation
fingerprint; catalog edits invalidate saved results.

Without a spectrum, reflection coefficients use energy, **ρ = 1 − α**, uniformly
in all eight octaves.
Zero is a fully absorbing surface, one is fully reflecting, and NULL means
inheritance; zero must never be treated as missing. Building coefficients apply
to the exterior vertical perimeter faces; wall coefficients apply to both sides
of every segment. The UI displays and edits only absorption α, converting to
the existing stored ρ at the boundary; both default controls therefore show 0.1
for the stored 0.9 defaults. An unchanged control preserves the exact original
stored coefficient. The Properties checkbox **Use calculation default** stores
NULL; clearing it stores 1 − the displayed α. The raw ρ field is hidden in QGIS
forms and attribute tables. Changes use the normal Apply/Cancel, undo/redo and
layer-save workflow. The displayed default comes from the project's calculation settings.

The building default corresponds to ISO 9613-2:2024 7.5.2's α=0.1 facade default;
using the same default for standalone walls and selecting order 3 are application
choices. Select measured values and check convergence with reflection order where
appropriate. Material boundaries remain attached to authored objects even when
screening joins adjacent wall objects. Old files gain reflections on their next
calculation unless order is set to zero. Engine 0.13.0 adds diffraction to reflected
paths. Engine 0.14.0 adds octave material losses; results from earlier engine
versions are stale and require recalculation.
Resolved settings and object overrides participate in input fingerprints and the
run stores the normalized settings. See the engine's reflection scope for
eligibility, propagation and verification limits.

Ground absorption areas (`ground_absorption`) have a name, one G value, and a
2D polygon perimeter. Their properties show only X/Y coordinates; there are no
elevation, height, inference, or raster-sampling options. G = 0 denotes hard
ground and G = 1 porous ground. Common identity/compatibility attributes are
retained, but `infer_z` and `ground_mode` are absent. No migration of existing
geometry is required; older readers reject the unknown object type. These areas
never supply terrain elevations. Engine 0.11.0 uses included areas to override the
calculation's default G; uncovered portions retain the default. Only the selected
variant's layers contribute. The G values and 2D geometry participate in the input
fingerprint, and run metadata reports `ground_area_count`.

Project validation checks all active ground-area layers, identifies both objects
and layers in overlap errors, and includes pending edits. Calculation preflight
checks each selected variant and starts no batch if any has overlapping interiors.
The standalone engine independently rejects overlaps before receiver calculation
or result writes. Containment and duplicate polygons are overlaps even with equal
G. Shared edges/points are allowed. No layer/object ordering overrides another
area. Polygon holes remain unsupported.

The engine uses horizontal path-length-weighted G separately for the source,
receiver and middle regions; see [solver ground contract](API.md).
A path exactly along a shared boundary uses the mean of adjoining polygon G
values. A lone polygon boundary uses that polygon's G. Zero-length endpoint
regions use G at that endpoint, and a vertically aligned pair uses G at its shared
XY. These boundary/degeneracy choices are Acalc conventions. Overlapping source
and receiver influence regions are valid and are unrelated to polygon overlaps.

Trees areas (`trees`) store an XY perimeter and `canopy_height`. The properties
editor converts height to the project's chosen elevation units; storage remains
metres regardless of horizontal CRS units. There is no vertex Z/M, ground
inference or terrain contribution. Each footprint describes dense vegetation
from the calculation terrain to that terrain plus its height. Notes can record
density observations, season and height provenance. The 10 m initial height is
an authoring default, not an ISO-prescribed forest height.

Included Trees layers select simplified foliage attenuation under ISO
9613-2:2024 A.2.2. Their geometry and heights enter the input fingerprint; run and
validation metadata include `tree_area_count`. Overlap is permitted and traversed
volume is counted once. A separate ground absorption area controls G. Polygon
holes and the detailed forest method are unsupported. No existing feature tables
change, so schema 12 is retained; older readers reject this new feature type.
See the engine's solver contract for path handling.

For objects with ground points (excluding area receiver perimeters), line and polygon editing offers **Specify per point**, **Uniform**, and
**Infer from terrain**. Per-point mode writes each authored Z. Uniform mode copies
the first point's Z to every point when an edit is committed, and saves
those actual ordinates directly. Single-point objects offer Specify; lines and
polygons offer per-point or uniform ground Z. Neither imports nor calculations
support inferred object Z. The migration-only `unresolved` ground mode requires
explicit values before an object can be applied, saved, calculated or exported
to QSI. No calculation path interprets its stored placeholder ordinates as ground.

The six height modes are independent of the ground-point editing mode:

| Editing mode | Stored mode | Roof elevation |
| --- | --- | --- |
| Relative to average Z | `average` | Mean ground Z + H |
| Relative to highest Z | `highest` | Maximum ground Z + H |
| Relative to lowest Z | `lowest` | Minimum ground Z + H |
| Absolute | `absolute` | H at every point |
| Relative per point | `individual` | Resolved ground Z + each stored M |
| Absolute per point | `absolute_individual` | Each authored Z + M |

The average excludes the repeated closing vertex. Shared modes enable H and
display a disabled **Roof Z** column; per-point modes disable H and enable the
**Height** (relative) or **Roof Z** (absolute) column. Ground Z controls continue
to follow `ground_mode` independently. Changes apply after an editor commits.

Geometry M remains non-negative height above stored Z in every mode. Absolute
per-point editor values are converted to M = roof − stored Z when saved; their
absolute roof elevations are retained when ground Z is explicitly changed.
Absolute elevations may be negative; roofs below ground are rejected. Shared
relative roofs use the explicit ground reference. These rules apply to the
calculation snapshot without rewriting saved Z/M.

### Explicit ground and terrain construction

Schemas 12 and later have no `infer_z` columns or object-elevation inference. The selected
variant's explicit ground vertices define the terrain surface; M and absolute
roof/source/receiver elevations never seed terrain. Area-receiver perimeters,
ground absorption areas and Trees supply no terrain samples. Measurements contribute
explicit ground vertices like point receivers.
Terrain lines constrain the surface as breaklines. Receiver-grid locations
sample this explicit surface at their configured height above ground.

Excluded layers and raster references have no effect on calculation terrain.
Raster sampling is an explicit authoring operation that writes ground Z before
calculation. Coincident equal samples deduplicate; conflicting elevations fail.
Alternative terrain belongs in alternative layers selected by different variants.
The selected inputs participate in the calculation fingerprint. These are
application geometry policies, not ISO-prescribed interpolation rules.

The surface is a deterministic 2.5D triangulated network with linear elevation
inside each triangle. Terrain-line endpoints are samples, and their segments
must remain edges of the surface. Crossing breaklines are split at their
intersection. Their interpolated elevations must agree, as must a sample lying
on a breakline; inconsistent elevations are rejected with a `1e-7 m` consistency
tolerance. Vertical terrain faces and overhangs cannot be represented by this
single-elevation-per-XY surface.

Outside the convex hull, elevation is copied from the nearest boundary point:
linear interpolation along an edge and constant elevation beyond a vertex.
A single sample defines constant elevation everywhere. Collinear samples define
piecewise linear elevation along their shared line, extend perpendicularly,
and clamp to the end elevations beyond the samples. These are explicit Acalc
coverage conventions, not surveyed coverage or ISO interpolation requirements.
Add terrain samples and breaklines wherever the assumed extension is unsuitable.

The plugin supplies QGIS defaults and form constraints and validates before its
save/export actions. These feature constraints are **not all SQLite constraints**;
external tools and QGIS's native save actions can write invalid content. A reader
must validate before evaluation. Native QGIS edits preserve geometry Z/M when
supported by the chosen tool; users must review digitizing/interpolation defaults.

## Exported variant file

An individual UTF-8 `.json` export contains `format: "acalc-model-variant"`,
`schema_version: 14`, `project_id`, `variant_id`, `name`, `definition`,
`calculation` (the shared engine and settings), `layers` (the included registry
records), `spectra` (both spectrum catalogs), and `directivities` (project directionality definitions, revisions and retained attachments).
Each spectrum is `{ "spectrum_id": "…", "name": "…", "spectrum": { … } }`.
Variant source assignments are selected through included source features;
catalog membership is project-wide, with no scenario-specific spectrum overrides.
`geopackage` is a filesystem path relative
to the JSON file, not a URI. The JSON is a definition, **not a geometry snapshot**;
engines read feature geometry from the referenced GeoPackage. Export validates all
features in included layers and rejects unsaved edits in those layers. Every
nonempty source reference must resolve to a sound power record, in every included layer. Unassigned sources and unknown bands remain explicit drafts.
The catalog in JSON records values at export time; the GeoPackage remains mutable.
Re-export after editing and do not treat this as an immutable calculation snapshot.

A JSON export is not required to calculate. Tools that read the model
GeoPackage directly for a variant must:

1. Validate the model and variant schema, resolve the GeoPackage, and verify
   its `project_id` against the definition.
2. Resolve variant layer IDs through the active registry. Never evaluate
   unrelated tables or archived layers.
3. Read every feature in the included layers, check geometry and identity constraints, convert
   XY units as needed, and interpret Z/M according to this specification.
4. Resolve source spectra by ID and require complete emissions and explicit engine
   parameters before evaluation; report unsupported data instead of assuming defaults.

## Current calculation and receiver results

`acalc_calculation_runs` and `acalc_receiver_results` are registered nonspatial
attribute tables. Results attach to receiver objects through stable layer and
object UUIDs. They do not depend on row FIDs or editable object names. A separate
table allows the same receiver to retain different results for different
variants without adding scenario-dependent columns to its geometry table.
Each successful calculation atomically replaces that variant's calculation
metadata and receiver rows. Earlier runs are not retained. Opening older files
removes superseded runs and their receiver rows, keeping the latest result set
per variant. The existing table names and internal `run_id` are retained for
file compatibility and identifying which calculation a result layer displays.

### `acalc_calculation_runs`

| Column | Type / meaning |
| --- | --- |
| `run_id` | TEXT PRIMARY KEY, UUID |
| `project_id` | TEXT NOT NULL, model identity at calculation time |
| `variant_id`, `variant_name` | TEXT NOT NULL, scenario identity and display-name snapshot |
| `engine`, `engine_version` | TEXT NOT NULL, engine identifier and implementation version |
| `standard` | TEXT NOT NULL, standard edition used |
| `started_at`, `finished_at` | TEXT NOT NULL, UTC timestamps |
| `input_sha256` | TEXT NOT NULL, SHA-256 of the engine's input snapshot; excludes stored results |
| `project_modified_at` | TEXT NOT NULL, model modification timestamp at input capture |
| `status` | TEXT NOT NULL, always `complete` |
| `settings_json` | TEXT NOT NULL, explicit settings snapshot |
| `source_count`, `receiver_count` | INTEGER NOT NULL, non-negative evaluated object counts |
| `elapsed_seconds` | REAL NOT NULL, non-negative calculation duration |
| `warnings_json` | TEXT NOT NULL, JSON list, default `[]` |
| `metadata_json` | TEXT NOT NULL, JSON object, default `{}` |

For API runs produced with schema 14, `source_count` counts authored point, line,
area, and Source Building objects, with each Source Building counted once.
Run metadata also stores `component_source_count` (the total individual sources
submitted to the engine), `source_building_count`, `building_count` (ordinary
Buildings), and `solid_building_count` (both building types). Component result
identities and spectra remain separate. Older saved runs keep their original
counts and remain readable until recalculated.

The terrain engine records profile
`terrain-omnidirectional-point-line-area-buildings-barriers-mixed-ground-reflections-diffraction-spectral-materials` and the included
`building_count` and `barrier_count` in run metadata. The CLI reports those counts for readiness checks
and completed calculations as well. Existing schema 3–5 files already contain the
ground/building/barrier coordinates and require no shielding schema migration. Included
object geometry, elevation/height settings, and variant membership participate
in the input fingerprint checked before results are committed.

### `acalc_receiver_results`

| Column | Type / meaning |
| --- | --- |
| `run_id` | TEXT NOT NULL, foreign key to the completed run; cascade on run deletion |
| `layer_id`, `object_id` | TEXT NOT NULL, stable receiver identity; normalized UUID strings |
| `receiver_name` | TEXT NOT NULL, display-name snapshot |
| `octave_levels_json` | TEXT NOT NULL, array of exactly eight unweighted received sound pressure levels in dB re 20 µPa, ordered 63–8000 Hz |
| `broadband_z_db` | Legacy REAL or NULL; new calculations leave it NULL |
| `broadband_a_db` | REAL or NULL, energetic A-weighted total of octave levels, dBA re 20 µPa |
| `metadata_json` | TEXT NOT NULL, JSON object, default `{}` |

The primary key is `(run_id, layer_id, object_id)`. Each octave level is finite or
null. In a completed result, **null means zero received energy**, never missing
data. A broadband level is null only when all octave bands have zero energy.
0 dB remains a real, nonzero level. Receiver levels are independent of the
sound-power catalog's reference quantity and units.
Engine 0.16.1 and later keep full precision throughout propagation, then round
each octave and broadband result independently to one decimal place at save.
Older saved rows are retained as written and marked out of date; rerun the
calculation to store rounded results. Result tables and labels display one
decimal place.

New engine runs retain the authored receiver POINT ZM GeoPackage blob as
`metadata_json.geometry_gpkg_hex`. This is a position snapshot for displaying
results independently of later receiver edits or deletion. Z/M retain the
authored ground/height convention; the blob is not a record of inferred terrain
or every resolved acoustic input. Adding this optional metadata does not change
the acoustic calculation or the model schema version.

### Area receiver generation

`area_receiver` is an additive layer type in schema 5, with one simple PolygonZM
perimeter, common object fields, `height_m`, and `spacing_m`. Existing model
tables require no migration. The perimeter defines only the XY grid boundary.
Acalc properties offers XY vertices, one **Height above ground** and **Point spacing**;
there are no ground-point modes, ground Z cells or raster-sampling controls.
The editor saves placeholder Z=0 and the shared height in M for compatibility.
Perimeter Z/M are ignored at calculation time; schemas 12 and later have no ground-mode or
inference columns on receiver areas.
`height_m` is authoritative, including when another editor changes that attribute
without updating the perimeter M. Neither perimeter vertices nor generated
receiver points seed, extend or constrain the terrain surface.
Readers that do not support this type must reject it rather than omit it.

In metric calculation coordinates, the axis-aligned grid starts at the
perimeter's minimum X and Y. Columns/rows advance by `spacing_m`; points outside
the polygon are omitted. Concave polygons and either winding are supported.
Area edges are included. Every building in the same variant excludes
points inside or on its footprint, irrespective of roof or receiver height.
Boundary tests use a 1e-8 m distance tolerance. No barrier footprint is inferred.
Holes, rotated grids, and a user-defined grid origin are unsupported.

After terrain resolution, each retained point samples ground Z from the
final surface and uses `height_m` as its Height. Generated points do not seed or
modify that surface. Their saved `geometry_gpkg_hex` is a generated POINT ZM,
with X/Y converted back to project units, resolved ground Z, and shared height.
All input polygon geometry, properties, terrain and included building footprints
participate in the input fingerprint. Grid generation is cancelable and bounded
to 1,000,000 bounding-box candidates across all areas in a variant. Exceeding
the limit fails with a spacing diagnostic and keeps earlier results intact.
An empty grid is valid, including a completely covered polygon; a successful
run replaces previous results even when no points remain.

Current runs do not store area samples as result rows: the API delivers each
area as a GeoTIFF (below) and `acalc_receiver_results` holds only point
receivers. The run's `receiver_count` still counts every calculated sample.

In legacy runs, each generated result has its own `object_id`: UUIDv5 with the normalized parent
polygon UUID as namespace. Its UTF-8 name is
`acalc-area-receiver-v1/<layer UUID>/<X bits>/<Y bits>`, where each coordinate is
the calculated metric float64 encoded as 16 lowercase hexadecimal digits of its
IEEE-754 bits. Identical retained grid locations preserve identity as building
exclusions change. Metadata also records `area_receiver_id` (the parent polygon
UUID), `grid_column`, and `grid_row` (zero-based bounding-box indices). The result
name appends one-based `[column, row]` to the area name. Results and map properties
use the parent link to open the editable area and its surface preview. Calculation
samples remain internal numerical data; the results list groups them by parent area.

### Receiver-layer and variant result map layers

When opening a model, refreshing results, or completing a calculation, the plugin
materializes each receiver layer from the latest completed variant run in
`acalc_results_<variant UUID without hyphens>_<receiver layer UUID without hyphens>`.
These POINT ZM tables are registered in `gpkg_contents` and `gpkg_geometry_columns`, but **not** in `acalc_layers`.
They cannot become variant inputs and do not change `acalc_project.modified_at`.
They are read-only in Acalc, stored in the GeoPackage, and placed in a separate
Results group. Calculation updates the existing receiver-layer/variant result
layer, creating it if absent. Its previous features are replaced, while the QGIS
layer identity, style and visibility are preserved. CLI results are projected on the next plugin open
or results refresh. New run metadata includes `receiver_layers`, a list of
`layer_id`, `name`, and `feature_type` snapshots, so included empty receiver layers
also receive empty result layers. All included receiver layers are evaluated;
there is no separate receiver-type selection in the calculation window. Point
receiver layers use stable layer/variant IDs. Area results use the additional
parent object ID and are raster surfaces, described below.

Earlier projections named only by variant are split into separate receiver-layer
projections on refresh. Cached positions recovered for old runs are preserved.
Their combined tables and map layers are then removed. A successful recalculation
also removes projections for receiver layers absent from that variant's new run.
Other variants' result layers remain in place. Splitting, replacing, and removing
these derived tables occurs in one transaction; authoritative saved results are
not changed by projection refresh.

Each point projection contains `fid`, `geom`, `run_id`, `variant_id`, `layer_id`,
`object_id`, `receiver_name`, `broadband_a_db`, the eight
`lz_<frequency>_db` fields, `out_of_date`, and `geometry_source`. Null level values
retain their zero-energy meaning. Identity is the receiver layer/object UUID pair.
The description has `acalc_result_layer: 4` and records the source receiver layer,
variant, projected run, and revision status. Refresh updates the out-of-date flag
and display name, including renamed receiver layers and renamed or deleted variants.
Projection failures leave the previous projection and saved calculation values intact.

For legacy runs without geometry metadata, a position may be copied from the
saved receiver only while the model revision still matches the run. Such a
recovered position is retained in that projection on later refreshes. Otherwise
the result remains an attribute row with null geometry and
`geometry_source='unavailable'`; current geometry must not be presented as an
unknown calculation position. Result points open the original receiver's properties
through its stable identity and select the matching variant. Deleted receivers
retain their saved result rows, even though their properties can no longer open.

#### Area result GeoTIFFs (current runs)

After the results document is read, the plugin downloads each area's raster
link (API feature `area_receiver_rasters`) into a staging folder, verifies its
size and SHA-256, checks its grid against the submitted boundary and spacing,
then writes `<model stem>_results/<run UUID>/<area name>_<area UUID prefix>.tif`
beside the GeoPackage: the engine's nine Float32 bands (dBA, then 63–8000 Hz
unweighted) with the model CRS assigned and the geotransform converted from
metres to project units (DEFLATE, floating-point predictor, tiled). These files
are outside the GeoPackage; move or copy the folder together with the model.
Run metadata `area_rasters` lists, per area, `api_area_receiver_id`, `layer_id`,
`object_id`, `path` (relative to the results folder), `receiver_count`,
`columns`, `rows`, `engine_bytes`, `engine_sha256` and `sample_warnings`. The
folder is created before the results transaction and deleted if it does not
commit. A replaced or cleared run's folder is deleted afterwards. Folders that
could not be deleted (for example a file still open on Windows) are removed by
a later results refresh. Only UUID-named folders are ever removed.

The GeoTIFF itself is the QGIS result layer; no GeoPackage table is written.
Its record key is `file:<path>`. On recalculation, the default, renamed and
duplicated views of the same variant/receiver-layer/area, including a legacy
GeoPackage surface, are re-pointed to the new file, keeping names and styles.
NoData marks missing/excluded samples **and** zero-energy bands; the raster
does not preserve that distinction or per-sample omission diagnostics. Hover
reads the pixel under the cursor and reports nothing for NoData.

#### Legacy GeoPackage surfaces

Runs saved before area rasters keep their samples as result rows. Their area
receiver surfaces use `acalc_area_<variant UUID hex>_<layer UUID hex>_<area UUID hex>`.
The owned `gpkg_contents` description has `acalc_result_layer: 5`, `result_type: area`,
and `area_receiver_id`. New records include `raster_bands`: dBA followed by
63, 125, 250, 500, 1000, 2000, 4000 and 8000 Hz. The base table stores dBA;
eight tables suffixed `_63` through `_8000` store unweighted octave levels.
Each is a Float32 GeoPackage tiled gridded coverage with TIFF tiles generated
by GDAL. Octave-table descriptions carry `acalc_area_band: 1`, `parent_table`
and `frequency_hz`; ownership is verified before replacement or deletion.
QGIS exposes all nine as one multiband raster through an inline GDAL VRT,
which can be exported as a nine-band GeoTIFF. No VRT sidecar is needed and all
pixels remain in the GeoPackage. Existing dBA-only surfaces are upgraded by
**Refresh saved results**, using their already-saved octave totals.
Grid pixels are centred on the calculated
points at the saved `spacing_m` converted into project CRS units; raster row order
is north to south. Pixel footprints extend half a cell past their centre, including
at perimeter samples. Missing/excluded and zero-energy samples use NoData and
are transparent. The original numerical records retain the distinction between
zero energy and a missing location. Hover lookup uses those original records,
never rounded raster values or contour interpolation.

The QGIS surface offers discrete filled 5 dB bands (blue at/below 30 through red
above 80 dBA) or 5 dB contour lines with bilinear display resampling. A band
selector switches between dBA and the eight octave levels (dB). The same modes appear in area properties,
with the saved perimeter outline and nearest-sample cross. Hovering inside the
area selects the closest retained calculation point and fills exactly one row
with dBA and all eight unweighted octave levels. The main results list contains
one parent area entry with no summary/max levels. These displays do not change
any propagation result, and the raster is not terrain input.

Legacy API saves retained aggregate area-sample levels and numerical omission/state
diagnostics, without per-source contributions in either receiver metadata or
the run's `api_result`. Current saves keep only the area summaries (including
the `raster` descriptor and `sample_warnings` digest) in `api_result`; pathway
warnings for area samples are reported from those digests. Point receiver
contributions are retained.

Engine 0.18.3 adds `area_receivers` to run metadata: each entry saves `layer_id`,
`object_id`, `name`, `geometry_gpkg_hex` (the perimeter), `spacing_m`, and `height_m`.
Empty areas are snapshotted too. The boundary is never taken from edited geometry
for a stale result. Legacy runs may recover it from an unchanged model; otherwise
multi-point grids recover spacing from saved grid indices/positions and display
without an unknown outline. Insufficient legacy information requires recalculation.
Old area point projections are removed after replacement surfaces succeed. GDAL
writes temporary rasters; tile data and coverage metadata are transferred inside
the same transaction as the other derived projections, retaining previous maps
on failure. No sidecar raster files or model schema migration are needed.

Variant, layer and feature identities deliberately have no foreign key from
results: deleting or archiving an authored object does not immediately erase its saved result.
The next successful calculation replaces the result set with its evaluated receivers.
Replacement of the calculation metadata and every receiver row is one transaction.
Failed or cancelled calculations leave existing results intact. The engine checks
the input snapshot again before committing and refuses results if another writer
changed the model during calculation. Saving results does not change
`acalc_project.modified_at`.

The plugin marks saved results out of date if their saved project identity or
modification timestamp differs from the current model. This is conservative: even
a saved name change can make a run stale. The stored input hash records the exact
input snapshot independently. Input-table triggers make normal external SQL edits
stale as well; replacing tables, removing triggers, or modifying the database
outside SQLite can bypass this marker. The engine's content hash remains the
authoritative input-change check before result commit. Keep writes committed before
copying a GeoPackage; SQLite's backup API is preferable when WAL mode is in use.


## Building face sources

Source Buildings have nullable TEXT `face_sources`, introduced on buildings in
schema 11 and restricted to `source_building` emitters in schema 14. NULL or an
empty JSON array means no attached emitters. Regular Buildings may retain the
column but reject nonempty attachments. Otherwise the array contains records with:
`id` (canonical UUID, unique within the building), `name`, `face`, `vertex_count`,
`x_m`, `y_m`, and nullable `spectrum_id`. Area sources additionally have
`width_m` and `height_m`, with no `type` field. Point sources instead have
`type: "point"`, `mounting` and `distance_m`, with no dimensions. `mounting` is
`"surface"` (an opening, louvre or panel on the face; `distance_m` is 0) or
`"standoff"` (a unit clear of the face; `distance_m` is at least 0.05 m). A
point's distance is perpendicular outward from the face in metres. Records
written before `mounting` existed are declared by the schema-13 upgrade. A
legacy record between 0 and 0.05 m stays undeclared: it remains readable but
cannot be placed or calculated until the user chooses surface or standoff; the
editor proposes standoff at the 0.05 m minimum. Distance alone never selects
the convention at calculation time. Roof sources may have `frame: "building_local"` or the older
`frame: "roof_top_edge"`; other frame values and frame fields on walls are rejected.
Width/height must be positive finite metres; offsets must be nonnegative finite metres. Unassigned
or incomplete power may be authored, but cannot be calculated. Assigned spectra
must be sound power: `total` for points, `total` or `per_square_metre` for areas. References participate
in catalog usage, deletion protection and export checks, including archived layers.

`wall:a:b` references zero-based authored perimeter vertices ordered CCW.
Its origin is the roof at a, X follows the horizontal a-to-b edge, and Y points
vertically down, as seen from outside. Its sources move with the left roof vertex
when that vertex's height changes. The building local origin is authored vertex 0,
local X follows the horizontal edge 0–1, local Y is 90° CCW, and local Z is up.
Rigid translation and rotation preserve that frame and all attachment offsets.
A planar `roof` with `building_local` uses the building's X projected into the
roof plane, with perpendicular down, to select its top boundary edge. The older
`roof_top_edge` instead selects the top edge in the projected-east/projected-south view
as X. The edge with the highest midpoint is selected; midpoint heights within
10⁻⁸ m are tied and the leftmost midpoint wins. X runs left to right (downward
for a vertical edge); Y is perpendicular within the roof plane. The origin is
the minimum-X/minimum-Y corner of the rotated planar bounding box. Nonplanar roofs use `roof:a:b:c`, with sorted
indices identifying a CCW first-ear triangulation starting at authored vertex 0.
Each facet uses the same top-edge selection convention. Version 0.23 changes
the model's roof triangulation from the world-XY lexicographic start to this
authored start, keeping roof geometry/facet identity stable through rotations.
An existing nonplanar roof can have different diagonals when its first authored
vertex was not the former canonical start. Missing attachment facets are rejected;
the editor's **Unattached sources…** control supports reassignment or removal
within the building draft. Reordering perimeter vertices changes local coordinates
and is prohibited while sources are attached. Local triangulation uses a 10⁻⁸ m
signed-distance tolerance for ear convexity and containment, keeping collinear
diagonal contacts stable through ordinary rotation/translation rounding.
Records without `frame` retain their original projected-east X / projected-south
Y geometry. New or edited roof sources use `building_local`; the editor previews
existing offsets/sizes in the new axes and explicitly applies that alignment on
Apply sources. Cancel preserves their saved frame and placement. Older engines
reject the new field rather than silently interpreting it in world axes.
Source rectangles must lie entirely inside the selected planar polygon, including
concave cutouts. A point's anchor must lie inside/on that polygon; its displaced
position can lie outside its footprint. Displacement uses `down × right`, the
unit outward normal, including the upward normal on inclined roofs.
The underside is not an emitting face. All offsets/sizes are metres regardless
of the horizontal CRS unit. Vertex indices are identities within an unchanged
perimeter topology: the editor disables insertion, deletion and reordering while
sources exist. External topology edits require removing/reassigning sources.
Coordinate and height edits retain offsets but must preserve a valid face and fit.

Definitions are stored in the building's feature edit buffer; Apply, undo/redo,
duplication, cancellation and save operate atomically with its geometry. Sources
dragged in the graphical editor use a 0.01 displayed-length-unit position/size
grid, with minimum width/height of 0.01 of that unit. Metre fields display two
decimals; converted units retain extra precision. Dragging a point marker or
rectangle body translates it; an area's bottom-right handle changes its size. Escape restores the draft
before a drag. The editor rounds existing finer values in its draft; cancellation
retains the original definitions. This UI convention is not a storage or engine
precision limit: readers continue to accept existing finite finer coordinates.

Component sources belong to their Source Building's variant membership. The engine resolves the building's
ground and roof modes, reconstructs rectangles as two 3D triangles each and point
attachments as single ordinary point sources. Actual rectangle area is width ×
height; density spectra receive 10 log10(area). Point power has no area correction.
Source IDs include layer/building/source UUIDs; inverse spectrum assignments are
retained. Final terrain supplies source ground Z and height without adding source
attachments as terrain supports. Type, dimensions, distance, spectra and resolved geometry enter
the run fingerprint. Invalid attachments fail before replacing saved results.

Surface points and rectangles radiate outward only and are not reflected by
their own face; standoff points are ordinary point sources that their face
reflects (ALGORITHM.md, building-attached sources). No transmission or
additional facade correction is inferred.
Existing screening/reflection path exclusions remain; published ISO facade-source
reference verification is pending.

## Schema 15 barrier height controls

Migration backs up schemas 1–14 and adds barrier `height_mode` (`individual`) and `height_value` (3 metres). Existing Z/M geometry is preserved. Average mode resolves the top as mean vertex Z plus H; each stored M is top minus local Z. Absolute per-point mode preserves Z + M when ground elevations change. Relative per-point M values remain independently editable; changing nominal H in the editor fills them all.
