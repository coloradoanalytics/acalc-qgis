# CadnaA 2021 model export

On Calculation, choose **Export…**, select a variant and format, and choose a
new export folder. Export does not require a calculation API connection. It
uses saved model data; open layer edits must first be saved through the existing
edit-buffer protection. Existing export folders are never overwritten. A failed
conversion or write leaves no partial export folder.

## Formats

**CadnaA 2021 — QSI model** writes `model.qsi`, the referenced SHP/SHX/DBF
files, spectra and continuous operating times, `IMPORT.txt`, an ID/settings
report, and a complete authored variant JSON snapshot. Import `model.qsi`
using CadnaA's QSI filter, with all companion files in the same directory.
The QSI format is `DIN45687-QSI:2010-7.1`, matching the supplied vendor export.

Supported: omnidirectional point, line and planar area sources; point receivers;
explicitly elevated buildings and barriers with scalar reflection coefficients;
terrain points/lines; and ground-factor polygons. Legacy `enabled` does not
exclude objects, consistent with the model contract. Measurements are listed
as omitted noncalculation objects; they are not converted to prediction receivers.

QSI conversion blocks legacy objects awaiting explicit ground Z, building-attached emitters,
receiver areas, Trees, assigned directivity and assigned absorption spectra,
polygon holes and conflicting explicit ground values at identical XY. These
require explicit ground authoring or additional mapping, as applicable. Empty unsupported
layers do not block export. No selected acoustic object is silently dropped.
Vertical area sources are excluded by the 2021 manual's QSI profile; the
exporter requires valid XY polygon geometry and a planar emitting polygon.

**ESRI shapefiles — authored GIS layers** exports each nonempty selected
layer with its original CRS, ground Z and height M, where applicable. DBF files
contain names and export IDs. Full feature attributes, library definitions and
original GeoPackage geometry bytes (hex) are retained in `model_snapshot.json`;
`export_report.json` maps file/ID pairs back to original layer/object IDs.
These GIS layers are not calculation-ready CadnaA objects. CadnaA 2021's QSI
filter must not be used to import these raw geometries as an acoustic model.

## Coordinates, levels and settings

QSI XY coordinates are the projected model coordinates multiplied by the
authoritative CRS linear-unit factor. Vertical values remain metres. Source and
receiver Z are absolute elevations, building polygon Z is roof elevation, and
barrier line Z is top elevation. Shared building roof modes reuse
`object_geometry.building_heights`; per-vertex absolute modes retain saved Z+M.
Every explicit ground vertex is also supplied as a height point; terrain lines
retain their breakline geometry. Ground absorption polygons are 2D and supply
no terrain samples. This preserves terrain inputs, not triangulation identity.

Sources retain total/per-metre/per-square-metre power through `LREF` (1/2).
Spectra use absolute unweighted octave levels, 63–8000 Hz, in the vendor's
`VALUE_18` through `VALUE_39` fields at increments of three. Schema-2 zero
energy is written as the published -200 sentinel; unknown bands are rejected.
The source's `LW` is the corresponding A-weighted energy sum, `EMI_TYPE=2`.
Continuous operation uses `TH_REF=2`, 60 minutes in every hour. Scalar energy
reflection coefficient rho becomes loss `-10 log10(rho)`; zero uses the
published -200 no-reflection sentinel. DBF numeric precision follows the sample
(usually 0.1 dB); widths expand when necessary, including for silence. Names
are limited to 40 characters and Windows-1252; full names remain in the report.

Match weather, default ground factor, meteorological correction, reflection
order, cutoff and integration settings manually from the report. The QSI package
does not claim to set CadnaA's calculation configuration. Feet-based projects
export scaled metre XY in a local frame; do not assign the original feet-based
EPSG code to those numbers. Check georeferencing before adding other layers.

## Evidence and outstanding verification

- A CadnaA 2021 QSI sample export and its companion DBFs establish the
  container dialect and point-source, receiver, building, barrier, contour,
  spectrum and operating-time field layouts. Record-free copies of those DBFs
  in `tests/fixtures/cadnaa/` retain the field layouts, which tests read directly.
- `CadnaA_Reference_2021MR2.pdf`, sections 7.2.14, 7.2.21 and 13.3.9, documents
  the supported QSI object categories and exclusion of vertical area sources.
- [ÖAL 36 Blatt 4 (2007)](https://www.oeal.at/images/rl_downloads/rl_36_bl4_2007.pdf),
  sections 6.1 and 6.2, supplies independent coordinate, source power-basis,
  silence, receiver, reflection-loss and ground-factor semantics. Its older
  spectrum indexing and barrier extensions are not substituted for the vendor
  sample's 2010 layouts. Line/area source fields share the documented industrial
  source record. Ground `G` is supplementary published evidence, without a
  matching CadnaA 2021 export fixture yet.
- `tests/test_model_export.py` checks vendor field layouts and independently
  constructed GDAL readbacks: source/receiver elevation, shared roof elevation,
  barrier top elevations, octave indexing, silence, operating time, density
  basis, ground-factor polygons, survey-foot conversion and variant membership.
  Tests also exercise blocked features, failure cleanup and overwrite refusal.

Actual import/re-export in CadnaA 2021 is **unverified**. In particular, inspect
reflection-loss interpretation: the supplied sample contains +200, whereas the
published older QSI definition describes -200 for no reflection. Check both
sentinels, positive reflection losses, octave interpretation, density references,
terrain reconstruction, source/receiver heights and ground G in CadnaA before
using results as comparison evidence. Nonhorizontal roofs need explicit checking.

Acalc's target remains ISO 9613-2:2024 with ISO 9613-1:1993 absorption. CadnaA
2021's manual describes ISO 9613-2:1996. Geometry transfer tests are supplementary
engineering evidence under ISO 17534-1 and ISO/TR 17534-3:2015, not published
acoustic reference passes, a 2024 acceptance interval, or conformity evidence.
