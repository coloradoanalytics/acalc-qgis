# Acalc user guide

This guide walks through model authoring, spectra, calculation and results in
the Acalc QGIS plugin. See the [README](../README.md) for installation.

## First model

1. In **Project**, choose **New**, enter a name and `.gpkg` filename, and choose
   a local **projected** coordinate reference system suited to the site. Existing
   files are never overwritten by project creation. **Coordinates and distance
   inputs use the CRS units.** Choose **meters or feet** separately for elevations
   and object heights, and specify the vertical datum in project details.
   Use **Project → Change CRS and units…** later to change these settings.
   Changing CRS reprojects every saved model layer, including archived layers,
   in one transaction; it does not transform the vertical datum. Existing results
   become out of date and retain their original map location.
   Horizontal spacing, source dimensions and calculation distances follow the
   exact CRS unit conversion, including US survey feet where applicable.
   Raw Z, M and meter-valued attributes remain stored in meters. Sound-power
   density references remain per meter or square meter. The separate elevation
   feet option means international feet (0.3048 m).
2. Choose a feature view from ☰, then use **☰ beside the layer selector → New layer**.
   **Terrain** has Points, Lines, Generated terrain, Ground Absorption, and Trees tabs.
   **Objects** has Buildings and Barriers tabs.
   The same layer menu contains **Rename layer** and **Delete layer**. Each view supports
   multiple independently named layers. All model layers appear in one
   **Acalc · project name** group with type-specific symbols.
3. Use **Add coordinates** for exact entry, or **Draw on map** to capture geometry
   using QGIS's snapping, coordinate transformation, and digitizing controls.
   Click once for a point; finish a line or polygon with a right-click.
   **Acalc properties opens automatically**, without QGIS's generic attribute
   dialog. Review coordinates, ground Z, height, and other properties; QGIS's
   digitizing defaults supply the initial Z/M values. **Apply to layer** adds the
   feature; **Cancel** discards the sketch. You can then draw another feature.
4. Use QGIS's selection tools or select rows in the dock. Layer selection opens
   the corresponding Acalc view. **Double-click a row** to open that feature's
   **Acalc properties** dialog. **Properties** also opens the dialog for a single
   selected feature. Name, notes, explicit ground elevations, all vertex coordinates, and the
   source spectrum assignment live in this modal; the dock contains the feature
   list rather than a property preview. **Go to** applies valid edits, closes the
   window and zooms to the object. **← Previous** and **Next →** apply valid edits
   and browse objects in layer order, preserving the current tab. **Cancel**
   discards changes to the current object; edits already applied while browsing
   remain in the layer edit buffer. Older objects with inferred ground require
   explicit ground Z values or raster sampling before applying changes.
   The feature list shows the first 500 name
   matches plus all selected features.
5. Use the buttons at the top of each object view. Click **Save layer edits** to commit a layer. Coordinate edits and feature
   deletion participate in QGIS's undo history until committed. **Discard edits**
   rolls back the layer. Both buttons are active only while the layer has unsaved edits, including after undo/redo. Acalc validates coordinates, dimensions, non-negative
   heights, polygon topology, names and object identifiers before
   saving through its dock.
6. On **Calculation**, select a variant from the dropdown. Its **⋯** menu contains
   Create, Rename, Delete, and Duplicate. Check layers in the tree below, grouped
   by object type. Membership saves immediately. **New layers are excluded by default**.
   Layer visibility in QGIS is independent of variant membership. Every object
   in an included layer participates; there is no per-object inclusion switch.
7. Use **Export variant definition** in the variant's **⋯** menu to save an individual JSON definition
   with a relative reference to the GeoPackage. Included layers must be saved and
   pass validation. Keep the JSON and GeoPackage together when sharing them.

Layer names show `*` in Acalc while their layer has unsaved edits; the QGIS
Layers tree shows a save indicator beside those layers. Undoing, saving or
discarding edits updates these indicators. Double-click a layer in Calculation's variant tree,
Terrain, or the QGIS Layers tree to open its view in the persistent Acalc window.
Default styling is applied only when Acalc creates a new layer. Custom symbols,
labels (including turning labels off), and opacity persist through selection,
data refreshes, recalculation, and reopening a saved QGIS project. Save the QGIS
project to retain these presentation settings between sessions.
Terrain rows include their object type. QGIS organizes layers into **Results**,
**Sources**, **Receivers**, **Objects**, and **Terrain** under the Acalc project
group. Source Buildings go into Sources alongside point, line, and area sources.
Buildings, barriers, ground absorption areas, and Trees go into Objects; terrain
points, terrain lines, and imported elevation rasters go into Terrain. Generated
result layers go directly into Results. Existing type and result variant folders
are consolidated when the model reloads. These groups do not change variant membership.
In the persistent window, **Sources** has Points, Lines, Areas, and Source Buildings tabs;
**Receivers** has Points, Areas, and Measurements tabs, like the Terrain page.

For layers with editable ground elevations, use **☰ beside the layer selector →
Set Z values…** to give every object a fixed ground Z or sample every vertex from
an elevation raster. The fixed value uses the project's elevation units. All
objects are included, regardless of selection or the name filter. Explicit Z
values set explicit ground elevations; object heights and building roof settings keep
their meaning. The entire change is one undoable edit, and a failed raster sample
or invalid roof clearance leaves the layer unchanged. Use **Save layer edits**
to save it. Area receiver boundaries, ground absorption areas, and Trees have no editable
ground Z, so this command is omitted for those types.

