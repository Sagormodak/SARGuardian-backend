#!/usr/bin/env python3
"""
SARGuardian GOFF Worker

Real NISAR/GOFF science worker for GitHub Actions execution.
Supports benchmark mode (single product) and full mode (complete stack).
"""

import argparse
import csv
import gc
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

import numpy as np

EXPECTED_COMMIT = "77cc9646cfa46d3aff3669d351912f984cf67aa3"
SCIENCE_REPOSITORY = "Sagormodak/SARGuardian"
COLLECTIONS = (
    "NISAR_L2_GOFF_BETA_V1",
    "NISAR_L2_GOFF_PROVISIONAL_V1",
)
TARGET_RADIUS_PX = 6
BUFFER_KM = 2.0
LOW_DISK_BYTES = 2 * 1024 ** 3

EXPECTED_EDGES = [
    ("D", 48, "20251125", "20251207"),
    ("A", 98, "20251128", "20251210"),
    ("D", 48, "20251207", "20251219"),
    ("A", 98, "20251210", "20251222"),
    ("D", 48, "20251219", "20251231"),
    ("A", 98, "20251222", "20260103"),
    ("D", 48, "20251231", "20260112"),
    ("A", 98, "20260103", "20260115"),
    ("A", 98, "20260620", "20260702"),
    ("D", 48, "20260629", "20260711"),
    ("A", 98, "20260702", "20260714"),
    ("D", 48, "20260711", "20260723"),
    ("A", 98, "20260714", "20260726"),
    ("D", 48, "20260723", "20260816"),
    ("A", 98, "20260726", "20260819"),
    ("D", 48, "20260816", "20260828"),
]


def get_runtime_params(parameters: dict) -> tuple[str, float, float]:
    """Extract runtime parameters with scientifically required defaults."""
    start_date = parameters.get("start_date", "2025-11-25")
    target_lat = float(parameters.get("target_lat", 28.27799))
    target_lon = float(parameters.get("target_lon", 85.52983))
    return start_date, target_lat, target_lon


class StageFailure(Exception):
    pass


def fail(marker):
    raise StageFailure(marker)


def disk_snapshot(label):
    usage = shutil.disk_usage("/")
    print(f"DISK_{label}_AVAILABLE_BYTES: {usage.free}")
    return usage.free


def rss_bytes():
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def peak_rss_worker(stop_event, peak):
    while not stop_event.is_set():
        current = rss_bytes()
        if current is not None:
            peak[0] = max(peak[0], current)
        stop_event.wait(0.05)


def safe_name(result):
    if hasattr(result, "get"):
        umm = result.get("umm", {})
        if isinstance(umm, dict):
            name = umm.get("GranuleUR")
            if name:
                return Path(str(name)).name
        for key in ("producer_granule_id", "granuleName", "name"):
            if result.get(key):
                return Path(str(result[key])).name
    for attr in ("granule_ur", "name"):
        value = getattr(result, attr, None)
        if value:
            return Path(str(value)).name
    return "unknown"


def parse_goff(name, result):
    stamps = re.findall(r"_(\d{8})T\d{6}", name)
    track = re.search(
        r"NISAR_L2_(PR|UR)_GOFF_\d+_(\d{3})_([AD])_", name
    )
    if len(stamps) < 4 or track is None:
        return None
    try:
        ref = datetime.strptime(stamps[0], "%Y%m%d").date()
        sec = datetime.strptime(stamps[2], "%Y%m%d").date()
    except ValueError:
        return None
    return {
        "result": result,
        "name": name,
        "processing": track.group(1),
        "path": int(track.group(2)),
        "direction": track.group(3),
        "ref": ref,
        "sec": sec,
        "ref_stamp": stamps[0],
        "sec_stamp": stamps[2],
        "span_days": (sec - ref).days,
    }


def edge_key(item):
    return (
        item["direction"], item["path"],
        item["ref_stamp"], item["sec_stamp"],
    )


def choose_documented_stack(results):
    candidates = {}
    for result in results:
        item = parse_goff(safe_name(result), result)
        if item is not None:
            candidates.setdefault(edge_key(item), []).append(item)

    chosen = []
    missing = []
    for direction, path, ref, sec in EXPECTED_EDGES:
        edge = (direction, path, ref, sec)
        options = sorted(
            candidates.get(edge, []),
            key=lambda item: (0 if item["processing"] == "PR" else 1,
                               item["name"]),
        )
        pr = next((item for item in options
                   if item["processing"] == "PR"), None)
        if pr is None:
            missing.append(edge)
        else:
            chosen.append((pr, "documented 16-product PR GOFF stack"))
    if missing:
        return None, missing
    return chosen, []


