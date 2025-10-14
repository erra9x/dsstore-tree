import argparse
import tempfile
import os
import requests
import sys
from urllib.parse import urljoin, urlparse
from argparse import RawTextHelpFormatter

try:
    from ds_store import DSStore
except ImportError:
    print("[ERROR] ds-store library not found. Install it with: pip install ds-store")
    sys.exit(1)

BANNER = r"""       __          __                        __               
  ____/ /_________/ /_____  ________        / /_________  ___ 
 / __  / ___/ ___/ __/ __ \/ ___/ _ \______/ __/ ___/ _ \/ _ \
/ /_/ (__  |__  ) /_/ /_/ / /  /  __/_____/ /_/ /  /  __/  __/
\__,_/____/____/\__/\____/_/   \___/      \__/_/   \___/\___/
"""


class Scanner:
    def __init__(self, url, download, quiet):
        self.url = url.rstrip('/')
        self.download = download
        self.download_dir = "dsstore-tree_" + urlparse(url).netloc
        self.quiet = quiet
        self.found_files = []
        self.found_dirs = []
        self.scanned_dirs = set()

    def is_valid_name(self, entry_name):
        """Validate entry name to prevent directory traversal attacks"""
        if entry_name.find('..') >= 0 or \
                entry_name.startswith('/') or \
                entry_name.startswith('\\') or \
                entry_name in ['.', '..', ''] or \
                not entry_name:
            return False
        return True

    def download_file(self, url, local_path):
        """Download a file from URL to local path"""
        try:
            r = requests.get(url, stream=True, timeout=10, verify=False)
            if r.status_code == 200:
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                with open(local_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                return True
        except Exception as e:
            if not self.quiet:
                print(f"[ERROR] Failed to download {url}: {e}")
        return False

    def parse_dsstore(self, content):
        """Parse .DS_Store content and return list of filenames"""
        filenames = set()
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp.write(content)
                tmp.flush()
                tmp_path = tmp.name

            d = DSStore.open(tmp_path, 'r+')

            # Extract filenames from .DS_Store
            for entry in d:
                filename = entry.filename
                if filename and self.is_valid_name(filename):
                    filenames.add(filename)

            d.close()
            os.unlink(tmp_path)
        except Exception as e:
            if not self.quiet:
                print(f"[ERROR] Failed to parse .DS_Store: {e}")

        return filenames

    def check_if_directory(self, base_url, name):
        """Check if an entry is a directory by looking for .DS_Store inside it"""
        dsstore_url = urljoin(base_url + '/', name + '/.DS_Store')
        has_dsstore = False
        dsstore_content = None

        try:
            r = requests.get(dsstore_url, timeout=10, verify=False)
            if r.status_code == 200:
                has_dsstore = True
                dsstore_content = r.content
        except Exception:
            pass

        # Even if no .DS_Store, check if it's a directory by trying to access with trailing slash
        is_directory = has_dsstore
        if not has_dsstore:
            # Check if accessing the name redirects (301/302) or if name/ is accessible
            try:
                r = requests.head(urljoin(base_url + '/', name), timeout=10, allow_redirects=False, verify=False)
                if r.status_code in [301, 302]:
                    is_directory = True
            except Exception:
                pass

        return is_directory, dsstore_content

    def check_if_file(self, url):
        """Check if an entry is an accessible file (not a directory)"""
        try:
            r = requests.head(url, timeout=10, allow_redirects=False, verify=False)
            # If it redirects (301/302), it's probably a directory
            if r.status_code in [301, 302]:
                return False
            if r.status_code == 200:
                return True
            # Some servers don't support HEAD, try GET
            r = requests.get(url, timeout=10, stream=True, allow_redirects=False, verify=False)
            # If it redirects, it's a directory
            if r.status_code in [301, 302]:
                return False
            if r.status_code == 200:
                return True
        except Exception:
            pass
        return False

    def scan_directory_recursively(self, directory_name, parent_path, depth=0):
        """Recursively scan a directory for .DS_Store files using depth-first search"""
        # Build full URL and local path
        if parent_path:
            full_url = urljoin(self.url + '/', parent_path + '/' + directory_name)
            local_path = os.path.join(self.download_dir, parent_path, directory_name)
            relative_path = os.path.join(parent_path, directory_name)
        else:
            full_url = urljoin(self.url + '/', directory_name)
            local_path = os.path.join(self.download_dir, directory_name)
            relative_path = directory_name

        # Skip if already scanned
        if relative_path in self.scanned_dirs:
            return

        self.scanned_dirs.add(relative_path)

        # Check if directory has .DS_Store
        is_dir, dsstore_content = self.check_if_directory(self.url, relative_path)

        if not is_dir or not dsstore_content:
            # No .DS_Store to parse, can't enumerate contents
            return

        # Print directory (only once, when actually scanning it)
        indent = "  " * depth
        print(f"{indent}[DIR] {relative_path}/")
        self.found_dirs.append(relative_path)

        if self.download:
            os.makedirs(local_path, exist_ok=True)

        # Parse .DS_Store to get entries
        filenames = self.parse_dsstore(dsstore_content)

        # Separate files and directories for better ordering
        files = []
        directories = []

        for name in sorted(filenames):
            entry_url = urljoin(full_url + '/', name)
            entry_local_path = os.path.join(local_path, name)
            entry_relative_path = os.path.join(relative_path, name)

            # Check if it's a directory first
            is_subdir, subdir_dsstore = self.check_if_directory(full_url, name)

            if is_subdir:
                # It's a directory
                if subdir_dsstore:
                    # Has .DS_Store, will scan it recursively
                    directories.append((name, entry_relative_path, True))
                else:
                    # No .DS_Store, just list it
                    directories.append((name, entry_relative_path, False))
            else:
                # Check if it's a file
                if self.check_if_file(entry_url):
                    files.append((name, entry_url, entry_local_path, entry_relative_path))

        # Print and process files first
        for name, entry_url, entry_local_path, entry_relative_path in files:
            print(f"{indent}  [FILE] {entry_relative_path}")
            self.found_files.append(entry_relative_path)

            if self.download:
                if self.download_file(entry_url, entry_local_path):
                    print(f"{indent}  [DOWNLOAD] {entry_relative_path}")

        # Then recursively process directories (depth-first)
        for name, entry_relative_path, has_dsstore in directories:
            if has_dsstore:
                # Recursively scan this directory immediately (depth-first)
                self.scan_directory_recursively(name, relative_path, depth + 1)
            else:
                # Just list the directory without scanning
                print(f"{indent}  [DIR] {entry_relative_path}/ (no .DS_Store)")
                self.found_dirs.append(entry_relative_path)

    def scan_from_root(self, initial_response):
        """Start scanning from root .DS_Store"""
        if self.download:
            os.makedirs(self.download_dir, exist_ok=True)
            if not self.quiet:
                print(f"[INFO] Files will be downloaded to: {self.download_dir}/")

        print(f"[DIR] /")
        self.found_dirs.append("/")

        # Parse root .DS_Store
        filenames = self.parse_dsstore(initial_response.content)

        # Separate files and directories
        files = []
        directories = []

        for name in sorted(filenames):
            entry_url = urljoin(self.url + '/', name)
            entry_local_path = os.path.join(self.download_dir, name)

            # Check if it's a directory
            is_dir, dir_dsstore = self.check_if_directory(self.url, name)

            if is_dir:
                if dir_dsstore:
                    # Has .DS_Store, will scan recursively
                    directories.append((name, True))
                else:
                    # No .DS_Store, just list it
                    directories.append((name, False))
            else:
                # Check if it's a file
                if self.check_if_file(entry_url):
                    files.append((name, entry_url, entry_local_path))

        # Print and process root-level files first
        for name, entry_url, entry_local_path in files:
            print(f"  [FILE] {name}")
            self.found_files.append(name)

            if self.download:
                if self.download_file(entry_url, entry_local_path):
                    print(f"  [DOWNLOAD] {name}")

        # Then recursively scan directories (depth-first)
        for name, has_dsstore in directories:
            if has_dsstore:
                # Recursively scan immediately (depth-first)
                self.scan_directory_recursively(name, '', depth=1)
            else:
                # Just list the directory
                print(f"  [DIR] {name}/ (no .DS_Store)")
                self.found_dirs.append(name)

    def scan(self):
        """Main scanning function"""
        if not self.quiet:
            print(f"[INFO] Scanning {self.url}")
            print(f"[INFO] Trying to access root .DS_Store...")

        try:
            r = requests.get(self.url + "/.DS_Store", timeout=10, verify=False)
            if r.status_code != 200:
                print(f"[ERROR] Failed to access .DS_Store (HTTP {r.status_code})")
                print(f"[ERROR] URL: {r.url}")
                sys.exit(1)

            if not self.quiet:
                print(f"[200] {r.url}")
                print(f"[INFO] Root .DS_Store found! Starting recursive scan...\n")

            self.scan_from_root(r)

            if not self.quiet:
                print(f"\n{'=' * 60}")
                print(f"[SUMMARY]")
                print(f"  Directories found: {len(self.found_dirs)}")
                print(f"  Files found: {len(self.found_files)}")
                if self.download:
                    print(f"  Downloaded to: {self.download_dir}/")
                print(f"{'=' * 60}")

        except requests.exceptions.RequestException as e:
            print(f"[ERROR] Network error: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"[ERROR] Unexpected error: {e}")
            sys.exit(1)

def main():
    parser = argparse.ArgumentParser(
        description=rf"  dsstore-tree is a tool to discover files and directories through .DS_Store exposure.",
        formatter_class=RawTextHelpFormatter
    )

    parser.add_argument('-u', '--url', help='Base URL to scan (e.g., https://example.com)', required=True)
    parser.add_argument('-d', '--download', help='Download and mirror discovered files and directories',
                        action="store_true")
    parser.add_argument('-q', '--quiet', help="Supress output where possible", action="store_true")

    args = parser.parse_args()

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    if not args.quiet:
        print(BANNER)

    # Suppress SSL warnings when verify=False
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    s = Scanner(args.url, args.download, args.quiet)
    s.scan()

if __name__ == "__main__":
    main()
