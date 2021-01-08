#!/usr/bin/env python3

import argparse
import gzip
import json
import os
import pathlib
import re
import sys
import typing
import warnings
from types import SimpleNamespace

from typing import Iterator
from typing import List
from typing import Dict
from typing import Any

import apt_pkg
import humanfriendly
import requests

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    TextColumn,
    TransferSpeedColumn,
    TimeRemainingColumn,
    Progress,
    TaskID,
    track
)
from rich.table import Table

console = Console()
print = console.print

SOURCES = ["/etc/apt/sources.list", "/etc/apt/sources.list.d"]

SOURCES_LINE_PAT = re.compile(r"^(?P<source_type>deb|deb-src)\s+(?:\[\S+\]\s+)?(?P<url>https?://\S+)\s+(?P<release>\S+)\s+(?P<components>[^#]+)\s*")
SOURCES_LINE_FILTER_PAT = re.compile(r"(\[[^\]]+\]\s+|\s+?#.*)")

USER_AGENT = "Debian APT-HTTP/1.3 (1.6.12ubuntu0.1)"

URL_MATCHER = re.compile("https?://(.*)\.(gz|xz|lzma|bz2)")

class NamespaceEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, SimpleNamespace):
            obj_data = o.__dict__
            obj_data['__class__'] = "SimpleNamespace"
            o = obj_data
            return(o)
        else:
            return super().default(o)

class DebSrcNotImplemented(NotImplementedError):
    """An exception to handle deb-src lines"""
    pass


class DebSrcLineUnparseable(Exception):
    """An exception to handle sources lines which do not pass our regex"""
    pass


class InvalidListException(Exception):
    """An exception to handle when the given file does not include any valid entries"""
    pass


def flatten(outer: List[List[Any]]) -> Iterator:
    for inner in outer:
        yield from inner

def parse_package_metadata(package: str) -> Dict[str, str]:
    pkg = {}
    lines = package.strip("\n").split("\n")

    while lines:
        line = lines.pop(0)
        try:
            k, v = line.split(": ", 1)
        except ValueError:
            print(f"Bad line: {line}")
            raise

        if k == "Size":
            v = int(v)
        # Look ahead to see if the next line is a continuation

        if lines and lines[0].startswith(" "):
            exp_lines = [v]

            while lines and lines[0].startswith(" "):
                line = lines.pop(0).strip()

                if line == ".":
                    exp_lines.append("")
                else:
                    exp_lines.append(line)
            v = "\n".join(exp_lines)
        pkg[k.lower()] = v
    return pkg

def get_larger_version(pkg1: SimpleNamespace, pkg2: SimpleNamespace) -> SimpleNamespace:
    # Ignore deprecation warning from apt_pkg.version_compare
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = apt_pkg.version_compare(pkg1.version, pkg2.version)

    if res >= 1:
        return pkg1
    else:
        return pkg2


def filter_deb_line(line):
    if line.startswith("deb"):
        try:
            return line[:line.index("#")].strip()
        except ValueError:
            return line.strip()
    else:
        raise DebSrcLineUnparseable()


def get_deb_lines(package_sources: List[str]) -> List[str]:
    package_files: list[str] = []
    for source in package_sources:
        if os.path.isfile(source):
            print(f"Adding file {source}")
            package_files.append(source)
        elif os.path.isdir(source):
            print(f"Adding directory {source}")
            sources_files = [os.path.join(source, f) for f in os.listdir(source)]
            package_files.extend(sources_files)

    packagelines = []
    listfiles = [lf for lf in package_files if lf.endswith(".list")]

    full_sources_lines = flatten([open(listfile).read().splitlines() for listfile in listfiles])

    for sources_line in full_sources_lines:
        sources_line_filtered = SOURCES_LINE_FILTER_PAT.sub("", sources_line).strip()

        if sources_line_filtered.startswith("deb-src"):
            continue

        if sources_line_filtered.startswith("deb"):
            packagelines.append(sources_line_filtered)

    if packagelines:
        return packagelines

    raise InvalidListException()

def get_files_from_deb_line(deb_line: str) -> List[str]:
    deb_line = re.sub(" ?#.*", "", deb_line)
    res = SOURCES_LINE_PAT.match(deb_line)

    if res is None:
        raise DebSrcLineUnparseable(f"Could not parse deb line {repr(deb_line)}")

    results = res.groupdict()

    if results['source_type'] == "deb-src":
        raise DebSrcNotImplemented()

    source_url = results['url']
    source_release = results['release']
    source_components = re.split(r"\s+", results['components'])

    release_data = []

    inrelease_file = os.path.join(source_url,
                                    "dists",
                                    source_release,
                                    "InRelease"
                                    )

    req = requests.get(inrelease_file)
    if req.status_code != 200:
        raise ValueError(f"Could not fetch InRelease file: error {req.status_code}")
    data = req.content.decode()