To copy external objects, load the vector layer into QGIS, then use **Project →
Import layer…** or **☰ beside the layer selector → Import layer…**. Choose the
source, a compatible Acalc object type and a name for the new layer. You can
copy all features or only the selection, including current unsaved source edits.
Map fields for object names, notes, ground elevations and heights, or supply
constant values. Ground absorption areas accept a G field/value; Trees accept a tree-height field/value; area receivers
also have height and spacing; sources can share a compatible project spectrum.

Import transforms X/Y into the model CRS. Select the units of source ground Z
and height fields explicitly; elevations must use the model's vertical datum. Choose geometry Z explicitly to use it
as **ground** elevation; roof/source elevations and geometry M are not interpreted
automatically. Alternatively, choose a ground field/value.
Multipart features become separate objects with new IDs. Curves, polygon holes,
empty geometries and invalid perimeters are rejected with the source feature ID;
an unsuccessful import leaves no partial model layer. The original layer is
unchanged. The copy retains the original layer name, feature ID and all source
attributes in `source_layer`, `source_fid` and the JSON `source_attributes` field.
Only explicitly mapped values become active Acalc properties; measurement levels,
materials and other acoustic properties can be assigned afterward.

The imported copy is saved immediately and opened in its Acalc view. Add it to
the appropriate variants before calculation, and assign spectra to any sources
that still need them. Subsequent source-layer changes do not update the copy.

To download building outlines directly, use **Project → Import OSM buildings…**
beside **Import layer…**. Choose the current map extent, draw a rectangle on the
map, or enter a WGS84 extent in the dialog. The requested area must be no larger
than **100 km²**. Choose a name for the new building layer and click **Download buildings**
to review the building count and any skipped objects before **Import buildings**
saves the layer. Downloads use the public [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API)
through QGIS's network settings, can be cancelled, and may fail when the service
is unavailable or busy. Downloading alone does not change the model.

