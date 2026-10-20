# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Colorado Analytics
"""Shared formatting and validation for API calculation counters."""

from .schema import ModelError


def calculation_scene_counts(value):
    """Count submitted sources/obstacles, separately from authored objects.

    Saved runs retain the request; completion summaries carry its component
    counts. Older runs without either keep their original recorded counts.
    """
    metadata = value.get("metadata", value)
    request = metadata.get("api_snapshot", {}).get("request", {})
    return {
        "source_count": (len(request["sources"]) if "sources" in request else
                         metadata.get("component_source_count", value["source_count"])),
        "building_count": (len(request["buildings"]) if "buildings" in request else
                           metadata.get("solid_building_count", metadata.get("building_count", 0))),
    }


PATH_COUNTS_HELP = (
    "Calculations checked: one per source–receiver–reflection-path combination, including the direct path. "
    "Each receiver counts separately. Octave bands and line/area integration samples do not add calculations. "
    "Counts from older engine runs used different units and must not be compared with these counts."
)

PRUNED_HELP = (
    " Pruned: calculations skipped by cutoffs. The pruned total never exceeds the checked total."
)


def path_counts_help(pruning=True):
    return PATH_COUNTS_HELP + (PRUNED_HELP if pruning else "")


def validate_path_counts(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or any(type(value.get(key)) is not int or value[key] < 0
                                         for key in ("checked", "pruned"))
            or value["pruned"] > value["checked"]):
        raise ModelError("The API returned invalid calculation counts.")
    return value


def path_counts_text(value, pruning=True):
    """Counts line; the pruned total is omitted when pruning was off for the run."""
    counts = validate_path_counts(value)
    if counts is None:
        return "Calculations checked: awaiting API information"
    text = f"Calculations checked: {counts['checked']:,}"
    if pruning:
        text += f" · Pruned: {counts['pruned']:,}"
    return text + "\n(one per source–receiver–reflection-path combination)"


WORK_PROGRESS_HELP = (
    "Live worker and queue activity, refreshed even while a path is still being evaluated. "
    "Jobs include path searches, screening, integration and their follow-up work. "
    "Evaluations count sampled/accounted operations. These are not calculation totals or a completion estimate; "
    "the percentage advances as complete beams finish."
)


def validate_work_progress(value):
    if value is None:
        return None
    keys = ("workers", "active_workers", "queued_jobs", "completed_jobs", "operations")
    if (not isinstance(value, dict)
            or any(type(value.get(key)) is not int or value[key] < 0 for key in keys)
            or value["workers"] < 1 or value["active_workers"] > value["workers"]):
        raise ModelError("The API returned invalid worker activity.")
    return value


def work_progress_text(value):
    work = validate_work_progress(value)
    if work is None:
        return ""
    return (f"Workers: {work['active_workers']:,}/{work['workers']:,} active · "
            f"Queued jobs: {work['queued_jobs']:,}\n"
            f"Jobs completed: {work['completed_jobs']:,} · Evaluations: {work['operations']:,}")