def get_packages_from_deb_line(deb_line: str) -> List[str]:
    deb_line = re.sub(" ?#.*", "", deb_line)
    res = SOURCES_LINE_PAT.match(deb_line)

    if res is None:
        raise DebSrcLineUnparseable(f"Could not parse deb line {repr(deb_line)}")

    results = res.groupdict()

    if results['source_type'] == "deb-src":
        raise DebSrcNotImplemented()

    source_url = results['url']
    source_release = results['release']
    source_components = re.split(r"\s+", results['components'])

    release_data = []

    for source_component in source_components:
        packages_file = os.path.join(source_url,
                                     "dists",
                                     source_release,
                                     source_component,
                                     "binary-amd64/Packages.gz"
                                     )
        try:
            local_file_name = URL_MATCHER.match(packages_file).group(1).replace("/", "_")
            local_file_path = os.path.join("/var/lib/apt/lists", local_file_name)
        except AttributeError as ae:
            console.log("Couldn't match URL!")
            raise AttributeError from ae

        if os.path.isfile(local_file_path):
            data = open(local_file_path).read()
            status = "[cyan]Cache[/]"
        else:
            req = requests.get(packages_file)

            if req.status_code == 200:
                data = gzip.decompress(req.content).decode()
                status = "[green]Fetch[/]"
            else:
                print(f"Got status code {req.status_code} from URL {packages_file}")
                raise(ValueError)

        component_data = [d.strip("\n") + f"\nuri: {source_url}" for d in data.strip("\n").split("\n\n") if d]
        print(f"{status} {source_release}/{source_component}: {len(component_data)} entries")
        release_data.extend(component_data)

    if len(source_components) != 1:
        print(f"{source_release}: {len(release_data)} entries")

    return release_data

def copy_url(task_id: TaskID, url: str, path: str) -> None:
    """Copy data from a url to a local file."""
    req = requests.get(url, stream=True)

    with open(path, "wb") as output:
        total_length = int(req.headers.get('content-length'))
        progress.update(task_id, total=total_length)

        for chunk in req.iter_content(chunk_size=1024*1024):  # 1 MB
            if chunk:
                output.write(chunk)
                output.flush()
                progress.update(task, advance=len(chunk))
        output.flush()
        progress.remove_task(task_id)

def main() -> None:
    parser = argparse.ArgumentParser(description='Scan, and optionally mirror, an Apt repository')
    parser.add_argument("sources", metavar="list_file", type=str, nargs="*", help="apt .list files to parse (default: system files)")
    parser.add_argument("--download", type=str, help="Download all packages from the given repository to this directory")
    parser.add_argument("--url-file", type=argparse.FileType("w"), help="Save URLs to file")
    parser.add_argument("--print-table", action="store_true", default=False, help="Print the package data to the console as a table")
    parser.add_argument("--output-file", type=argparse.FileType("w"), help="Save repository data to a JSON file")
    parser.add_argument("--input-file", type=argparse.FileType("r"), help="Load repository data from a JSON file")
    args = parser.parse_args()

    packages: typing.Dict[str, SimpleNamespace] = {}

    if args.input_file:
        packages = json.load(args.input_file)
    else:
        apt_pkg.init()
        package_data = []
        try:
            if args.sources:
                deb_lines = get_deb_lines(args.sources)
            else:
                deb_lines = get_deb_lines(SOURCES)
        except InvalidListException:
            print("Error: the specified file contains no valid debian source lines!")
            sys.exit(255)

        for deb_line in track(deb_lines, description="Processing deb lines...", console=console):
            try:
                release_data = get_packages_from_deb_line(deb_line)
                package_data.extend(release_data)
            except DebSrcLineUnparseable:
                print(f"[red]ERR[/] Could not parse line {repr(deb_line)}, skipping")
            except DebSrcNotImplemented:
                print(f"Source repository parsing is not implemented, skipping {repr(deb_line)}")

        for package in track(package_data, description="Processing package data...", console=console):
            if not package:
                continue
            pkg = parse_package_metadata(package)
            package = SimpleNamespace(**pkg)
            name = package.package

            if name in packages.keys():
                packages[name] = get_larger_version(packages[name], package)
            else:
                packages[name] = package

    pkg_len = max([len(package.package) for package in packages.values()])
    ver_len = max([len(package.version) for package in packages.values()])

    sizes = [package.size for package in packages.values()]
    print("Total size: " + humanfriendly.format_size(sum(sizes), binary=False))
    progress = Progress(
        # TextColumn("[bold blue]{task.fields[packagename]}", justify="right"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=None),
        "[progress.percentage]{task.percentage:>3.1f}%",
        "•",
        DownloadColumn(),
        "•",
        TransferSpeedColumn(),
        "•",
        TimeRemainingColumn(),
    )
    if args.download:
        print("Starting download")
        with progress:
            packages_task = progress.add_task("Package downloads", total=len(packages))
            for package_name in sorted(packages.keys()):
                package = packages[package_name]
                url = f"{package.uri}/{package.filename}"
                target = f"{args.download}/{package.filename}"

                if os.path.isfile(target) and os.stat(target).st_size == package.size:
                    print(f"Package {package_name} already downloaded, skipping")

                    continue

                pathlib.Path(target).parent.mkdir(parents=True, exist_ok=True)

                req = requests.get(url, stream=True)

                with open(target, "wb") as output:
                    total_length = int(req.headers.get('content-length'))
                    task = progress.add_task(f"{package_name.ljust(pkg_len)}", total=total_length)

                    for chunk in req.iter_content(chunk_size=1024*1024):  # 1 MB
                        if chunk:
                            output.write(chunk)
                            output.flush()
                            progress.update(task, advance=len(chunk))
                    output.flush()
                    progress.remove_task(task)
                progress.advance(packages_task)

    if args.print_table:
        table = Table(title="Available packages")
        table.add_column("Package name", width=pkg_len+2)
        table.add_column("Version", width=ver_len+2)
        table.add_column("Size", width=12)

        for package_name, package in packages.items():
            table.add_row(package.package, package.version, humanfriendly.format_size(package.size, binary=False))

            if args.url_file:
                args.url_file.write(f"{package_name:{pkg_len}} {package.uri}/{package.filename}\n")
        console.print(table)

    if args.output_file:
        json.dump(packages, args.output_file, cls=NamespaceEncoder)


if __name__ == "__main__":
    main()