The import can use an explicit OSM `height` in meters or feet. Missing or unusable
heights use the editable default, initially **6 m**; `building:levels` is not
converted into height. Alternatively, disable OSM heights to apply the chosen
default to every building. Choose a constant ground elevation (the default) or
elevations sampled from a raster. The raster option
uses the same raster selector and sampling procedure as **Take elevations from
raster…** in an individual building's properties. Select a raster file directly
or choose a file-backed raster already loaded in QGIS. Every perimeter vertex must have a valid sample
before any buildings are saved. The sampled Z values are stored in meters as
explicit per-point ground elevations, retaining each building's relative height.
The raster is only read during import; changing it later does not update the
buildings. Raster sampling does not transform vertical datums, so use elevations
in the model's vertical datum. Review these height
and elevation assumptions before calculating; OSM outlines do not supply surveyed
ground levels or acoustic material properties. The [OSM height documentation](https://wiki.openstreetmap.org/wiki/Key:height)
describes the source tag's units. Imported buildings use **Relative to average Z**:
the roof is flat at the average perimeter ground elevation plus the imported
height. OSM roof forms are not read.

Closed building ways and assembled multipolygon outer rings are supported.
Objects with holes, invalid or unclosed geometry are skipped with visible details;
explicitly elevated, underground and roof-only structures are also skipped.
Legacy multipolygons without a building tag are reported and skipped with their
member ways, so courtyard boundaries are not lost.
Objects tagged only as building parts are not imported. Footprints are retained
whole, including portions outside the requested rectangle. X/Y coordinates are
transformed into the model CRS. The saved copy retains source IDs, tags and
**© OpenStreetMap contributors** attribution with the [copyright link](https://www.openstreetmap.org/copyright)
and [ODbL 1.0 license](https://opendatacommons.org/licenses/odbl/1-0/).
Add the new layer to the appropriate variants before calculating. It is a saved
snapshot; later OSM changes do not update it.

Project details have an explicit **Save project details** button. Opening or
closing a model through Acalc prompts to save/discard pending project details, calculation parameters, and layer
edits. Save project details before unloading the plugin, closing QGIS, or creating
a different QGIS map: QGIS itself only prompts about its own layer edits.

The Project page keeps an activity log. **View log…** opens a larger window;
both views retain earlier messages and update as you work. Other metadata (JSON)
is at the bottom of the page.

To open properties from the map, **right-click a visible model feature → Acalc
properties** with a tool that shows QGIS's map context menu, such as **Pan Map**.
When features overlap, the submenu lists their names and layers so you can
choose the intended object. It works across visible model layers, regardless of
which layer is active. Merely opening or dismissing the menu does not change the
selection or data. QGIS's **Identify** feature action menus also offer **Acalc
properties**. Digitizing tools retain their normal right-click-to-finish behavior.

The dialog's **Coordinates** tab edits ground Z and per-vertex height;
point, line, and area sources also have a **Spectrum** tab for selecting and previewing a project
sound power spectrum.
Buildings are passive objects with no attached sources. Source Buildings represent
a source in the UI and can contain many point and area component sources. Both
remain solid obstacles for screening, reflection, terrain, and receiver-grid exclusion.
Their properties show an interactive **3D building preview** beside the
coordinate table. Drag to rotate, middle-drag or Shift-drag to pan, scroll to zoom,
and double-click or choose
**Reset view** to fit the building again. Coordinate and height edits update the
preview when committed with Enter, Tab, or by clicking away; typing alone does
not change it. Selecting a vertex row highlights its wall edge. The
preview respects relative and absolute per-point heights and all four shared-roof modes,
including horizontal CRS unit conversion. Incomplete or invalid coordinates
show an inline message until corrected. Ground Z must be explicit for a preview;
sampling a raster can supply it.
Regular Buildings have no selectable faces.
In **Source Building** properties, click a wall or roof face to highlight it, then
choose **Sources**. Add one or
more rectangular area sources or point sources and assign a project sound power
spectrum. Point sources use total sound power and have a **Distance from surface**
setting, measured perpendicular outward from the wall or roof. Area sources use
total or per-square-meter power and have width/height. Both use X/Y offsets from
the surface's top-left corner, in the displayed length units. The
source editor includes a 2D face view above the coordinate table. The source
being edited is highlighted in orange; other sources on that face stay visible
in grey. Click a source in the drawing or select its table row to edit it;
position and size edits update the drawing immediately. The view shows the true
face outline, a grid in CRS units and the 0, 0 origin, including sloped facade edges.
Drag anywhere inside a source to select and move it without changing its size.
Use its bottom-right handle to resize it while keeping its top-left position.
Dragging snaps to 0.01 of the displayed length unit; area dimensions remain
at least 0.01 of that unit. Metre fields show two decimals; converted units retain
extra precision. Escape cancels an active drag. Tabbing into a coordinate selects
its value for replacement. Octave cells stay blank when no spectrum is assigned;
unknown bands in an assigned spectrum still show **Unknown**.
An out-of-bounds rectangle is marked while editing and must be corrected before
applying. In the building's 3D preview, the
selected face is blue; sources are orange. The marked **0, 0** is the face origin.
Wall offsets run right and down when viewed from outside. The building's first
perimeter vertex and first edge define its local coordinate system; roof axes
follow a top edge selected in that frame. Moving or rotating the building carries
its locally attached sources with it. Raising a wall's left roof vertex moves
its sources by the same amount, retaining their displayed offsets. Nonplanar
roofs expose separate triangular facets with identities tied to authored vertices.
Older roof sources retain their saved axes until **Apply sources** adopts the
local axes shown in the editor. Rectangles must fit entirely in their
face. Source edits remain drafts until **Apply to layer**, and use the building's
save, cancel, duplicate and undo workflow. Remove attached sources before adding,
removing or reordering perimeter vertices; coordinate/height changes retain the
attachments and recheck their fit. If an older roof facet is unavailable, use
**Unattached sources…** to reassign its sources or remove them from the draft.

The Source Building layer's variant membership includes its component sources in
calculations. Editing summaries count each Source Building once. Running and
saved calculation summaries count its individual emitting components as sources
and its building as an obstacle.
Rectangles and points use the existing omnidirectional source solvers; attachment
does not imply transmission, half-space directivity, or an extra facade correction.
Existing unsupported propagation paths still fail explicitly.
This viewer uses Qt without requiring a separate 3D module or service.

**Ground absorption areas** are 2D polygons with a name and a single **G** value
between 0 (hard ground) and 1 (porous ground). Draw their perimeter on the map or
enter X/Y coordinates; no Z or height is required. They support the normal edit,
duplicate, undo, and save workflows. Areas included in a variant override the
default ground factor inside their polygons; uncovered ground uses the default.
Project validation reports overlapping areas by name and layer. Calculation blocks
if areas included in a selected variant overlap; shared edges and vertices are allowed.

**Trees** marks dense woodland with a drawable 2D polygon. Choose **Terrain → Trees** from
the main menu, create a layer, and use **Draw on map** or **Add coordinates**.
Set **Tree height** above local terrain (initially 10 m, an editable application
default), then include the layer in the calculation variant. Use these areas
only for vegetation dense enough to block the view through it; record seasonal
conditions and supporting observations in Notes.

Calculations use the simplified foliage method in ISO 9613-2:2024 Annex A.2.2.
Paths above the canopy receive no foliage loss. Overlapping Trees areas count
their shared volume once. Trees do not change ground factor G: add a ground
absorption area separately when appropriate. The detailed forest-parameter
method is not implemented. See the ISO 9613-2 engine's documentation of its
Trees calculation scope for path assumptions and verification limits.

Other line and polygon objects have three **Ground points** options: **Specify per point** enables
every Z cell; **Uniform** enables only the first point's Z and copies its committed
value to all other points; **Infer from terrain** disables every Z cell and
resolves elevations from the variant's included object layers at calculation time. Uniform also applies to newly added
points. Switching back to Specify per point keeps the current values, which can
then be edited independently. The selected mode is retained when the object
is saved and reopened. Point objects offer **Specify** and **Infer from terrain**.

**Apply to layer** places changes in the QGIS edit buffer, with undo support;
**Cancel** leaves feature data unchanged. Use **Save layer edits** to commit.
Existing buildings also have **Go to Building**, which applies changes to the
edit buffer, closes the window, selects the building, and zooms to it on the map.

Existing objects have **Delete** and **Duplicate** buttons in their properties
window, also available by right-clicking an object in the dock's feature list.
**Delete** asks for confirmation. **Duplicate** immediately adds a copy to the
same layer and opens its properties. It copies the object's current layer
attributes and geometry (including pending layer edits), assigns a new object ID,
and appends “ copy” to its name; unapplied changes in the original properties
window are excluded. Canceling the new properties window keeps the copy. Both
actions support QGIS undo and remain pending until **Save layer edits**.

The same right-click menu offers **Move to layer**, listing other model layers
of the same object type. Moving preserves the object's ID, properties, and
geometry, including pending edits, and selects it in the destination layer.
Save edits to **both** layers to keep the move; to undo it, undo once in each
layer. The command is disabled when no matching destination layer exists.

While drawing, **Esc** cancels the unfinished sketch. Switching tools or layers,
stopping layer editing, closing the model, or unloading Acalc cancels pending
capture. Each accepted feature is one undoable addition; undo/redo never opens a
properties dialog. Acalc's Draw workflow does not change layer form-suppression
settings or QGIS's global attribute-form preferences.

## Project spectra

Open **☰ → Spectra** for the project's **Sound power** and **Absorption** catalogs.
Each tab lists its spectra and shows whether bands are complete or still unknown.
Use **New**, **Edit** (or double-click), **Duplicate**, and **Delete** to manage
records. The editor includes a name, reference/notes, and eight octave bands from
63 Hz to 8 kHz. **Save spectrum** writes immediately to the GeoPackage; **Cancel**
leaves it unchanged. New projects start with two empty catalogs.

Sound power is entered as unweighted levels in **dB** only. Choose **total sound
power**, **sound power per meter**, or **sound power per square meter** in the
spectrum editor. Point sources accept total power; line sources accept total or
per-meter power; area sources accept total or per-square-meter power. Total power
is distributed uniformly over a line’s acoustic length or an area’s emitting
surface. Density levels gain `10 log10(length)` or `10 log10(area)`.

**Enter 0 for no energy.** This is the model's sound-power entry convention.
Negative nonzero dB levels remain valid. Blank means unknown. Absorption
coefficients range from 0 to 1, with blank meaning unknown. Adjustable sound
power spectra have separate **Current sound power** and **Initial guess** rows.
Current bands may be blank before the first fit; ordinary calculation requires
complete current bands. The initial guess stays in place when a fit updates
current sound power.

All band editors and previews are horizontal. Select cells and use **Ctrl+C** or
**Ctrl+V** to exchange a row of values with a spreadsheet; a pasted column is also
accepted. **Tab** advances through band values while editing.

Assign a sound power spectrum in a point, line or area source's **Acalc properties →
Spectrum** tab. Assignment participates in layer undo/redo and **Save layer edits**.
Editing a catalog record preserves its ID and updates every source using it across
all variants. Duplicate the spectrum to make an independent alternative.
Referenced records cannot be deleted, including references from removed layers.
Finish relevant object edit sessions before deleting records so undo cannot
restore a deleted reference.

Assign an **Absorption spectrum** in a building or wall's properties. The eight
coefficients apply to every exterior vertical building face, or both sides of
the wall. The preview shows octave values and the material reference. Assignment
uses the same Apply/Cancel, undo/redo and layer-save workflow as other properties.
The assigned spectrum overrides the single absorption value; clearing it
restores the stored override or calculation default. Absorption 0 means
fully reflecting and 1 means fully absorbing. Every reflected bounce uses the
appropriate octave value. Individual face/side assignments remain future work.

**Validate model** reports missing assignments and unknown bands on included
sources, and invalid or incomplete absorption spectra assigned to reflecting
objects. Drafts can still be saved and exported as variant definitions;
an exported definition does not imply readiness for calculation. Exports include
both project catalogs, along with the relative GeoPackage reference.

### Directionality authoring

Choose **Acalc menu → Directivity** to manage the project's local library.
Each definition has a name, notes, principal direction, level reference, and an
editable table of individual azimuth/elevation directions against frequencies.
Add/remove directions and frequencies, paste rectangular spreadsheet cells, and
inspect the selected frequency in the orbitable 3D preview. A blank is unknown;
0 dB is a real directional level, not silence. The preview uses nearest-direction
sampling and an illustrative radius; it does not normalize or calculate propagation.

**Use a template** opens a separate input/review dialog. Revolve a 2D profile
through 360° with dB or energy interpolation, or generate three angular sectors.
Both produce editable tables and retain their original inputs. Edits mark the
generated table as modified. The chimney/stack form retains diameter, exhaust
temperature, the 2024 Annex B method identity and departure-angle rules as a
draft with unknown values; numerical Annex B generation is not implemented.

**Import table** accepts native definition JSON and CSV/TSV. Text tables start
with `azimuth_deg,elevation_deg,63,125,...` (numeric frequency headings in Hz).
**Attach CLF** retains original CF1/CF2 bytes and a SHA-256 checksum inside the
project, up to 10 MiB. It explicitly does not decode those binaries or infer any
directional values. **Export definition** writes portable versioned JSON.

In **point-source properties → Directionality**, select a saved definition,
aim its principal Up/Down/North/South/East/West, then apply incremental rotations
about the fixed project Z, east–west or north–south axes. North means grid north
(+Y), not geodetic true north. Rotation order matters; the full rotation matrix
preserves roll. Orbiting the preview changes only the camera. Source assignments
and orientation participate in Apply/Cancel, layer undo/redo and Save layer edits.
Library edits are shared and save immediately, even if source editing is cancelled.
Referenced definitions cannot be deleted, including references in archived layers;
point-source edit sessions must be closed before deletion so undo remains safe.

This release integrates directionality **authoring**, not directional propagation.
The plugin and Rust engine reject calculations with assigned directional point
sources, preserving prior results. Unassigned definitions do not affect calculations.

The model uses schema 15. Opening a schema 1–14 project first writes a separate
`*.schema-N-backup-*.gpkg` beside it, then upgrades it transactionally.
Geometry, IDs, layer membership, and the latest results per variant are preserved.
Schema 14 separates Buildings from Source Buildings. Existing building layers with
any attached sources, including archived layers, become Source Buildings; layers
without sources stay Buildings. A mixed layer moves as a whole, preserving all
objects, geometry, source assignments, IDs, and variant membership. The upgrade
creates a backup before making changes.

Schema 13 declares each building-mounted point source as a surface or standoff
point, following the distance rule that previously selected its convention
(0 = surface, at least 0.05 m = standoff). Points between 0 and 0.05 m stay
undeclared and must be reviewed in the face-source editor before calculating.
Version-1 calculation settings keep their atmosphere, ground, meteorology, C0 and
reflection values; controls of the retired in-plugin solver are dropped and the
numerical settings take their defaults.
Z inference is removed, including saved flags and API requests. Legacy objects
whose ground Z was a placeholder are marked unresolved and require explicit
elevations before saving, calculating or QSI export. Raster sampling and fixed
layer Z tools can supply these elevations. Area-receiver grids retain their
height above the explicit terrain surface; they have no inference flags.
Schema 10 upgrades add empty building face-source assignments.
Schema 8 upgrades add the directionality catalog and empty point-source assignments.
Schema 7 upgrades also add empty absorption assignments without changing scalar
reflection settings, spectra or terrain. Older plugins/engines reject schema 15;
use the migration backup if needed. For older schemas, existing
watt spectra convert to dB; old silence markers become 0. Numeric zero in a
sound-power spectrum now always means no energy. Calculation parameters move to
the shared model settings. If previous configurations had different parameter
sets, the Calculate window offers those sets for review and selection before
running. Elevation groups and their caches are removed. Raster references and
saved object coordinates are retained. Terrain now comes only from a variant's
included layers; review older variants that relied on ground outside those layers
or on raster surfaces. Sample those rasters onto objects if needed. Older plugins
can use the backup. Re-export variant JSON after upgrading.
See the [schema contract](GEOPACKAGE.md).

## Calculate receiver levels

**Measurements** are point receivers with manually entered sound-pressure
levels. Create a Measurements layer, add a point, then enter octave levels on
its **Measured levels** tab. dBA updates as each band is entered and is
rounded to one decimal place. With no
octave levels, dBA can be entered manually. Blank values mean unknown;
numeric 0 is a measured 0 dB level. Use Notes for conditions, equipment and
provenance. Include the layer in a variant to see its observations beside
calculated receivers in **Results**, even before the variant has been calculated.
Included Measurements also act as normal point receivers during calculation.
Engine results are stored separately for each variant and never overwrite the
entered measurements. The **Measured vs calculated** properties tab compares
measured and calculated dBA and every octave band, with a signed difference
(calculated minus measured). Positive differences mean the calculation is higher;
blank differences mean a measurement or calculation is unavailable.

Use **Back-calculate source power…** on the Calculation page to fit sound-power
spectra from the included Measurements. Mark the source spectra **Adjustable** in
the sound-power editor and enter their separate initial guesses; current values
may remain unknown. Locked spectra must have complete octave values and remain
fixed background contributions. Sources sharing a spectrum are fitted together.
Each octave needs at least as many independent Measurements as adjustable spectra;
broadband-only observations cannot determine an octave spectrum.

Select an engine advertising `source_receiver_transfers` (use **Test connection**
after updating the engine), choose the variant, and **Evaluate fit**. Evaluation
uses the general octave-band ground method, with pruning and absolute integration
tolerance disabled, and leaves saved forward results and spectra unchanged.
Review the fitted rows and RMS level error, then **Apply fitted spectra** to save
current values while retaining initial guesses. Applying marks forward results out
of date; changed model inputs or Measurements require a fresh evaluation. APIs
without the transfer capability remain available for forward calculations only.

For a forward calculation of receiver levels:

1. Open **☰ → Settings** and select an engine connection. **Local engine**
   connects to `http://127.0.0.1:8343` without a key. Use the adjacent **☰** menu:
   **New engine…** or **Edit engine…** opens the connection editor; **Delete
   engine…** asks for confirmation. For a remote instance, choose **Remote**,
   enter a name, URL and client key, then **Save**. The key is stored only in
   QGIS’s encrypted authentication database; connections belong to the QGIS
   profile, never the project. **Test connection** works both here and in the
   editor: it checks reachability, health, access and each installed engine’s
   capabilities. The list shows one row per method/engine and its installed
   version; unavailable, unsupported and unknown versions are identified.
   Detailed capabilities remain cached for input validation. The editor tests
   draft values without saving them. **Cancel** preserves the saved endpoint,
   key and previous checks. Changing a remote URL requires explicitly entering
   a key for that endpoint. Internal node URLs are rejected; a local engine
   requiring a key offers Remote mode at the same URL. Remote connections support
   HTTP on the LAN and HTTPS for public services.
2. Include saved source and receiver layers in a variant and assign complete
   sound-power spectra. The plugin converts the complete selected model to
   metric JSON: project X/Y units, ground Z plus acoustic height M, material
   assignments and the model's zero-as-silence spectrum convention are preserved.
   Unsupported effects stop conversion with an incompatibility message.
3. Open **☰ → Calculation**, then **Calculate**. Choose **Engine** in
   the calculation window; connection management remains on **Settings**.
   Each choice shows the connection name and discovered engine. Choose variants
   and review **Calculation Parameters**. Ground method, environmental inputs,
   reflection order, pruning and source-integration controls are saved with the
   model. **Pruning** is off by default. When switched on, choose whether it is
   based on dBA or dBC: as the calculation progresses, paths not expected to
   raise that overall level at any receiver are abandoned. Thresholds are set
   automatically; overall levels stay very close to an unpruned calculation, but
   individual octave bands may lose accuracy. Projects that had the former
   numeric cutoff switched on open with dBA pruning.
   Connection Settings contain only connection/discovery information.
   On the Calculation page, drag the divider below the variant tree to give
   the scrollable messages more space. **Copy messages** copies the full message
   history shown there, including error details. The divider position is remembered.
   **Check inputs** checks local model conversion against freshly fetched API
   capabilities. The service checks propagation geometry and resource limits
   during calculation. Starting saves parameters and offers to save pending
   layer edits. Variants run sequentially. The run display shows the current
   phase, percentage, source and receiver counts, source–receiver pairs, and
   elapsed time. Receiver-grid counts update after API preparation. Each
   completed variant retains its duration; the final display retains total
   run time. Progress reaches 100% after all selected variants complete and
   their results are saved.
   **Cancel calculation** requests API cancellation and skips remaining variants.
   Failed/cancelled work retains earlier results. A server-confirmed completed
   job may still be saved when completion races with cancellation.
4. Open **Results** for octave sound pressure levels (63–8000 Hz) and dBA levels. Calculated octave and broadband levels are displayed to one decimal
   place; the returned API precision is retained in saved results. Each
   variant has one saved result set inside the GeoPackage.
   Calculating again replaces its results; earlier runs are not retained.
   Model changes mark results out of date. Zero energy displays separately from
   0 dB. Double-click a receiver to
   open its existing properties modal on the **Results** tab. Choose a variant
   to inspect its current octave and broadband results. This tab is also
   available when opening point or area receiver properties from the object list
   or map. An area receiver appears once in the results list, with blank level
   columns. Its **Results** tab shows the saved area surface. Hover over the area
   to see the nearest calculated point's dBA and eight octave levels in a single
   row; a cross marks that point. Choose **Filled areas** or **Contour lines**.
   Both displays use 5 dB intervals. Hover values come from the saved area
   raster's pixel under the cursor (older runs: the nearest saved sample).

   Point receiver layers get read-only point result layers with dBA labels and
   octave fields. Each area receiver instead gets one nine-band raster surface per
   variant in **Results → variant name**, named with the variant, receiver layer,
   and area. No generated area points are added to the map or results list.
   Its bands are dBA and the eight unweighted octaves from 63 to 8000 Hz.
   Right-click a surface → **Area result display** to switch between filled bands
   and contour lines, choose **Sound level** to select a band, or choose
   **Acalc properties** to open the parent area's results. The area preview also
   has a band selector. Export the layer as GeoTIFF to retain all nine bands.
   The calculation engine delivers each area as a GeoTIFF, downloaded separately
   after the run's results so large grids do not slow the result transfer. Acalc
   saves it beside the model in `<model name>_results/<run ID>/` with the model's
   CRS and adds it to the project; keep that folder with the `.gpkg` when moving a
   model. Individual source contributions are retained only for point receivers.
   Raster cells are centred on the calculation grid. Excluded/missing samples and
   zero-energy samples are transparent and are not reported on hover. (Runs saved
   by older versions keep their samples in the GeoPackage and still report zero
   energy as −∞.)
   Contour lines interpolate the saved grid; they are a display of the calculation,
   not additional calculated receivers or added accuracy.

   Map results use shared data sources in the GeoPackage. Selecting layers, editing
   the model, or browsing the results panel does not update their names or data.
   When calculation finishes, Acalc updates a matching default-named results
   layer, retaining its style and visibility, or creates one if none exists.
   Renaming or duplicating a results layer keeps it connected to the same data.
   Every layer referencing those results receives recalculated values, including
   duplicates outside Results, while keeping its own name, styling, and visibility.
   Renaming and duplicating do not create data snapshots. Unrelated layers are
   left alone. Save the QGIS project to retain layer names and styles between
   sessions. Area rasters also update their calculated extent and cell size.
   Empty areas retain an empty surface and one list entry. **Refresh saved results**
   explicitly publishes saved calculations, including those made outside QGIS.
   Saved area boundaries and spacing
   survive later input edits; legacy areas with insufficient saved grid information
   need recalculation. Receiver layers remain editable inputs.

**Calculation scope follows the selected API and the frontend adapter.**
The adapter targets ISO 9613-2:2024 with ISO 9613-1:1993 atmospheric absorption.
Services advertising schema-11 terrain construction can receive selected building
solids, attached sources, lines, terrain observations and breaklines, reflecting
barriers, ground regions, foliage and receiver grids, subject to their individual
capabilities. The schema-11 terrain profile is required.
Directional emission remains unsupported by
the frontend. Conversion does not establish that a service can calculate the
whole scene. The engine evaluates the selected ground method on irregular
terrain without flattening it; method applicability remains the assessor’s choice.
**Check inputs** checks local conversion; service preparation
and propagation can still reject a scene. Ground methods are never switched
automatically to bypass an applicability restriction.
See the full restrictions in [API.md](API.md) and the discovered
capabilities. Implementation support does not establish acoustic conformity;
the [engine's verification notes](https://github.com/coloradoanalytics/engine-iso#verification)
describe independent evidence and gaps.

The plugin submits immutable JSON snapshots, polls the returned job links and
writes only completed results in one GeoPackage transaction. Run metadata keeps
submitted inputs, API identity/version, capabilities, settings, variant membership
and reversible object IDs. Changed inputs prevent a completed job from replacing
results for a different revision. A completed empty receiver grid deliberately
replaces the variant's results with an empty set. The API never receives a local
GeoPackage path.

### Full integration reports (optional)

Enable full extended-source integration reports only for compliance evidence or
debugging on a specific run. Result size and memory use grow with the number of
integrations (roughly 1 KB per integration; large models can produce many gigabytes).

1. Open your model in the **Acalc** panel, open the **☰** menu, and select
   **Project**. Under **Project details**, find **Other metadata (JSON)**.
2. Manually add `"keep_integration_reports": true` inside the existing JSON object.
   If the field is empty or contains only `{}`, enter this complete object:

   ```json
   {
     "keep_integration_reports": true
   }
   ```

   Keep any existing metadata entries and separate entries with commas. Use the
   JSON boolean `true` without quotes around it; the string `"true"` does not enable
   reports. The plugin never inserts, defaults, or pre-fills this optional key.
3. Click **Save project details**, then start the intended calculation. The selected
   engine must advertise `integration_reports` in its capabilities, or the plugin
   refuses the run with an explanation.

The flag remains enabled for subsequent runs until you remove the key or change
its value to `false` and click **Save project details** again. Do this after the
intended run to return to summary-only reports; absent or `false` leaves the engine's
summary default in place. The exact submitted request saved with each completed
run records when full reports were requested.

## Geometry and layers

| View | Geometry | Vertex coordinates | Style |
| --- | --- | --- | --- |
| Point sources | PointZM | X, Y, ground Z, height M | Orange circle |
| Line sources | LineStringZM | X, Y, ground Z, height M | Amber line |
| Area sources | PolygonZM | X, Y, ground Z, height M | Amber fill |
| Point receivers | PointZM | X, Y, ground Z, height M | Blue diamond |
| Measurements | PointZM | X, Y, ground Z, height M; manually entered levels | Teal triangle |
| Area receivers | PolygonZM | X, Y, ground Z; shared height and spacing | Translucent blue fill |
| Barriers | LineStringZM | X, Y, ground Z, height M | Magenta line |
| Source Buildings | PolygonZM | X, Y, ground Z, height M; component sources | Translucent orange fill |
| Buildings | PolygonZM | X, Y, ground Z, height M | Translucent brown fill |
| Terrain points | PointZ | X, Y, ground Z | Green point |
| Terrain lines | LineStringZ | X, Y, ground Z | Dashed green line |

Area receivers have one polygon perimeter, a height above ground (default
**1.5 m**) and point spacing (default **10 m**). Their regular grid is aligned
with the project X/Y axes and starts at the polygon bounding box's minimum X/Y.
Points on the area perimeter are included; points outside it are omitted.
Each point samples the final ground surface and adds the polygon's shared height.
Spacing inputs follow the CRS units; heights follow the separate elevation unit setting. Both remain stored in meters.
Every building in the selected variant excludes grid points inside or
on its footprint, including points above its roof. Buildings in excluded layers do not affect the grid. Concave perimeters are supported; holes and
rotated/custom grid origins are not yet supported. A variant is limited to
one million candidate grid points; increase spacing for larger areas.
If no grid points remain, calculation saves an empty result layer and removes
the previous points. Generated points never become editable input features.

**Z is the ground, not the elevated object.** For elevated objects, absolute
elevation is `Z + M`, in meters relative to the recorded vertical datum. Each
vertex has its own values. Buildings have one perimeter that closes automatically.
The **Height mode** dropdown offers **Relative to average Z**, **Relative to
highest Z**, **Relative to lowest Z**, **Absolute**, **Relative per point**, and
**Absolute per point**. The first three add H to the selected ground reference
to give a flat roof. Absolute uses H as the roof elevation directly. These four
modes enable H and display the resulting roof elevation in inactive table cells.
Per-point modes disable H and enable table entry of either a height above each
ground point or an absolute roof elevation. Ground-point rules remain independent.
A roof below any ground vertex is rejected; negative absolute elevations are
allowed. The arithmetic average uses each perimeter vertex once, excluding
closure. Legacy geometry with interior
boundaries is preserved during migration; replace that footprint with a perimeter
polygon before editing it in the simplified editor.

Barriers offer **Relative to average Z**, **Relative per point**, and **Absolute per point** height modes. Nominal height sets the common top above average ground Z, or fills each relative point height (which can then be edited individually). Absolute per-point values specify top elevations. New barrier points use the nominal height; saved models retain the mode and nominal height.

**Terrain → Points** and **Lines** contain the terrain object editors.
Terrain is determined by the object layers included in each variant. All specified
ground vertices in those layers define the calculation surface, with terrain
lines as breaklines. Object Z inference is unavailable. Object heights never become ground
samples. Area receiver boundaries, ground absorption areas, and Trees
do not contribute ground samples. Measurements contribute like point receivers. There are no elevation groups, separate terrain membership, or priority
settings. Conflicting elevations at the same XY are rejected.

**Terrain → Generated terrain** lists terrain saved in the model GeoPackage.
Choose **Generate terrain from raster…**, select a raster file or a loaded QGIS
raster, then **Draw box on map** to
mark the area to import. The area menu also accepts coordinates or the current
map extent. Enter a new terrain-layer name and **Maximum elevation deviation**
(default 0.5 m, displayed in the project's elevation units), then choose
**Generate terrain**. Generation runs in the background and can be cancelled.

The importer starts with the box corners, triangulates in XY, and inserts the
worst-fitting raster samples in batches until the maximum checked elevation error
meets the tolerance. Checks include native pixel centres inside the box and
bilinearly sampled boundary points at approximately half-pixel spacing. Smooth
planes need few vertices; berms and changes in slope retain more detail. The
reported tolerance measures agreement at these samples, not continuous-surface
error, raster survey accuracy, or a sound-level error bound. The import requires
complete elevation coverage: interior NoData pixels, missing boundary samples,
and boxes outside the raster fail before a terrain layer is saved. Resource
limits are 1,000,000 candidate samples and 20,000 retained vertices; exceeding a
limit requires a smaller area or (for the vertex limit) a larger tolerance.

Each generation creates and opens a new layer in **Terrain → Generated terrain**,
saved in the model GeoPackage. Use the layer dropdown and **Layer commands** menu
(**New layer…**, **Rename layer…**, **Delete layer…**) as on other layer tabs.
The selected layer's mesh appears in the list below, with its area, vertex/triangle
counts, achieved deviation, and import details. **Zoom to mesh** shows it on the map.
Meshes never appear in the Lines editor. Opening one from QGIS or a variant returns
to Generated terrain. Their stored triangle edges preserve the checked surface
for calculation. Add the layer under **Generated terrain** in **Variants** to use it.
Deletion excludes the layer from all variants; use **Project → Restore layer** to
recover it. Each generation starts a new import, even when using the same raster.
Select the appropriate layers in each variant. The raster is only an input to
generation: it is not registered with the model or added to QGIS, and cancellation
or failure adds nothing to the list.
Other included ground points and breaklines still participate in the terrain;
conflicting elevations are rejected by the existing surface contract. Terrain
outside the selected box follows the engine's usual nearest-boundary extension,
so select an area covering the propagation paths you need.

Use bare-earth data in the model's vertical datum. Horizontal coordinates are
transformed from the raster CRS to the model CRS, and GDAL scale/offset and
vertical units are applied. Missing vertical units mean meters; vertical datums
are not transformed. Raster source, band, sampled-data fingerprint, CRS, area,
tolerance, algorithm, and generation statistics are saved with each generated
layer. This is historical provenance; manual edits do not recompute the original
error report. No live source-raster association is needed.

Choose a raster file directly for **Take elevations from raster…** in object
properties or **Set Z values…** on a layer. These authoring actions use bilinear
interpolation, replace explicit ground Z, retain X/Y and height choices, and
support the existing edit-buffer undo/cancel workflow. Unlike terrain generation,
object sampling renormalizes valid neighbours around NoData. Rasters are never
read during calculation: changing or deleting the source file cannot change
saved ground coordinates. Generate a new terrain layer or explicitly resample
objects to use changed elevations. See the
[surface contract](GEOPACKAGE.md#explicit-ground-and-terrain-construction).

**Remove layer** removes it from the active model and every variant.
Its saved feature table stays in the GeoPackage. **Project → Restore layer**
recovers it; variant membership must then be chosen again. Removing a layer
using QGIS's layer tree only unloads it from the map. Acalc's **Reload layers**, or
selecting that layer in the dock, reloads it. Renaming through QGIS also updates
the model's display name. Unrelated QGIS layers remain untouched when a model closes.

Saving a normal `.qgz` map stores a reference to the Acalc `.gpkg`. With the plugin
enabled, reopening the map reconnects its model and reuses existing layers instead
of duplicating them. The `.qgz` is optional and is not the acoustical model file.
An empty new model contains one empty **Default** variant and no model layers.

## Export to CadnaA

Use **Calculation → Export…** to choose a variant and export either a CadnaA
2021 QSI package or authored GIS shapefiles. QSI includes compatible acoustic
attributes and spectra; raw GIS layers need setup in CadnaA. Export writes a new
folder with import instructions and a settings/identity report. See
[CadnaA export support and verification limits](CADNAA_EXPORT.md), including
the requirement for explicit ground elevations and the 1996/2024 ISO edition
difference. Actual import into CadnaA 2021 is awaiting verification.

## Scope and limitations

Model authoring remains broader than calculation support. Individual face/side
material assignment and operating states remain
future work. No source emission or material values are invented.

Use **Validate model** after editing with external tools or normal QGIS tools.
QGIS's own save commands do not run the Acalc validator; export does. Acalc supports
one open model per dock, one projected CRS per model, and a shared layer geometry
for every variant that includes it. Scenario-specific geometry belongs in
separate layers. Imports from other noise software and permanent purging of
removed layers are not implemented.

The normative [GeoPackage schema](GEOPACKAGE.md) describes engine-independent
readers and writers, and the [calculation API](API.md) describes the HTTP contract
between the plugin and calculation engines.
