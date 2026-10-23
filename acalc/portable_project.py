# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Native model adapter for Core's hierarchy v2. No numerical transformations.

Unowned JSON is retained, including numeric spellings. Native files are opaque;
only runtime raster lookup is rebound after extraction. Core owns the envelope.
"""

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import uuid

from .schema import ModelError, SCHEMA_VERSION, new_id

MAX_BYTES = 32 << 20
NAMESPACE = "acalc-qgis"
EXTENSION = ".acalc-project.json"


class Number:
    """An authored JSON number, never converted through binary floating point."""
    def __init__(self, text):
        self.text = text


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ModelError(f"Duplicate project JSON member: {key}.")
        result[key] = value
    return result


def _constant(value):
    raise ModelError(f"Nonfinite project JSON number: {value}.")


def decode(raw):
    if len(raw) > MAX_BYTES:
        raise ModelError("Portable project exceeds the 32 MiB limit. Reduce its file assets.")
    try:
        return json.loads(raw, parse_int=Number, parse_float=Number,
                          parse_constant=_constant, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ModelError(f"Invalid portable project JSON: {error}") from error


def encode(value):
    def render(item):
        if isinstance(item, Number):
            return item.text
        if isinstance(item, dict):
            return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + render(v)
                                  for k, v in item.items()) + "}"
        if isinstance(item, list):
            return "[" + ",".join(render(v) for v in item) + "]"
        return json.dumps(item, ensure_ascii=False, allow_nan=False)
    try:
        result = (render(value) + "\n").encode("utf-8")
    except (ValueError, TypeError, RecursionError) as error:
        raise ModelError(f"Cannot encode portable project: {error}") from error
    if len(result) > MAX_BYTES:
        raise ModelError("Portable project exceeds the 32 MiB limit. Reduce its file assets.")
    return result


def _integer(value):
    if isinstance(value, Number):
        try:
            return int(value.text)
        except ValueError:
            pass
    if type(value) is int:
        return value
    raise ModelError("Expected an integer in the portable project.")


def _relative(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ModelError("Portable file paths must be safe relative paths.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("..", ".", "") for p in value.split("/")):
        raise ModelError("Portable file paths must be safe relative paths.")
    return path


def _under(root, name):
    relative = _relative(name)
    path = (Path(root) / str(relative)).resolve()
    if Path(root).resolve() not in path.parents:
        raise ModelError("Portable file escapes its project directory.")
    return path


def _bytes(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ModelError(f"Missing or oversized portable file: {path.name}.")
    with path.open("rb") as stream:
        value = stream.read(MAX_BYTES + 1)
    if len(value) > MAX_BYTES:
        raise ModelError(f"Portable file exceeds the 32 MiB limit: {path.name}.")
    return value


def file_bytes(record, directory):
    choices = [key for key in ("data", "path", "missing") if key in record]
    if len(choices) != 1:
        raise ModelError("Each portable file needs exactly one byte source.")
    if choices[0] == "missing":
        raise ModelError(f"Portable file is missing: {record.get('name', record.get('id'))}.")
    if choices[0] == "data":
        try:
            value = base64.b64decode(record["data"], validate=True)
        except (ValueError, TypeError, binascii.Error) as error:
            raise ModelError("Invalid base64 portable file data.") from error
    else:
        value = _bytes(_under(directory, record["path"]))
    if len(value) != _integer(record.get("size")) or hashlib.sha256(value).hexdigest() != record.get("sha256"):
        raise ModelError(f"Portable file size/hash mismatch: {record.get('name', '')}.")
    return value


def validate(document):
    """Validate the adapter boundary; the generated Core schema is authoritative."""
    if (not isinstance(document, dict) or document.get("format") != "acalc-project"
            or _integer(document.get("schema_version")) != 2):
        raise ModelError("Choose an Acalc hierarchy version 2 project file.")
    project = document.get("project", {})
    if not isinstance(project, dict) or not isinstance(project.get("info"), dict):
        raise ModelError("Portable project info is missing.")
    if not isinstance(project["info"].get("project_id"), str) or not project["info"]["project_id"]:
        raise ModelError("Portable project identity is missing.")
    for key in ("rooms", "measurements", "calculations", "models"):
        if not isinstance(project.get(key), list):
            raise ModelError(f"Portable project {key} must be an array.")
    for key in ("files", "tasks", "equipment"):
        if not isinstance(document.get(key), list):
            raise ModelError(f"Portable project {key} must be an array.")
    for key in ("materials", "test_results", "partitions", "windows", "floors", "roofs"):
        if not isinstance(project.get("data"), dict) or not isinstance(project["data"].get(key), list):
            raise ModelError(f"Portable project data.{key} must be an array.")
    for key in ("organizations", "contacts", "notes", "documents"):
        if not isinstance(document.get("dependencies"), dict) or not isinstance(document["dependencies"].get(key), list):
            raise ModelError(f"Portable project dependencies.{key} must be an array.")
    transfer = document.get("transfer", {})
    if (not isinstance(transfer, dict) or transfer.get("admin") not in ("included", "omitted")
            or transfer.get("files") not in ("embedded", "external")
            or ("admin" in project) != (transfer["admin"] == "included")):
        raise ModelError("Portable project admin/file transfer declaration is inconsistent.")
    identities = [f.get("id") for f in document["files"] if isinstance(f, dict)]
    if (len(identities) != len(document["files"]) or any(not isinstance(x, str) or not x for x in identities)
            or len(set(identities)) != len(identities)):
        raise ModelError("Portable file identities must be nonempty and unique.")
    for record in document["files"]:
        owner = record.get("owner", {})
        if (not isinstance(owner, dict) or not owner.get("kind") or not owner.get("id")
                or _integer(record.get("size")) < 0):
            raise ModelError("Portable files need an owner and nonnegative byte size.")
        if "missing" in record and record["missing"] is not True:
            raise ModelError("The portable missing-file marker must be true.")
        if transfer["files"] == "embedded" and "data" not in record:
            raise ModelError("Embedded portable projects require every file's bytes.")


def read(path):
    document = decode(_bytes(path))
    validate(document)
    return document


def models(document):
    return [m for m in document["project"]["models"]
            if isinstance(m, dict) and isinstance(m.get("payload"), dict)
            and m["payload"].get("application") == NAMESPACE]


def _binding(model):
    try:
        payload = model["payload"]
        binding = payload["extensions"][NAMESPACE]
        if _integer(payload["model_schema_version"]) != SCHEMA_VERSION:
            raise ModelError(f"Portable native model must use schema {SCHEMA_VERSION}.")
        main = binding["geopackage_file_id"]
        files = binding["files"]
        if (not isinstance(files, dict) or not isinstance(payload["file_ids"], list)
                or any(not isinstance(i, str) or not i for i in payload["file_ids"])
                or len(set(payload["file_ids"])) != len(payload["file_ids"])
                or main not in files or not set(files) <= set(payload["file_ids"])):
            raise ModelError("Native model file bindings do not match its file IDs.")
        names = [str(_relative(v)) for v in files.values()]
        if len(set(names)) != len(names) or files[main] != "model.gpkg":
            raise ModelError("Native model paths conflict or its GeoPackage binding is invalid.")
        if any(name.startswith("model.gpkg/") for name in names):
            raise ModelError("Native file and directory paths conflict.")
        for name in names:
            if any(other.startswith(name + "/") for other in names):
                raise ModelError("Native file and directory paths conflict.")
        if set(names) & {"model.gpkg-wal", "model.gpkg-shm", "model.gpkg-journal"}:
            raise ModelError("Portable GeoPackages must be checkpointed without SQLite sidecars.")
        if not isinstance(binding["native_model_id"], str):
            raise ModelError("The native model identity is missing.")
        rasters = binding.get("elevation_rasters", {})
        if not isinstance(rasters, dict):
            raise ModelError("Native elevation bindings must be an object.")
        for raster in rasters.values():
            if (not isinstance(raster, dict) or raster.get("file_id") not in files
                    or not isinstance(raster.get("stored_path"), str)):
                raise ModelError("Native elevation file bindings are incomplete.")
        return binding
    except (KeyError, TypeError, AttributeError) as error:
        raise ModelError("This QGIS model has no supported native file binding.") from error


def _companion(store):
    return Path(store.path).with_suffix(".acalc-qgis.json")


def attach(store):
    """Restore a working-copy binding without changing the native GeoPackage."""
    companion = _companion(store)
    if not companion.exists():
        return
    context = decode(_bytes(companion))
    if not isinstance(context, dict) or not isinstance(context.get("model_id"), str) or not context.get("project_file"):
        raise ModelError("Invalid portable working model binding.")
    project_path = _under(companion.parent, context["project_file"])
    document = read(project_path)
    model = next((m for m in models(document) if m.get("id") == context["model_id"]), None)
    if model is None:
        raise ModelError("The working model's portable project reference is missing.")
    binding = _binding(model)
    if _under(companion.parent, binding["files"][binding["geopackage_file_id"]]) != Path(store.path):
        raise ModelError("The portable binding points to another GeoPackage.")
    store.portable_context = dict(project_file=str(project_path), model_id=model["id"])
    store.portable_rasters = {}
    for ident, raster in binding.get("elevation_rasters", {}).items():
        path = _under(companion.parent, binding["files"][raster["file_id"]])
        store.portable_rasters[ident] = dict(stored_path=raster["stored_path"], path=str(path))


def import_model(project_path, destination, model_id):
    """Publish an entire validated working folder, never overwrite user files."""
    from .storage import ModelStore
    project_path, destination = Path(project_path).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ModelError("Choose a new working folder; the destination already exists.")
    document = read(project_path)
    matching = [m for m in models(document) if m.get("id") == model_id]
    model = matching[0] if len(matching) == 1 else None
    if model is None:
        raise ModelError("The selected project has no matching QGIS model.")
    binding = _binding(model)
    records = {f["id"]: f for f in document["files"]}
    # Verify all file bytes, including sections this application does not edit.
    content = {ident: file_bytes(record, project_path.parent) for ident, record in records.items()
               if not record.get("missing")}
    if not set(binding["files"]) <= set(content):
        raise ModelError("Native model references a missing file record.")
    if set(binding["files"].values()) & {"project.acalc-project.json", "model.acalc-qgis.json"}:
        raise ModelError("Native file bindings conflict with working project metadata.")
    temporary = Path(tempfile.mkdtemp(prefix=".acalc-import-", dir=destination.parent))
    try:
        for ident, name in binding["files"].items():
            target = _under(temporary, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content[ident])
        # Embed external files so the working context never depends on its origin.
        for ident, record in records.items():
            if ident not in content:
                continue
            record.pop("path", None)
            record["data"] = base64.b64encode(content[ident]).decode("ascii")
        document["transfer"]["files"] = "external" if any(f.get("missing") for f in records.values()) else "embedded"
        (temporary / "project.acalc-project.json").write_bytes(encode(document))
        (temporary / "model.acalc-qgis.json").write_bytes(encode(dict(
            project_file="project.acalc-project.json", model_id=model_id)))
        store = ModelStore.open(temporary / "model.gpkg")
        native = store.metadata()
        if native["schema_version"] != SCHEMA_VERSION or native["project_id"] != binding["native_model_id"]:
            raise ModelError("The native model identity/schema does not match its portable descriptor.")
        if (temporary / "model.gpkg").read_bytes() != content[binding["geopackage_file_id"]]:
            raise ModelError("Opening the portable native model unexpectedly changed its bytes.")
        attach(store)
        # Validate bindings against actual raster identities, not just arbitrary IDs.
        with store.connection(readonly=True) as db:
            native_rasters = {r["raster_id"]: r["path"] for r in db.execute("SELECT * FROM acalc_elevation_rasters")}
        bound_rasters = binding.get("elevation_rasters", {})
        if set(native_rasters) != set(bound_rasters) or any(
                bound_rasters[ident]["stored_path"] != path for ident, path in native_rasters.items()):
            raise ModelError("Portable elevation raster bindings do not match native references.")
        # Rename only after all bytes, native schema and runtime bindings validate.
        destination.mkdir()  # Exclusive reservation: never replace even an empty user folder.
        try:
            for child in temporary.iterdir():
                os.rename(child, destination / child.name)
        except BaseException:
            shutil.rmtree(destination)
            raise
        store = ModelStore.open(destination / "model.gpkg")
        attach(store)
        return store
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _new_document(metadata):
    return dict(format="acalc-project", schema_version=2,
                project=dict(info=dict(project_id=metadata["project_id"], name=metadata["name"],
                                      status="draft", client_organization_id=None, contact_ids=[],
                                      related_organization_ids=[], note_ids=[], document_ids=[], file_ids=[]),
                             rooms=[], measurements=[], data={key: [] for key in
                                 ("materials", "test_results", "partitions", "windows", "floors", "roofs", "sound_power_spectra")},
                             calculations=[], models=[]),
                dependencies=dict(organizations=[], contacts=[], notes=[], documents=[]),
                files=[], tasks=[], equipment=[], transfer=dict(admin="omitted", files="embedded"))


def _native_files(store):
    """Native GeoPackage plus local GDAL elevation and saved result closure."""
    from osgeo import gdal
    from .area_results import results_directory
    assets = {"model.gpkg": Path(store.path)}
    rasters = {}
    for raster in store.elevation_rasters():
        path = Path(raster["path"])
        source = gdal.Open(str(path))
        if source is None or source.GetDriver().ShortName == "VRT":
            raise ModelError(f"Convert {raster['name']} to a self-contained raster before portable export.")
        native_files = source.GetFileList() or [str(path)]
        source = None
        prefix = "elevation/" + str(uuid.UUID(raster["raster_id"])) + "/"
        for item in native_files:
            candidate = Path(item).resolve()
            try:
                relative = candidate.relative_to(path.resolve().parent).as_posix()
            except ValueError as error:
                raise ModelError("Raster dependencies must be beside their source raster.") from error
            assets[prefix + relative] = candidate
        rasters[raster["raster_id"]] = prefix + path.name
    result_root = results_directory(store)
    # Keep the closure of all retained result sets; never sweep unrelated files.
    with store.connection(readonly=True) as db:
        runs = [json.loads(r[0]) for r in db.execute("SELECT metadata_json FROM acalc_calculation_runs")]
    for run in runs:
        for raster in run.get("area_rasters", []):
            path = _under(result_root, raster["path"])
            if not path.is_file():
                raise ModelError("A saved calculation raster is missing; restore it before portable export.")
            source = gdal.Open(str(path))
            if source is None or source.GetDriver().ShortName == "VRT":
                raise ModelError("Saved result rasters must be self-contained before portable export.")
            native_files = source.GetFileList() or [str(path)]
            source = None
            for item in native_files:
                candidate = Path(item).resolve()
                try:
                    name = candidate.relative_to(result_root.resolve()).as_posix()
                except ValueError as error:
                    raise ModelError("Saved raster dependencies escape the results directory.") from error
                assets["model_results/" + name] = candidate
    return assets, rasters


def export_model(store, destination):
    """Update just the selected native model and its immutable file records."""
    metadata = store.metadata()
    if metadata["schema_version"] != SCHEMA_VERSION:
        raise ModelError("Upgrade a native copy to the current schema before portable export.")
    if Path(store.path + "-wal").exists() and Path(store.path + "-wal").stat().st_size:
        raise ModelError("Close active GeoPackage writers before portable export; pending WAL data must be checkpointed first.")
    context = getattr(store, "portable_context", None)
    document = read(context["project_file"]) if context else _new_document(metadata)
    model = next((m for m in models(document) if context and m["id"] == context["model_id"]), None)
    if model is None:
        model = dict(id=str(uuid.uuid5(uuid.UUID(metadata["project_id"]), NAMESPACE)),
                     name=metadata["name"], payload={})
        document["project"]["models"].append(model)
    old_binding = model.get("payload", {}).get("extensions", {}).get(NAMESPACE, {})
    records = {f["id"]: f for f in document["files"]}
    origin = Path(context["project_file"]).parent if context else Path(store.path).parent
    # Re-export remains standalone after moving/removing the original JSON folder.
    for record in records.values():
        if record.get("missing"):
            continue
        content = file_bytes(record, origin)
        record.pop("path", None)
        record["data"] = base64.b64encode(content).decode("ascii")
    assets, rasters = _native_files(store)
    bindings = {}
    old_by_name = {name: ident for ident, name in old_binding.get("files", {}).items()}
    for name, path in assets.items():
        content = _bytes(path)
        ident = old_by_name.get(name)
        digest = hashlib.sha256(content).hexdigest()
        if ident not in records or records[ident]["sha256"] != digest:
            ident = new_id()
            record = dict(id=ident, name=path.name, original_name=path.name,
                          media_type="application/geopackage+sqlite3" if name == "model.gpkg" else "application/octet-stream",
                          size=len(content), sha256=digest,
                          owner=dict(kind="project", id=document["project"]["info"]["project_id"]),
                          data=base64.b64encode(content).decode("ascii"))
            document["files"].append(record)
            records[ident] = record
        bindings[ident] = name
    by_name = {name: ident for ident, name in bindings.items()}
    with store.connection(readonly=True) as db:
        stored_rasters = {r["raster_id"]: r["path"] for r in db.execute("SELECT * FROM acalc_elevation_rasters")}
    payload = model["payload"]
    other_file_ids = [ident for ident in payload.get("file_ids", []) if ident not in old_binding.get("files", {})]
    crs = dict(payload.get("crs", {})) if isinstance(payload.get("crs"), dict) else {}
    crs.update(wkt=metadata["crs_wkt"], authid=metadata["crs_authid"])
    units = dict(payload.get("units", {}))
    units.update(horizontal=metadata["horizontal_unit"], horizontal_to_m=metadata["horizontal_to_m"],
                 vertical=metadata["vertical_unit"], vertical_datum=metadata["vertical_datum"])
    model_metadata = dict(payload.get("metadata", {}))
    for key in old_binding.get("metadata_keys", []):
        model_metadata.pop(key, None)
    native_metadata = decode(metadata["metadata_json"].encode("utf-8"))
    model_metadata.update(native_metadata)
    payload.update(application=NAMESPACE, model_schema_version=SCHEMA_VERSION,
                   crs=crs, units=units, metadata=model_metadata, file_ids=other_file_ids + list(bindings))
    native_binding = dict(old_binding)
    native_binding.update(
        native_model_id=metadata["project_id"], geopackage_file_id=by_name["model.gpkg"], files=bindings,
        metadata_keys=list(native_metadata),
        elevation_rasters={ident: dict(file_id=by_name[name], stored_path=stored_rasters[ident])
                           for ident, name in rasters.items()})
    payload.setdefault("extensions", {})[NAMESPACE] = native_binding
    model["name"] = metadata["name"]
    document["transfer"]["files"] = "external" if any(f.get("missing") for f in document["files"]) else "embedded"
    validate(document)
    encoded = encode(document)
    destination = Path(destination).resolve()
    if destination == Path(store.path) or destination == _companion(store):
        raise ModelError("Choose a separate portable project filename.")
    fd, temporary = tempfile.mkstemp(prefix=".acalc-export-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return document
