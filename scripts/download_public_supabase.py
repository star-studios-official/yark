#!/usr/bin/env python3
"""
Download data that is publicly accessible through a Supabase project's
normal REST/Storage APIs.

This script does NOT bypass RLS, authentication, signed URLs, or bucket
permissions. It records which discovery requests succeeded and downloads
only objects that the public API permits.

Environment:
  SUPABASE_URL       e.g. https://example.supabase.co
  SUPABASE_ANON_KEY  public/anon/publishable key
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

BASE = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_ANON_KEY", "").strip()

if not BASE or not KEY:
    print("SUPABASE_URL and SUPABASE_ANON_KEY are required.", file=sys.stderr)
    sys.exit(2)

HEADERS = {
    "apikey": KEY,
    "Authorization": f"Bearer {KEY}",
    "Accept": "application/json",
}

REST = f"{BASE}/rest/v1"
STORAGE = f"{BASE}/storage/v1"

OUT = Path("public-supabase-dump")
OUT.mkdir(exist_ok=True)
TABLE_DIR = OUT / "tables"
TABLE_DIR.mkdir(exist_ok=True)
OBJECT_DIR = OUT / "objects"
OBJECT_DIR.mkdir(exist_ok=True)

session = requests.Session()
session.headers.update(HEADERS)

discovery: dict[str, Any] = {
    "supabase_url": BASE,
    "rest_base": REST,
    "storage_base": STORAGE,
    "tables": {},
    "buckets": {},
    "objects": {},
    "errors": [],
}

def get(url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", 30)
    return session.get(url, **kwargs)

def post(url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", 30)
    return session.post(url, **kwargs)

def safe_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    return s[:180] or "_"

def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")

def discover_tables_from_openapi() -> list[str]:
    """
    PostgREST normally exposes an OpenAPI document at /rest/v1/.
    The root endpoint itself may return the OpenAPI schema when requested
    with the appropriate Accept header.
    """
    names: list[str] = []
    for accept in (
        "application/openapi+json",
        "application/json",
    ):
        try:
            r = get(f"{REST}/", headers={"Accept": accept})
            if not r.ok:
                continue
            data = r.json()
            paths = data.get("paths", {})
            for path in paths:
                if not isinstance(path, str):
                    continue
                if path.startswith("/") and path != "/":
                    name = path.lstrip("/").split("/")[0]
                    if re.fullmatch(r"[A-Za-z0-9_$-]+", name):
                        names.append(name)
            if names:
                break
        except Exception as exc:
            discovery["errors"].append(
                {"operation": "openapi", "error": repr(exc)}
            )
    return sorted(set(names))

def try_known_names(names: list[str]) -> list[str]:
    """
    A small set of names visible in the app's bundle. These are only
    candidates; failed requests are ignored.
    """
    candidates = {
        "catalog",
        "catalog_items",
        "catalog_item",
        "dump_binary",
        "dumps",
        "dump",
        "buildDumpExports",
    }
    candidates.update(names)
    return sorted(candidates)

def download_table(name: str) -> bool:
    url = f"{REST}/{quote(name, safe='')}"
    params = {
        "select": "*",
        "limit": "1000",
    }
    try:
        r = get(url, params=params)
        if not r.ok:
            discovery["tables"][name] = {
                "status": r.status_code,
                "accessible": False,
                "response": r.text[:1000],
            }
            return False

        data = r.json()
        discovery["tables"][name] = {
            "status": r.status_code,
            "accessible": True,
            "rows_downloaded": len(data) if isinstance(data, list) else None,
        }
        write_json(TABLE_DIR / f"{safe_name(name)}.json", data)
        return True
    except Exception as exc:
        discovery["errors"].append(
            {"operation": "table", "name": name, "error": repr(exc)}
        )
        return False

def discover_buckets() -> list[str]:
    # Runtime bucket names recovered from the application.
    return ["catalog-images", "mark-dumps", "unknown-photos"]

def list_bucket(bucket: str) -> list[dict[str, Any]]:
    # Listing is optional. Public objects can be fetched directly even when
    # object listing is disabled.
    url = f"{STORAGE}/object/list/{quote(bucket, safe='')}"
    body = {
        "prefix": "",
        "limit": 1000,
        "offset": 0,
        "sortBy": {"column": "name", "order": "asc"},
    }
    try:
        r = post(url, json=body)
        if r.ok:
            data = r.json()
            return data if isinstance(data, list) else []
        discovery["buckets"].setdefault(bucket, {}).update({
            "object_list_status": r.status_code,
            "object_list_accessible": False,
            "object_list_response": r.text[:1000],
        })
    except Exception as exc:
        discovery["errors"].append({
            "operation": "object_list",
            "bucket": bucket,
            "error": repr(exc),
        })
    return []

def download_public_object(bucket: str, path: str) -> bool:
    # Direct public Storage endpoint. No bearer token is required for an
    # object that is genuinely configured as public.
    url = (
        f"{STORAGE}/object/public/"
        f"{quote(bucket, safe='')}/"
        f"{quote(path, safe='/')}"
    )
    try:
        r = requests.get(url, headers={"Accept": "*/*"},
                         stream=True, timeout=60)
        if not r.ok:
            discovery["objects"].setdefault(bucket, []).append({
                "path": path,
                "status": r.status_code,
                "downloaded": False,
                "response": r.text[:500],
            })
            return False

        target = OBJECT_DIR / safe_name(bucket) / Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)

        discovery["objects"].setdefault(bucket, []).append({
            "path": path,
            "status": r.status_code,
            "downloaded": True,
            "bytes": target.stat().st_size,
        })
        return True
    except Exception as exc:
        discovery["errors"].append({
            "operation": "public_object_download",
            "bucket": bucket,
            "path": path,
            "error": repr(exc),
        })
        return False

def bundle_paths(bucket: str) -> list[str]:
    # Concrete path visible in the application bundle. More paths can be
    # added here when recovered from the APK.
    if bucket == "catalog-images":
        return ["catalog/images/back/catalog_0005_back.jpg"]
    return []

def main() -> None:
    print(f"Supabase: {BASE}")

    print("\n[1/3] Discovering REST tables...")
    tables = discover_tables_from_openapi()
    print(f"OpenAPI candidates: {len(tables)}")
    successful_tables = 0
    for name in try_known_names(tables):
        if download_table(name):
            successful_tables += 1
            print(f"  + public table/view: {name}")
    print(f"Accessible tables/views: {successful_tables}")

    print("\n[2/3] Trying actual Storage buckets...")
    for bucket in discover_buckets():
        listed = list_bucket(bucket)
        paths = [
            str(item["name"]) for item in listed
            if isinstance(item, dict) and item.get("name")
            and not str(item["name"]).endswith("/")
        ]

        # Listing may be denied; still try concrete public paths recovered
        # from the application.
        paths.extend(bundle_paths(bucket))
        paths = list(dict.fromkeys(paths))

        print(f"  {bucket}: {len(paths)} candidate paths")
        for path in paths:
            if download_public_object(bucket, path):
                print(f"    + {path}")

    print("\n[3/3] Writing manifest and ZIP...")
    write_json(OUT / "discovery.json", discovery)

    zip_path = Path("database-dump.zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in OUT.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(OUT.parent))

    print(f"\nCreated {zip_path} ({zip_path.stat().st_size:,} bytes)")

if __name__ == "__main__":
    main()
