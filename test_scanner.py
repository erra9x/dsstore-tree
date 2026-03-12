"""Unit tests for dsstore_tree.py"""

import argparse
import json
import os
import tempfile
from unittest.mock import MagicMock, Mock, patch, PropertyMock

import pytest

from dsstore_tree import Entry, ScanResult, Scanner, _c, main


# ---------------------------------------------------------------------------
# Helper: _c (color formatting)
# ---------------------------------------------------------------------------

class TestColorHelper:
    def test_color_enabled(self):
        assert _c("\033[1;34m", "hello", True) == "\033[1;34mhello\033[0m"

    def test_color_disabled(self):
        assert _c("\033[1;34m", "hello", False) == "hello"


# ---------------------------------------------------------------------------
# Entry dataclass
# ---------------------------------------------------------------------------

class TestEntry:
    def test_defaults(self):
        e = Entry(path="foo.txt", is_dir=False, url="http://x/foo.txt")
        assert e.downloaded is False

    def test_dir_entry(self):
        e = Entry(path="bar/", is_dir=True, url="http://x/bar/")
        assert e.is_dir is True


# ---------------------------------------------------------------------------
# ScanResult dataclass
# ---------------------------------------------------------------------------

class TestScanResult:
    def _make_result(self):
        r = ScanResult(base_url="http://example.com")
        r.entries = [
            Entry(path="/", is_dir=True, url="http://example.com/"),
            Entry(path="css/", is_dir=True, url="http://example.com/css/"),
            Entry(path="index.html", is_dir=False, url="http://example.com/index.html"),
            Entry(path="style.css", is_dir=False, url="http://example.com/style.css", downloaded=True),
        ]
        return r

    def test_dirs_property(self):
        r = self._make_result()
        assert len(r.dirs) == 2
        assert all(e.is_dir for e in r.dirs)

    def test_files_property(self):
        r = self._make_result()
        assert len(r.files) == 2
        assert all(not e.is_dir for e in r.files)

    def test_to_dict_structure(self):
        r = self._make_result()
        d = r.to_dict()
        assert d["base_url"] == "http://example.com"
        assert len(d["directories"]) == 2
        assert len(d["files"]) == 2
        assert d["summary"]["directories"] == 2
        assert d["summary"]["files"] == 2

    def test_to_dict_file_fields(self):
        r = self._make_result()
        d = r.to_dict()
        f = d["files"][1]  # style.css (downloaded=True)
        assert "path" in f
        assert "url" in f
        assert "downloaded" in f

    def test_empty_result(self):
        r = ScanResult(base_url="http://x")
        assert r.dirs == []
        assert r.files == []
        d = r.to_dict()
        assert d["summary"]["directories"] == 0
        assert d["summary"]["files"] == 0


# ---------------------------------------------------------------------------
# Scanner.__init__
# ---------------------------------------------------------------------------

class TestScannerInit:
    def test_defaults(self):
        s = Scanner("http://example.com/")
        assert s.url == "http://example.com"  # trailing slash stripped
        assert s.download is False
        assert s.quiet is False
        assert s.color is True
        assert s.max_depth == 0
        assert s.timeout == 10
        assert s.threads == 10
        assert s.json_output is False

    def test_custom_params(self):
        s = Scanner(
            "http://test.com",
            download=True,
            quiet=True,
            color=False,
            max_depth=3,
            timeout=30,
            threads=5,
            json_output=True,
        )
        assert s.download is True
        assert s.quiet is True
        assert s.color is False  # json_output forces color off
        assert s.max_depth == 3
        assert s.timeout == 30
        assert s.threads == 5

    def test_proxy_set(self):
        s = Scanner("http://x.com", proxy="http://127.0.0.1:8080")
        assert s.session.proxies["http"] == "http://127.0.0.1:8080"
        assert s.session.proxies["https"] == "http://127.0.0.1:8080"

    def test_custom_headers(self):
        s = Scanner("http://x.com", headers={"X-Custom": "val"})
        assert s.session.headers["X-Custom"] == "val"

    def test_json_output_disables_color(self):
        s = Scanner("http://x.com", color=True, json_output=True)
        assert s.color is False

    def test_download_dir_based_on_netloc(self):
        s = Scanner("http://target.local:8080/path")
        assert s.download_dir == "dsstore-tree_target.local:8080"