def grid_signature(grid):
    return (
        grid["height"], grid["width"], str(grid["transform"]),
        float(grid["res_x"]), float(grid["res_y"]),
        tuple(grid["rows"].tolist()), tuple(grid["cols"].tolist()),
    )


def run_benchmark(science_root, output_dir, job_id, parameters):
    """Run benchmark mode: process ONE real GOFF product."""
    print("NISAR_GOFF_BENCHMARK_START")
    print(f"JOB_ID: {job_id}")
    print(f"SCIENCE_COMMIT: {EXPECTED_COMMIT}")

    start_date, target_lat, target_lon = get_runtime_params(parameters)

    sys.path.insert(0, str(science_root / "src"))
    import earthaccess
    from goff_reader import read_goff
    import gunw_reader
    from gunw_reader import aoi_grid, place_on_grid, set_aoi

    actual_commit = subprocess.check_output(
        ["git", "-C", str(science_root), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    if actual_commit != EXPECTED_COMMIT:
        raise RuntimeError("science source revision mismatch")

    set_aoi("source")
    source_ring = gunw_reader.AOIS["source"]
    lons = [point[0] for point in source_ring]
    lats = [point[1] for point in source_ring]
    bbox = (min(lons), min(lats), max(lons), max(lats))

    END = datetime.now(timezone.utc).date().isoformat()
    raw_dir = Path(tempfile.mkdtemp(prefix="nisar-goff-benchmark-"))
    result_dir = Path(output_dir)

    try:
        earthaccess.login(strategy="environment")
        results = []
        for collection in COLLECTIONS:
            collection_results = earthaccess.search_data(
                short_name=collection,
                temporal=(start_date, END),
                bounding_box=bbox,
                count=100,
            )
            results.extend(collection_results)
        print(f"GOFF_DISCOVERY_COUNT: {len(results)}")

        selected_items, missing = choose_documented_stack(results)
        if selected_items is None:
            print(f"GOFF_MISSING_EXPECTED_EDGES: {missing}")
            fail("NISAR_GOFF_BENCHMARK_NETWORK_NOT_FOUND")
        print(f"GOFF_SELECTED_COUNT: {len(selected_items)}")
        for item, reason in selected_items:
            print(
                f"GOFF_SELECTED_PRODUCT: {item['name']} "
                f"direction={item['direction']} path={item['path']} "
                f"ref={item['ref']} secondary={item['sec']} "
                f"span_days={item['span_days']} reason={reason}"
            )

        item = selected_items[0][0]
        print(f"GOFF_DOWNLOAD_PROGRESS: 1/1 (benchmark mode)")
        disk_before = disk_snapshot("BEFORE_GOFF_DOWNLOAD")
        if disk_before < LOW_DISK_BYTES:
            fail("NISAR_GOFF_BENCHMARK_LOW_DISK")

        download_started = datetime.now(timezone.utc)
        download_start = monotonic()
        download_succeeded = False
        for _attempt in range(3):
            try:
                earthaccess.download([item["result"]], local_path=raw_dir)
                download_succeeded = True
                break
            except Exception:
                for partial_path in raw_dir.rglob("*"):
                    if partial_path.is_file():
                        partial_path.unlink()
        if not download_succeeded:
            if shutil.disk_usage("/").free < LOW_DISK_BYTES:
                fail("NISAR_GOFF_BENCHMARK_LOW_DISK")
            fail("NISAR_GOFF_BENCHMARK_DOWNLOAD_FAILED")

        download_finished = datetime.now(timezone.utc)
        raw_files = sorted(raw_dir.rglob("*.h5"))
        if len(raw_files) != 1:
            fail("NISAR_GOFF_BENCHMARK_DOWNLOAD_FAILED")
        raw_path = raw_files[0]
        file_size = raw_path.stat().st_size
        disk_after_download = disk_snapshot("AFTER_GOFF_DOWNLOAD")

        rss_before = rss_bytes()
        peak = [rss_before or 0]
        monitor_stop = threading.Event()
        monitor = threading.Thread(
            target=peak_rss_worker,
            args=(monitor_stop, peak),
            daemon=True,
        )
        read_started = datetime.now(timezone.utc)
        read_start = monotonic()
        monitor.start()
        try:
            goff_result = read_goff(
                raw_path,
                layer="layer2",
                clip_aoi=True,
                auto_ref=True,
                deramp=True,
            )
        except Exception:
            fail("NISAR_GOFF_BENCHMARK_READ_FAILED")
        finally:
            monitor_stop.set()
            monitor.join(timeout=2)
        read_finished = datetime.now(timezone.utc)
        read_elapsed = monotonic() - read_start
        rss_after = rss_bytes()

        try:
            layer_key = next(
                (key for key in goff_result["layers"]
                 if key.endswith("layer2")),
                sorted(goff_result["layers"])[0],
            )
            layer = goff_result["layers"][layer_key]
            valid = layer["valid"]
            range_mm = layer["range_m"] * 1000.0
            grid = aoi_grid(
                layer["xs"], layer["ys"],
                gunw_reader.AOI_RING, layer["epsg"],
            )
            if grid is None:
                fail("NISAR_GOFF_BENCHMARK_READ_FAILED")
            lattice = place_on_grid(
                np.where(valid, range_mm, np.nan), grid
            )
            n_px = int(valid.sum())
            correlation = layer.get("correlation")
            coherence = (
                float(correlation[valid].mean())
                if correlation is not None and valid.any() else None
            )
            print(
                f"GOFF_READ_PROGRESS: 1/1 "
                f"valid_pixels={n_px}"
            )
            print(
                f"GOFF_LATTICE_PROGRESS: 1/1 "
                f"finite_pixels={int(np.isfinite(lattice).sum())}"
            )
        except StageFailure:
            raise
        except Exception:
            fail("NISAR_GOFF_BENCHMARK_READ_FAILED")
        finally:
            del goff_result
            gc.collect()

        try:
            raw_path.unlink()
            if raw_path.exists() or list(raw_dir.rglob("*.h5")):
                fail("NISAR_GOFF_BENCHMARK_READ_FAILED")
        except StageFailure:
            raise
        except Exception:
            fail("NISAR_GOFF_BENCHMARK_READ_FAILED")

        disk_after_delete = disk_snapshot("AFTER_GOFF_RAW_DELETE")

        read_record = {
            "product": item["name"],
            "direction": item["direction"],
            "path": item["path"],
            "reference": str(item["ref"]),
            "secondary": str(item["sec"]),
            "span_days": item["span_days"],
            "file_size_bytes": file_size,
            "download_started_utc": download_started.isoformat(),
            "download_finished_utc": download_finished.isoformat(),
            "download_elapsed_seconds": round(monotonic() - download_start, 3),
            "read_started_utc": read_started.isoformat(),
            "read_finished_utc": read_finished.isoformat(),
            "read_elapsed_seconds": round(read_elapsed, 3),
            "rss_before_bytes": rss_before,
            "rss_peak_observed_bytes": peak[0],
            "rss_after_bytes": rss_after,
            "disk_before_download_available_bytes": disk_before,
            "disk_after_download_available_bytes": disk_after_download,
            "disk_after_raw_delete_available_bytes": disk_after_delete,
            "valid_pixel_count": n_px,
            "mean_correlation": coherence,
        }

        summary = {
            "job_id": job_id,
            "mode": "benchmark",
            "science_repository": SCIENCE_REPOSITORY,
            "science_commit": EXPECTED_COMMIT,
            "collections": list(COLLECTIONS),
            "reference_strategy": {
                "aoi": "source",
                "target_lat": target_lat,
                "target_lon": target_lon,
                "target_radius_pixels": TARGET_RADIUS_PX,
                "buffer_km": BUFFER_KM,
                "engine": "goff_reader.read_goff",
            },
            "selected_count": 1,
            "selected_products": [
                {
                    "identifier": item["name"],
                    "direction": item["direction"],
                    "path": item["path"],
                    "reference": str(item["ref"]),
                    "secondary": str(item["sec"]),
                    "span_days": item["span_days"],
                    "selection_reason": "documented 16-product PR GOFF stack",
                }
            ],
            "read_records": [read_record],
            "benchmark_started_utc": datetime.now(timezone.utc).isoformat(),
            "raw_cleanup_success": True,
            "overall_success": True,
        }

        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "result.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        (result_dir / "timeseries.csv").write_text(
            "geometry,component,epoch,cumulative_mm\n"
            f"BENCHMARK,1,{item['ref']},0.0\n",
            encoding="utf-8",
        )
        manifest = {
            "job_id": job_id,
            "science_mode": "benchmark",
            "science_commit": EXPECTED_COMMIT,
            "provenance": {
                "science_repository": SCIENCE_REPOSITORY,
                "science_commit": EXPECTED_COMMIT,
                "collections": list(COLLECTIONS),
            },
            "selected_products": [item["name"]],
            "acquisition_dates": [str(item["ref"]), str(item["sec"])],
            "geometry_path": f"{item['direction']}{item['path']}",
            "processing_timings": read_record,
            "disk_measurements": {
                "before_download_bytes": disk_before,
                "after_download_bytes": disk_after_download,
                "after_raw_delete_bytes": disk_after_delete,
            },
            "peak_memory_bytes": peak[0],
            "cleanup_status": "success",
            "final_processing_status": "completed",
            "processing_parameters": {
                "target_lat": target_lat,
                "target_lon": target_lon,
                "target_radius_pixels": TARGET_RADIUS_PX,
                "buffer_km": BUFFER_KM,
            },
        }
        (result_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        (result_dir / "README.txt").write_text(
            "SARGuardian GOFF benchmark result.\n"
            f"Science commit: {EXPECTED_COMMIT}\n"
            f"Job ID: {job_id}\n"
            "Processed one GOFF product to verify Earthdata auth, download, "
            "GOFF layer2 reading, AOI clipping, derived state generation, "
            "raw .h5 deletion, disk recovery, memory measurement, timing measurement, "
            "and compact result creation.\n",
            encoding="utf-8",
        )

        print("NISAR_GOFF_BENCHMARK_SUCCESS")
        return True

    except StageFailure as exc:
        raise RuntimeError(str(exc))
    finally:
        try:
            if raw_dir.exists():
                for raw_file in raw_dir.rglob("*.h5"):
                    raw_file.unlink()
                shutil.rmtree(raw_dir)
        except Exception:
            pass


def run_full(science_root, output_dir, job_id, parameters):
    """Run full mode: process complete GOFF stack with time-series inversion."""
    print("NISAR_GOFF_FULL_START")
    print(f"JOB_ID: {job_id}")
    print(f"SCIENCE_COMMIT: {EXPECTED_COMMIT}")

    start_date, target_lat, target_lon = get_runtime_params(parameters)

    sys.path.insert(0, str(science_root / "src"))
    import earthaccess
    import gunw_reader
    from goff_reader import read_goff
    from gunw_reader import aoi_grid, place_on_grid, set_aoi
    from timeseries import (
        Pair, apply_common_datum, connected_components,
        invert_component, report_series,
    )

    actual_commit = subprocess.check_output(
        ["git", "-C", str(science_root), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    if actual_commit != EXPECTED_COMMIT:
        raise RuntimeError("science source revision mismatch")

    set_aoi("source")
    source_ring = gunw_reader.AOIS["source"]
    lons = [point[0] for point in source_ring]
    lats = [point[1] for point in source_ring]
    bbox = (min(lons), min(lats), max(lons), max(lats))

    END = datetime.now(timezone.utc).date().isoformat()
    raw_dir = Path(tempfile.mkdtemp(prefix="nisar-goff-full-"))
    result_dir = Path(output_dir)
    summary_path = result_dir / "result.json"
    csv_path = result_dir / "timeseries.csv"

    test_started_at = datetime.now(timezone.utc)
    test_start = monotonic()
    read_records = []
    grid_signatures = {}
    lattices = {}
    pairs = []
    selected_items = []

    try:
        earthaccess.login(strategy="environment")
        discovery_succeeded = False
        for _attempt in range(3):
            try:
                results = []
                for collection in COLLECTIONS:
                    collection_results = earthaccess.search_data(
                        short_name=collection,
                        temporal=(start_date, END),
                        bounding_box=bbox,
                        count=100,
                    )
                    results.extend(collection_results)
                discovery_succeeded = True
                break
            except Exception:
                if _attempt == 2:
                    raise
        if not discovery_succeeded:
            raise RuntimeError("GOFF discovery did not complete")
        print(f"GOFF_DISCOVERY_COUNT: {len(results)}")
        selected_items, missing = choose_documented_stack(results)
        if selected_items is None:
            print(f"GOFF_MISSING_EXPECTED_EDGES: {missing}")
            fail("NISAR_GOFF_FULL_NETWORK_NOT_FOUND")
        print(f"GOFF_SELECTED_COUNT: {len(selected_items)}")
        for item, reason in selected_items:
            print(
                f"GOFF_SELECTED_PRODUCT: {item['name']} "
                f"direction={item['direction']} path={item['path']} "
                f"ref={item['ref']} secondary={item['sec']} "
                f"span_days={item['span_days']} reason={reason}"
            )

        selected_items = [item for item, _reason in selected_items]
        for item in selected_items:
            pairs.append(Pair(
                path=item["path"], direction=item["direction"],
                ref=item["ref"], sec=item["sec"],
                value=None, n_px=None, coherence=None,
                source=item["name"],
            ))

        for index, (item, pair) in enumerate(zip(selected_items, pairs), 1):
            print(f"GOFF_DOWNLOAD_PROGRESS: {index}/{len(selected_items)}")
            disk_before = disk_snapshot("BEFORE_GOFF_DOWNLOAD")
            if disk_before < LOW_DISK_BYTES:
                fail("NISAR_GOFF_FULL_LOW_DISK")
            download_started = datetime.now(timezone.utc)
            download_start = monotonic()
            download_succeeded = False
            for _attempt in range(3):
                try:
                    earthaccess.download(
                        [item["result"]], local_path=raw_dir
                    )
                    download_succeeded = True
                    break
                except Exception:
                    for partial_path in raw_dir.rglob("*"):
                        if partial_path.is_file():
                            partial_path.unlink()
            if not download_succeeded:
                if shutil.disk_usage("/").free < LOW_DISK_BYTES:
                    fail("NISAR_GOFF_FULL_LOW_DISK")
                fail("NISAR_GOFF_FULL_DOWNLOAD_FAILED")
            download_finished = datetime.now(timezone.utc)
            raw_files = sorted(raw_dir.rglob("*.h5"))
            if len(raw_files) != 1:
                fail("NISAR_GOFF_FULL_DOWNLOAD_FAILED")
            raw_path = raw_files[0]
            file_size = raw_path.stat().st_size
            disk_after_download = disk_snapshot("AFTER_GOFF_DOWNLOAD")

            rss_before = rss_bytes()
            peak = [rss_before or 0]
            monitor_stop = threading.Event()
            monitor = threading.Thread(
                target=peak_rss_worker,
                args=(monitor_stop, peak),
                daemon=True,
            )
            read_started = datetime.now(timezone.utc)
            read_start = monotonic()
            monitor.start()
            try:
                goff_result = read_goff(
                    raw_path,
                    layer="layer2",
                    clip_aoi=True,
                    auto_ref=True,
                    deramp=True,
                )
            except Exception:
                fail("NISAR_GOFF_FULL_READ_FAILED")
            finally:
                monitor_stop.set()
                monitor.join(timeout=2)
            read_finished = datetime.now(timezone.utc)
            read_elapsed = monotonic() - read_start
            rss_after = rss_bytes()

            try:
                layer_key = next(
                    (key for key in goff_result["layers"]
                     if key.endswith("layer2")),
                    sorted(goff_result["layers"])[0],
                )
                layer = goff_result["layers"][layer_key]
                valid = layer["valid"]
                range_mm = layer["range_m"] * 1000.0
                grid = aoi_grid(
                    layer["xs"], layer["ys"],
                    gunw_reader.AOI_RING, layer["epsg"],
                )
                if grid is None:
                    fail("NISAR_GOFF_FULL_READ_FAILED")
                signature = grid_signature(grid)
                geometry = (pair.direction, pair.path)
                prior_signature = grid_signatures.get(geometry)
                if prior_signature is None:
                    grid_signatures[geometry] = signature
                elif prior_signature != signature:
                    fail("NISAR_GOFF_FULL_READ_FAILED")
                lattice = place_on_grid(
                    np.where(valid, range_mm, np.nan), grid
                )
                pair.n_px = int(valid.sum())
                correlation = layer.get("correlation")
                pair.coherence = (
                    float(correlation[valid].mean())
                    if correlation is not None and valid.any() else None
                )
                lattices[id(pair)] = (lattice, grid, layer["epsg"])
                print(
                    f"GOFF_READ_PROGRESS: {index}/{len(selected_items)} "
                    f"valid_pixels={pair.n_px}"
                )
                print(
                    f"GOFF_LATTICE_PROGRESS: {index}/{len(selected_items)} "
                    f"finite_pixels={int(np.isfinite(lattice).sum())}"
                )
            except StageFailure:
                raise
            except Exception:
                fail("NISAR_GOFF_FULL_READ_FAILED")
            finally:
                del goff_result
                gc.collect()

            try:
                raw_path.unlink()
                if raw_path.exists() or list(raw_dir.rglob("*.h5")):
                    fail("NISAR_GOFF_FULL_READ_FAILED")
            except StageFailure:
                raise
            except Exception:
                fail("NISAR_GOFF_FULL_READ_FAILED")

            read_records.append({
                "product": item["name"],
                "direction": item["direction"],
                "path": item["path"],
                "reference": str(item["ref"]),
                "secondary": str(item["sec"]),
                "span_days": item["span_days"],
                "file_size_bytes": file_size,
                "download_started_utc": download_started.isoformat(),
                "download_finished_utc": download_finished.isoformat(),
                "download_elapsed_seconds": round(monotonic() - download_start, 3),
                "read_started_utc": read_started.isoformat(),
                "read_finished_utc": read_finished.isoformat(),
                "read_elapsed_seconds": round(read_elapsed, 3),
                "rss_before_bytes": rss_before,
                "rss_peak_observed_bytes": peak[0],
                "rss_after_bytes": rss_after,
                "disk_before_download_available_bytes": disk_before,
                "disk_after_download_available_bytes": disk_after_download,
                "disk_after_raw_delete_available_bytes": disk_snapshot(
                    "AFTER_GOFF_RAW_DELETE"
                ),
                "valid_pixel_count": pair.n_px,
                "mean_correlation": pair.coherence,
            })

        print("GOFF_COMMON_REF_PROGRESS: starting")
        try:
            apply_common_datum(
                pairs, lattices, target_lat, target_lon,
                TARGET_RADIUS_PX, BUFFER_KM,
            )
            if any(pair.value is None for pair in pairs):
                fail("NISAR_GOFF_FULL_COMMON_REF_FAILED")
            print("GOFF_COMMON_REF_PROGRESS: succeeded")
        except StageFailure:
            raise
        except Exception:
            fail("NISAR_GOFF_FULL_COMMON_REF_FAILED")

        print("GOFF_INVERSION_PROGRESS: starting")
        all_rows = []
        inversion_records = []
        try:
            by_geometry = {}
            for pair in pairs:
                by_geometry.setdefault((pair.direction, pair.path), []).append(pair)
            for geometry, group in sorted(by_geometry.items()):
                components = connected_components(group)
                for component_index, component in enumerate(components, 1):
                    inside = [
                        pair for pair in group
                        if pair.ref in component and pair.sec in component
                    ]
                    result = invert_component(inside, component)
                    if "error" in result:
                        fail("NISAR_GOFF_FULL_INVERSION_FAILED")
                    label = f"{'ASC' if geometry[0] == 'A' else 'DESC'} path {geometry[1]}"
                    rows = report_series(label, component_index, result)
                    all_rows.extend(rows)
                    inversion_records.append({
                        "geometry": label,
                        "component": component_index,
                        "epochs": [str(epoch) for epoch in component],
                        "cumulative_mm": [float(value)
                                          for value in result["cumulative_mm"]],
                        "error_mm": [
                            None if not np.isfinite(value) else float(value)
                            for value in result["error_mm"]
                        ],
                        "residual_rms_mm": result["residual_rms_mm"],
                        "sigma_mm": result["sigma_mm"],
                        "dof": result["dof"],
                        "velocity_mm_per_day": result["velocity_mm_per_day"],
                        "velocity_err_mm_per_day": result["velocity_err_mm_per_day"],
                        "n_pairs": result["n_pairs"],
                    })
            if len(all_rows) != sum(len(record["epochs"])
                                   for record in inversion_records):
                fail("NISAR_GOFF_FULL_OUTPUT_FAILED")
            print("GOFF_INVERSION_PROGRESS: succeeded")
        except StageFailure:
            raise
        except Exception:
            fail("NISAR_GOFF_FULL_INVERSION_FAILED")

        summary = {
            "job_id": job_id,
            "mode": "full",
            "science_repository": SCIENCE_REPOSITORY,
            "science_commit": EXPECTED_COMMIT,
            "collections": list(COLLECTIONS),
            "reference_strategy": {
                "aoi": "source",
                "target_lat": target_lat,
                "target_lon": target_lon,
                "target_radius_pixels": TARGET_RADIUS_PX,
                "buffer_km": BUFFER_KM,
                "engine": "timeseries.apply_common_datum",
                "min_common_source_value": 200,
            },
            "selected_count": len(selected_items),
            "selected_products": [
                {
                    "identifier": item["name"],
                    "direction": item["direction"],
                    "path": item["path"],
                    "reference": str(item["ref"]),
                    "secondary": str(item["sec"]),
                    "span_days": item["span_days"],
                    "selection_reason": "documented 16-product PR GOFF stack",
                }
                for item in selected_items
            ],
            "pair_measurements": [
                {
                    "reference": str(pair.ref),
                    "secondary": str(pair.sec),
                    "direction": pair.direction,
                    "path": pair.path,
                    "value_mm": pair.value,
                    "valid_pixel_count": pair.n_px,
                    "mean_correlation": pair.coherence,
                }
                for pair in pairs
            ],
            "inversions": inversion_records,
            "rows": all_rows,
            "read_records": read_records,
            "test_started_utc": test_started_at.isoformat(),
            "test_elapsed_seconds": round(monotonic() - test_start, 3),
            "raw_cleanup_success": True,
            "overall_success": True,
        }
        result_dir.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
        with csv_path.open("w", newline="") as fh:
            fields = [
                "geometry", "component", "epoch", "days_from_start",
                "cumulative_mm", "error_mm",
            ]
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(all_rows)

        manifest = {
            "job_id": job_id,
            "science_mode": "full",
            "science_commit": EXPECTED_COMMIT,
            "provenance": {
                "science_repository": SCIENCE_REPOSITORY,
                "science_commit": EXPECTED_COMMIT,
                "collections": list(COLLECTIONS),
            },
            "selected_products": [item["name"] for item in selected_items],
            "acquisition_dates": sorted({
                str(item["ref"]) for item in selected_items
            } | {
                str(item["sec"]) for item in selected_items
            }),
            "processing_timings": read_records,
            "disk_measurements": [
                {
                    "product": record["product"],
                    "before_download_bytes": record[
                        "disk_before_download_available_bytes"
                    ],
                    "after_download_bytes": record[
                        "disk_after_download_available_bytes"
                    ],
                    "after_raw_delete_bytes": record[
                        "disk_after_raw_delete_available_bytes"
                    ],
                }
                for record in read_records
            ],
            "peak_memory_bytes": max(
                (record["rss_peak_observed_bytes"] or 0 for record in read_records),
                default=0,
            ),
            "cleanup_status": "success",
            "final_processing_status": "completed",
            "processing_parameters": {
                "start_date": start_date,
                "target_lat": target_lat,
                "target_lon": target_lon,
                "target_radius_pixels": TARGET_RADIUS_PX,
                "buffer_km": BUFFER_KM,
            },
        }
        (result_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        print(f"GOFF_COMPACT_JSON_PATH: {summary_path}")
        print(f"GOFF_COMPACT_CSV_PATH: {csv_path}")
        print(f"GOFF_RESULT_DIR: {result_dir}")

        return True

    except StageFailure as exc:
        raise RuntimeError(str(exc))
    finally:
        try:
            if raw_dir.exists():
                for raw_file in raw_dir.rglob("*.h5"):
                    raw_file.unlink()
                shutil.rmtree(raw_dir)
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="SARGuardian GOFF Worker")
    parser.add_argument("--job-id", required=True, help="Job ID")
    parser.add_argument("--output-dir", required=True, help="Output directory for result package")
    parser.add_argument("--science-root", required=True, help="Path to pinned science repository")
    parser.add_argument("--benchmark-only", action="store_true", help="Run benchmark mode (single product)")
    parser.add_argument("--parameters", default="{}", help="JSON parameters")
    args = parser.parse_args()

    science_root = Path(args.science_root)
    output_dir = Path(args.output_dir)
    job_id = args.job_id
    parameters = json.loads(args.parameters)

    if args.benchmark_only:
        run_benchmark(science_root, output_dir, job_id, parameters)
    else:
        run_full(science_root, output_dir, job_id, parameters)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"SAFE_ERROR: {exc}")
        sys.exit(1)
