# Apt Parser

A work-in-progress apt mirror parser/scanner/downloader.

## Usage

The tool has a --help function which can get you started. Notable parameters include:

* `--download DOWNLOAD` - A path to a directory to save files into
* `--url-file URL_FILE` - A file path to save a list of .deb file URLs to
* `--input/output-file` - A file path to save the parsed packages data to, or to load it from.

Beyond those options, the script requires sources lists to detect mirrors from; files are read
as-is, and directories are scanned for `.list` files. The default sources used are the default
sources in Debian and Ubuntu - `/etc/apt/sources.list` and `/etc/apt/sources.list.d`.

Several existing `.list` files were checked into this repo by accident, and can be used for testing.

## TODO

* Instead of going straight for the Packages.gz files:
  * Download the InRelease file
  * Scan it for file targets
  * Parse it for metadata, including supported architectures
  * Remove duplicate file targets (e.g. if `Packages.xz` is available, don't download `Packages`)
  * Remove non-selected architectures (default should be to onloy download the current architecture)
  * Optionally download source packages as well
  * Automatically write out an output-file, and use that to determine differences (new packages)
  * Add option to download all versions, not just the latest ones (yuk)
  * ...more!