# ---------------------------------------------------------------------------
# Scanner._is_valid_name (static method)
# ---------------------------------------------------------------------------

class TestIsValidName:
    @pytest.mark.parametrize("name", [
        "index.html",
        "css",
        "my-file.txt",
        "a",
        "file with spaces",
        "UPPERCASE",
        ".htaccess",
        ".hidden",
    ])
    def test_valid_names(self, name):
        assert Scanner._is_valid_name(name) is True

    @pytest.mark.parametrize("name,reason", [
        ("", "empty string"),
        (".", "single dot"),
        ("..", "double dot"),
        ("foo/../bar", "path traversal"),
        ("foo..bar", "contains .."),
        ("/etc/passwd", "starts with /"),
        ("\\windows", "starts with backslash"),
    ])
    def test_invalid_names(self, name, reason):
        assert Scanner._is_valid_name(name) is False, f"Should reject: {reason}"

    def test_none_is_falsy(self):
        # _is_valid_name checks `not name` first, None would be falsy
        assert Scanner._is_valid_name("") is False


# ---------------------------------------------------------------------------
# Scanner._parse_dsstore
# ---------------------------------------------------------------------------

class TestParseDsstore:
    def test_parses_entries(self):
        """Mock DSStore.open to return fake entries."""
        s = Scanner("http://x.com", quiet=True)
        mock_entry1 = Mock()
        mock_entry1.filename = "index.html"
        mock_entry2 = Mock()
        mock_entry2.filename = "css"
        mock_entry3 = Mock()
        mock_entry3.filename = ".."  # should be filtered

        mock_ds = MagicMock()
        mock_ds.__enter__ = Mock(return_value=iter([mock_entry1, mock_entry2, mock_entry3]))
        mock_ds.__exit__ = Mock(return_value=False)

        with patch("dsstore_tree.DSStore.open", return_value=mock_ds):
            result = s._parse_dsstore(b"fake content")

        assert result == {"index.html", "css"}

    def test_handles_parse_error(self):
        """On exception, returns empty set."""
        s = Scanner("http://x.com", quiet=True)
        with patch("dsstore_tree.DSStore.open", side_effect=Exception("corrupt")):
            result = s._parse_dsstore(b"bad data")
        assert result == set()

    def test_filters_invalid_names(self):
        s = Scanner("http://x.com", quiet=True)
        mock_entry = Mock()
        mock_entry.filename = "/etc/passwd"

        mock_ds = MagicMock()
        mock_ds.__enter__ = Mock(return_value=iter([mock_entry]))
        mock_ds.__exit__ = Mock(return_value=False)

        with patch("dsstore_tree.DSStore.open", return_value=mock_ds):
            result = s._parse_dsstore(b"fake")
        assert result == set()

    def test_empty_filename_skipped(self):
        s = Scanner("http://x.com", quiet=True)
        mock_entry = Mock()
        mock_entry.filename = ""

        mock_ds = MagicMock()
        mock_ds.__enter__ = Mock(return_value=iter([mock_entry]))
        mock_ds.__exit__ = Mock(return_value=False)

        with patch("dsstore_tree.DSStore.open", return_value=mock_ds):
            result = s._parse_dsstore(b"fake")
        assert result == set()

    def test_none_filename_skipped(self):
        s = Scanner("http://x.com", quiet=True)
        mock_entry = Mock()
        mock_entry.filename = None

        mock_ds = MagicMock()
        mock_ds.__enter__ = Mock(return_value=iter([mock_entry]))
        mock_ds.__exit__ = Mock(return_value=False)

        with patch("dsstore_tree.DSStore.open", return_value=mock_ds):
            result = s._parse_dsstore(b"fake")
        assert result == set()

    def test_temp_file_cleanup(self):
        """Ensure temp file is cleaned up even on success."""
        s = Scanner("http://x.com", quiet=True)
        created_files = []

        original_mkstemp = tempfile.mkstemp
        def tracking_mkstemp(*args, **kwargs):
            fd, path = original_mkstemp(*args, **kwargs)
            created_files.append(path)
            return fd, path

        mock_ds = MagicMock()
        mock_ds.__enter__ = Mock(return_value=iter([]))
        mock_ds.__exit__ = Mock(return_value=False)

        with patch("dsstore_tree.tempfile.mkstemp", side_effect=tracking_mkstemp):
            with patch("dsstore_tree.DSStore.open", return_value=mock_ds):
                s._parse_dsstore(b"data")

        for f in created_files:
            assert not os.path.exists(f), "Temp file should be cleaned up"


