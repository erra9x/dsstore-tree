# dsstore-tree

Recursively discover files and directories on web servers by parsing exposed `.DS_Store` files.

![dsstore-tree](static/dsstore-tree-demo.png)

## Features

- **Recursive discovery** — follows nested `.DS_Store` files to map full directory trees
- **Concurrent scanning** — parallel HTTP requests for fast enumeration
- **Download & mirror** — optionally download all discovered files preserving directory structure
- **JSON output** — machine-readable results for integration with other tools
- **Proxy support** — route through HTTP/SOCKS proxies (e.g., Burp Suite)
- **Custom headers** — add cookies, auth tokens, or any HTTP headers
- **Depth limiting** — control recursion depth
- **Colored output** — visual distinction between files, directories, and status messages

## Installation

Install via `pipx` (recommended):

```
pipx install git+https://github.com/vflame6/dsstore-tree.git
```

Or manually:

```
git clone https://github.com/vflame6/dsstore-tree.git
cd dsstore-tree
pip3 install -r requirements.txt
```

## Usage

```
dsstore-tree -u https://example.com
```

### Options

```
-u, --url URL       Base URL to scan (required)
-d, --download      Download and mirror discovered files
-q, --quiet         Suppress informational output
-j, --json          Output results as JSON
-o, --output FILE   Write JSON results to file
-H, --header K:V    Custom header (repeatable)
--proxy URL         HTTP/SOCKS proxy (e.g., http://127.0.0.1:8080)
--threads N         Concurrent requests (default: 10)
--timeout SEC       HTTP timeout in seconds (default: 10)
--depth N           Max recursion depth (0 = unlimited)
--no-color          Disable colored output
```

### Examples

Basic scan:
```
dsstore-tree -u https://target.com
```

Scan through Burp Suite proxy:
```
dsstore-tree -u https://target.com --proxy http://127.0.0.1:8080
```

Download all files and save JSON report:
```
dsstore-tree -u https://target.com -d -o report.json
```

With authentication:
```
dsstore-tree -u https://target.com -H "Cookie: session=abc123"
```

JSON output for piping:
```
dsstore-tree -u https://target.com -j | jq '.files[].path'
```

## About .DS_Store

`.DS_Store` (Desktop Services Store) is a hidden file created by macOS Finder to store folder display preferences (icon positions, view options, etc.). When deployed to web servers accidentally, these files leak the names of files and directories — even those not linked or indexed anywhere.

This is a known information disclosure vulnerability. dsstore-tree automates the exploitation by recursively parsing these files to reconstruct the full directory tree of a web application.

## License

MIT
