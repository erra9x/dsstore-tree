#!/usr/bin/env python3
"""dsstore-tree — Discover files and directories via exposed .DS_Store files."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
import urllib3

try:
    from ds_store import DSStore
except ImportError:
    print("[ERROR] ds-store library not found. Install it with: pip install ds-store")
    sys.exit(1)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BANNER = r"""       __          __                        __
  ____/ /_________/ /_____  ________        / /_________  ___
 / __  / ___/ ___/ __/ __ \/ ___/ _ \______/ __/ ___/ _ \/ _ \
/ /_/ (__  |__  ) /_/ /_/ / /  /  __/_____/ /_/ /  /  __/  __/
\__,_/____/____/\__/\____/_/   \___/      \__/_/   \___/\___/
"""

# ANSI colors
COLOR_DIR = "\033[1;34m"   # bold blue
COLOR_FILE = "\033[0;32m"  # green
COLOR_DL = "\033[0;36m"    # cyan
COLOR_ERR = "\033[0;31m"   # red
COLOR_INFO = "\033[0;33m"  # yellow
COLOR_RESET = "\033[0m"


def _c(color: str, text: str, use_color: bool) -> str:
    return f"{color}{text}{COLOR_RESET}" if use_color else text


@dataclass
class Entry:
    """A discovered file or directory."""
    path: str
    is_dir: bool
    url: str
    downloaded: bool = False


@dataclass
class ScanResult:
    """Aggregated scan results."""
    base_url: str
    entries: list[Entry] = field(default_factory=list)

    @property
    def dirs(self) -> list[Entry]:
        return [e for e in self.entries if e.is_dir]

    @property
    def files(self) -> list[Entry]:
        return [e for e in self.entries if not e.is_dir]

    def to_dict(self) -> dict:
        return {
            "base_url": self.base_url,
            "directories": [e.path for e in self.dirs],
            "files": [{"path": e.path, "url": e.url, "downloaded": e.downloaded} for e in self.files],
            "summary": {
                "directories": len(self.dirs),
                "files": len(self.files),
            },
        }


class Scanner:
    def __init__(
        self,
        url: str,
        *,
        download: bool = False,
        quiet: bool = False,
        color: bool = True,
        max_depth: int = 0,
        timeout: int = 10,
        threads: int = 10,
        proxy: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        json_output: bool = False,
    ):
        self.url = url.rstrip("/")
        self.download = download
        self.download_dir = "dsstore-tree_" + urlparse(url).netloc
        self.quiet = quiet
        self.color = color and not json_output
        self.max_depth = max_depth
        self.timeout = timeout
        self.threads = threads
        self.json_output = json_output
        self.result = ScanResult(base_url=self.url)
        self.scanned_dirs: set[str] = set()

        self.session = requests.Session()
        self.session.verify = False
        self.session.timeout = timeout
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        if headers:
            self.session.headers.update(headers)

    # -- helpers --

    def _log(self, msg: str) -> None:
        if not self.quiet and not self.json_output:
            print(msg)

    @staticmethod
    def _is_valid_name(name: str) -> bool:
        if not name or name in (".", ".."):
            return False
        if ".." in name or name.startswith(("/", "\\")):
            return False
        return True

    def _parse_dsstore(self, content: bytes) -> set[str]:
        filenames: set[str] = set()
        tmp_path: Optional[str] = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".ds_store")
            os.write(fd, content)
            os.close(fd)
            with DSStore.open(tmp_path, "r+") as d:
                for entry in d:
                    if entry.filename and self._is_valid_name(entry.filename):
                        filenames.add(entry.filename)
        except Exception as e:
            self._log(_c(COLOR_ERR, f"[ERROR] Failed to parse .DS_Store: {e}", self.color))
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        return filenames

    def _fetch_dsstore(self, base_url: str) -> Optional[bytes]:
        """Fetch .DS_Store from a URL, return content or None."""
        dsstore_url = base_url.rstrip("/") + "/.DS_Store"
        try:
            r = self.session.get(dsstore_url, timeout=self.timeout)
            if r.status_code == 200 and len(r.content) > 0:
                return r.content
        except Exception:
            pass
        return None

    def _is_accessible_file(self, url: str) -> bool:
        """Check if URL points to an accessible file (not a directory redirect)."""
        try:
            r = self.session.head(url, timeout=self.timeout, allow_redirects=False)
            if r.status_code in (301, 302):
                return False
            if r.status_code == 200:
                return True
        except Exception:
            pass
        return False

    def _download_file(self, url: str, local_path: str) -> bool:
        try:
            r = self.session.get(url, stream=True, timeout=self.timeout)
            if r.status_code == 200:
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                return True
        except Exception as e:
            self._log(_c(COLOR_ERR, f"[ERROR] Download failed {url}: {e}", self.color))
        return False

    # -- core scanning --

    def _classify_entries(self, base_url: str, names: set[str]):
        """Classify names into dirs (with/without .DS_Store) and files. Uses thread pool."""
        dirs_with_ds: list[tuple[str, bytes]] = []
        dirs_without_ds: list[str] = []
        files: list[str] = []

        def probe(name: str):
            entry_url = urljoin(base_url + "/", name)
            ds_content = self._fetch_dsstore(entry_url)
            if ds_content is not None:
                return ("dir_ds", name, ds_content)
            # Check redirect -> directory without .DS_Store
            try:
                r = self.session.head(entry_url, timeout=self.timeout, allow_redirects=False)
                if r.status_code in (301, 302):
                    return ("dir_nods", name, None)
            except Exception:
                pass
            # Check if file
            if self._is_accessible_file(entry_url):
                return ("file", name, None)
            return (None, name, None)

        with ThreadPoolExecutor(max_workers=self.threads) as pool:
            futures = {pool.submit(probe, n): n for n in sorted(names)}
            for future in as_completed(futures):
                kind, name, content = future.result()
                if kind == "dir_ds":
                    dirs_with_ds.append((name, content))
                elif kind == "dir_nods":
                    dirs_without_ds.append(name)
                elif kind == "file":
                    files.append(name)

        dirs_with_ds.sort(key=lambda x: x[0])
        dirs_without_ds.sort()
        files.sort()

        return dirs_with_ds, dirs_without_ds, files

    def _scan_dir(self, rel_path: str, ds_content: bytes, depth: int) -> None:
        """Recursively scan a directory given its .DS_Store content."""
        if rel_path in self.scanned_dirs:
            return
        if self.max_depth > 0 and depth > self.max_depth:
            return

        self.scanned_dirs.add(rel_path)
        base_url = self.url if not rel_path else urljoin(self.url + "/", rel_path)
        indent = "  " * depth

        display_path = rel_path + "/" if rel_path else "/"
        self._log(f"{indent}{_c(COLOR_DIR, f'[DIR] {display_path}', self.color)}")
        self.result.entries.append(Entry(path=display_path, is_dir=True, url=base_url + "/"))

        if self.download and rel_path:
            os.makedirs(os.path.join(self.download_dir, rel_path), exist_ok=True)

        names = self._parse_dsstore(ds_content)
        if not names:
            return

        dirs_with_ds, dirs_without_ds, files = self._classify_entries(base_url, names)

        # Files
        for name in files:
            file_rel = f"{rel_path}/{name}" if rel_path else name
            file_url = urljoin(base_url + "/", name)
            entry = Entry(path=file_rel, is_dir=False, url=file_url)

            self._log(f"{indent}  {_c(COLOR_FILE, f'[FILE] {file_rel}', self.color)}")

            if self.download:
                local_path = os.path.join(self.download_dir, file_rel)
                if self._download_file(file_url, local_path):
                    entry.downloaded = True
                    self._log(f"{indent}  {_c(COLOR_DL, f'[DOWNLOAD] {file_rel}', self.color)}")

            self.result.entries.append(entry)

        # Dirs without .DS_Store
        for name in dirs_without_ds:
            dir_rel = f"{rel_path}/{name}" if rel_path else name
            dir_url = urljoin(base_url + "/", name + "/")
            self._log(f"{indent}  {_c(COLOR_DIR, f'[DIR] {dir_rel}/ (no .DS_Store)', self.color)}")
            self.result.entries.append(Entry(path=dir_rel + "/", is_dir=True, url=dir_url))

        # Recurse into dirs with .DS_Store
        for name, content in dirs_with_ds:
            dir_rel = f"{rel_path}/{name}" if rel_path else name
            self._scan_dir(dir_rel, content, depth + 1)

    def scan(self) -> ScanResult:
        """Main entry point. Returns ScanResult."""
        self._log(_c(COLOR_INFO, f"[INFO] Scanning {self.url}", self.color))
        self._log(_c(COLOR_INFO, "[INFO] Fetching root .DS_Store...", self.color))

        root_content = self._fetch_dsstore(self.url)
        if root_content is None:
            msg = f"[ERROR] No .DS_Store found at {self.url}/.DS_Store"
            if self.json_output:
                print(json.dumps({"error": msg}))
            else:
                print(_c(COLOR_ERR, msg, self.color))
            sys.exit(1)

        self._log(_c(COLOR_INFO, "[INFO] Root .DS_Store found! Starting recursive scan...\n", self.color))

        if self.download:
            os.makedirs(self.download_dir, exist_ok=True)
            self._log(_c(COLOR_INFO, f"[INFO] Downloading to: {self.download_dir}/", self.color))

        self._scan_dir("", root_content, depth=0)

        # Summary
        if self.json_output:
            print(json.dumps(self.result.to_dict(), indent=2))
        else:
            self._log(f"\n{'=' * 60}")
            self._log("[SUMMARY]")
            self._log(f"  Directories: {len(self.result.dirs)}")
            self._log(f"  Files:       {len(self.result.files)}")
            if self.download:
                self._log(f"  Downloaded to: {self.download_dir}/")
            self._log("=" * 60)

        return self.result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="  dsstore-tree — discover files and directories via exposed .DS_Store files.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("-u", "--url", required=True, help="Base URL to scan (e.g., https://example.com)")
    parser.add_argument("-d", "--download", action="store_true", help="Download and mirror discovered files")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress informational output")
    parser.add_argument("-j", "--json", action="store_true", dest="json_output", help="Output results as JSON")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output")
    parser.add_argument("--depth", type=int, default=0, metavar="N", help="Max recursion depth (0 = unlimited)")
    parser.add_argument("--timeout", type=int, default=10, metavar="SEC", help="HTTP timeout in seconds (default: 10)")
    parser.add_argument("--threads", type=int, default=10, metavar="N", help="Concurrent requests (default: 10)")
    parser.add_argument("--proxy", type=str, default=None, metavar="URL", help="HTTP/SOCKS proxy (e.g., http://127.0.0.1:8080)")
    parser.add_argument("-H", "--header", action="append", default=[], metavar="K:V", help="Custom header (repeatable)")
    parser.add_argument("-o", "--output", type=str, default=None, metavar="FILE", help="Write JSON results to file")

    args = parser.parse_args()

    use_color = sys.stdout.isatty() and not args.no_color and not args.json_output
    if not args.quiet and not args.json_output:
        print(BANNER)

    headers: dict[str, str] = {}
    for h in args.header:
        if ":" in h:
            k, v = h.split(":", 1)
            headers[k.strip()] = v.strip()

    scanner = Scanner(
        args.url,
        download=args.download,
        quiet=args.quiet,
        color=use_color,
        max_depth=args.depth,
        timeout=args.timeout,
        threads=args.threads,
        proxy=args.proxy,
        headers=headers or None,
        json_output=args.json_output,
    )

    result = scanner.scan()

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result.to_dict(), f, indent=2)
        if not args.quiet:
            print(f"[INFO] Results written to {args.output}")


if __name__ == "__main__":
    main()