# ---------------------------------------------------------------------------
# Scanner._fetch_dsstore
# ---------------------------------------------------------------------------

class TestFetchDsstore:
    def test_returns_content_on_200(self):
        s = Scanner("http://x.com", quiet=True)
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.content = b"\x00\x00\x00\x01Bud1"
        s.session.get = Mock(return_value=mock_resp)

        result = s._fetch_dsstore("http://x.com")
        assert result == b"\x00\x00\x00\x01Bud1"
        s.session.get.assert_called_once_with("http://x.com/.DS_Store", timeout=10)

    def test_returns_none_on_404(self):
        s = Scanner("http://x.com", quiet=True)
        mock_resp = Mock()
        mock_resp.status_code = 404
        mock_resp.content = b""
        s.session.get = Mock(return_value=mock_resp)

        assert s._fetch_dsstore("http://x.com") is None

    def test_returns_none_on_empty_200(self):
        s = Scanner("http://x.com", quiet=True)
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.content = b""
        s.session.get = Mock(return_value=mock_resp)

        assert s._fetch_dsstore("http://x.com") is None

    def test_returns_none_on_exception(self):
        s = Scanner("http://x.com", quiet=True)
        s.session.get = Mock(side_effect=Exception("connection error"))
        assert s._fetch_dsstore("http://x.com") is None


# ---------------------------------------------------------------------------
# Scanner._is_accessible_file
# ---------------------------------------------------------------------------

class TestIsAccessibleFile:
    def test_200_is_file(self):
        s = Scanner("http://x.com", quiet=True)
        mock_resp = Mock()
        mock_resp.status_code = 200
        s.session.head = Mock(return_value=mock_resp)
        assert s._is_accessible_file("http://x.com/test.txt") is True

    def test_301_is_not_file(self):
        s = Scanner("http://x.com", quiet=True)
        mock_resp = Mock()
        mock_resp.status_code = 301
        s.session.head = Mock(return_value=mock_resp)
        assert s._is_accessible_file("http://x.com/dir") is False

    def test_302_is_not_file(self):
        s = Scanner("http://x.com", quiet=True)
        mock_resp = Mock()
        mock_resp.status_code = 302
        s.session.head = Mock(return_value=mock_resp)
        assert s._is_accessible_file("http://x.com/dir") is False

    def test_404_is_not_file(self):
        s = Scanner("http://x.com", quiet=True)
        mock_resp = Mock()
        mock_resp.status_code = 404
        s.session.head = Mock(return_value=mock_resp)
        assert s._is_accessible_file("http://x.com/missing") is False

    def test_exception_returns_false(self):
        s = Scanner("http://x.com", quiet=True)
        s.session.head = Mock(side_effect=Exception("timeout"))
        assert s._is_accessible_file("http://x.com/x") is False


# ---------------------------------------------------------------------------
# Scanner._classify_entries
# ---------------------------------------------------------------------------

class TestClassifyEntries:
    def test_classifies_dir_with_dsstore(self):
        """Entry with a reachable .DS_Store → dir_ds."""
        s = Scanner("http://x.com", quiet=True, threads=1)
        ds_content = b"fake_ds_content"

        def mock_fetch(url):
            # _classify_entries calls _fetch_dsstore with the entry URL (e.g. http://x.com/css)
            if url.rstrip("/").endswith("/css") or url.rstrip("/").endswith("x.com/css"):
                return ds_content
            return None

        s._fetch_dsstore = mock_fetch
        s._is_accessible_file = Mock(return_value=False)
        s.session.head = Mock()  # won't be called if _fetch_dsstore returns content

        dirs_with, dirs_without, files = s._classify_entries("http://x.com", {"css"})
        assert len(dirs_with) == 1
        assert dirs_with[0] == ("css", ds_content)
        assert dirs_without == []
        assert files == []

    def test_classifies_dir_without_dsstore(self):
        """Entry with redirect but no .DS_Store → dir_nods."""
        s = Scanner("http://x.com", quiet=True, threads=1)
        s._fetch_dsstore = Mock(return_value=None)

        mock_resp = Mock()
        mock_resp.status_code = 301
        s.session.head = Mock(return_value=mock_resp)
        s._is_accessible_file = Mock(return_value=False)

        dirs_with, dirs_without, files = s._classify_entries("http://x.com", {"images"})
        assert dirs_with == []
        assert dirs_without == ["images"]
        assert files == []

    def test_classifies_file(self):
        """Entry that returns 200 on HEAD → file."""
        s = Scanner("http://x.com", quiet=True, threads=1)
        s._fetch_dsstore = Mock(return_value=None)

        mock_resp = Mock()
        mock_resp.status_code = 404
        s.session.head = Mock(return_value=mock_resp)
        s._is_accessible_file = Mock(return_value=True)

        dirs_with, dirs_without, files = s._classify_entries("http://x.com", {"readme.txt"})
        assert dirs_with == []
        assert dirs_without == []
        assert files == ["readme.txt"]

    def test_unclassifiable_entry_ignored(self):
        """Entry that's not a dir and not an accessible file → ignored."""
        s = Scanner("http://x.com", quiet=True, threads=1)
        s._fetch_dsstore = Mock(return_value=None)

        mock_resp = Mock()
        mock_resp.status_code = 404
        s.session.head = Mock(return_value=mock_resp)
        s._is_accessible_file = Mock(return_value=False)

        dirs_with, dirs_without, files = s._classify_entries("http://x.com", {"ghost"})
        assert dirs_with == []
        assert dirs_without == []
        assert files == []

    def test_multiple_entries_sorted(self):
        """Results should be sorted alphabetically."""
        s = Scanner("http://x.com", quiet=True, threads=1)
        s._fetch_dsstore = Mock(return_value=None)

        mock_resp = Mock()
        mock_resp.status_code = 404
        s.session.head = Mock(return_value=mock_resp)
        s._is_accessible_file = Mock(return_value=True)

        _, _, files = s._classify_entries("http://x.com", {"z.txt", "a.txt", "m.txt"})
        assert files == ["a.txt", "m.txt", "z.txt"]


# ---------------------------------------------------------------------------
# Scanner._log
# ---------------------------------------------------------------------------

class TestLog:
    def test_log_prints_when_not_quiet(self, capsys):
        s = Scanner("http://x.com", quiet=False, json_output=False)
        s._log("hello")
        assert "hello" in capsys.readouterr().out

    def test_log_silent_when_quiet(self, capsys):
        s = Scanner("http://x.com", quiet=True)
        s._log("hello")
        assert capsys.readouterr().out == ""

    def test_log_silent_when_json(self, capsys):
        s = Scanner("http://x.com", json_output=True)
        s._log("hello")
        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Scanner._download_file
# ---------------------------------------------------------------------------

class TestDownloadFile:
    def test_successful_download(self, tmp_path):
        s = Scanner("http://x.com", quiet=True)
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.iter_content = Mock(return_value=[b"chunk1", b"chunk2"])
        s.session.get = Mock(return_value=mock_resp)

        dest = str(tmp_path / "sub" / "file.txt")
        assert s._download_file("http://x.com/file.txt", dest) is True
        assert os.path.exists(dest)
        with open(dest, "rb") as f:
            assert f.read() == b"chunk1chunk2"

    def test_failed_download_non_200(self, tmp_path):
        s = Scanner("http://x.com", quiet=True)
        mock_resp = Mock()
        mock_resp.status_code = 403
        s.session.get = Mock(return_value=mock_resp)

        dest = str(tmp_path / "file.txt")
        assert s._download_file("http://x.com/file.txt", dest) is False

    def test_failed_download_exception(self, tmp_path):
        s = Scanner("http://x.com", quiet=True)
        s.session.get = Mock(side_effect=Exception("network error"))

        dest = str(tmp_path / "file.txt")
        assert s._download_file("http://x.com/file.txt", dest) is False


# ---------------------------------------------------------------------------
# Scanner.scan (integration-ish, with mocked HTTP)
# ---------------------------------------------------------------------------

class TestScan:
    def test_scan_no_dsstore_exits(self):
        s = Scanner("http://x.com", quiet=True, json_output=True)
        s._fetch_dsstore = Mock(return_value=None)
        with pytest.raises(SystemExit) as exc_info:
            s.scan()
        assert exc_info.value.code == 1

    def test_scan_with_entries(self):
        s = Scanner("http://x.com", quiet=True)
        s._fetch_dsstore = Mock(return_value=b"root_ds")
        s._parse_dsstore = Mock(return_value={"file.txt"})
        s._classify_entries = Mock(return_value=([], [], ["file.txt"]))
        s._is_accessible_file = Mock(return_value=True)

        result = s.scan()
        assert len(result.dirs) >= 1  # at least root dir
        assert any(e.path == "file.txt" for e in result.files)

    def test_scan_respects_max_depth(self):
        """With max_depth=1, should not recurse past depth 1."""
        s = Scanner("http://x.com", quiet=True, max_depth=1)
        call_count = 0

        original_scan_dir = s._scan_dir

        def tracking_scan_dir(rel_path, ds_content, depth):
            nonlocal call_count
            call_count += 1
            original_scan_dir(rel_path, ds_content, depth)

        s._scan_dir = tracking_scan_dir
        s._fetch_dsstore = Mock(return_value=b"root")
        # Root parse returns a dir, that dir also has DS_Store
        ds_inner = b"inner_ds"

        def mock_parse(content):
            if content == b"root":
                return {"subdir"}
            return {"deep_file"}

        s._parse_dsstore = mock_parse
        s._classify_entries = Mock(side_effect=[
            ([("subdir", ds_inner)], [], []),  # root level: subdir has DS_Store
            ([], [], ["deep_file"]),            # subdir level
        ])

        s.scan()
        # Should have scanned root + subdir (depth 0 + depth 1), but subdir won't recurse further
        assert call_count == 2


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

class TestCLI:
    def test_url_required(self):
        with pytest.raises(SystemExit):
            with patch("sys.argv", ["dsstore_tree"]):
                main()

    def test_basic_args(self):
        """Test that main() creates Scanner with correct args."""
        with patch("sys.argv", ["dsstore_tree", "-u", "http://test.com", "-q", "--json"]):
            with patch("dsstore_tree.Scanner") as MockScanner:
                mock_instance = MagicMock()
                mock_instance.scan.return_value = ScanResult(base_url="http://test.com")
                MockScanner.return_value = mock_instance

                main()

                MockScanner.assert_called_once()
                call_kwargs = MockScanner.call_args
                assert call_kwargs[0][0] == "http://test.com"
                assert call_kwargs[1]["quiet"] is True
                assert call_kwargs[1]["json_output"] is True

    def test_header_parsing(self):
        with patch("sys.argv", ["dsstore_tree", "-u", "http://t.com", "-q", "-H", "X-Token:abc123", "-H", "Auth:Bearer xyz"]):
            with patch("dsstore_tree.Scanner") as MockScanner:
                mock_instance = MagicMock()
                mock_instance.scan.return_value = ScanResult(base_url="http://t.com")
                MockScanner.return_value = mock_instance

                main()

                call_kwargs = MockScanner.call_args[1]
                assert call_kwargs["headers"] == {"X-Token": "abc123", "Auth": "Bearer xyz"}

    def test_output_file(self, tmp_path):
        out_file = str(tmp_path / "out.json")
        with patch("sys.argv", ["dsstore_tree", "-u", "http://t.com", "-q", "--json", "-o", out_file]):
            with patch("dsstore_tree.Scanner") as MockScanner:
                mock_instance = MagicMock()
                result = ScanResult(base_url="http://t.com")
                mock_instance.scan.return_value = result
                MockScanner.return_value = mock_instance

                main()

                assert os.path.exists(out_file)
                with open(out_file) as f:
                    data = json.load(f)
                assert data["base_url"] == "http://t.com"
