#!/usr/bin/env python3

import re
import os
import sys
import gzip
import time
import pathlib
import warnings

from types import SimpleNamespace

from clint.textui import progress, colored
from tqdm import tqdm

import humanfriendly
import requests
import apt_pkg

SOURCES_LIST = "/etc/apt/sources.list"
SOURCES_LISTDIR = "/etc/apt/sources.list.d/"

SOURCES_FILES = []
SOURCES_DIRS = []

USER_AGENT = "Debian APT-HTTP/1.3 (1.6.12ubuntu0.1)"

URL_MATCHER = re.compile("https?://(.*)\.(gz|xz|lzma|bz2)")

def get_larger_version(pkg1, pkg2):
    # Ignore deprecation warning from apt_pkg.version_compare
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = apt_pkg.version_compare(pkg1.version, pkg2.version)
    if res >= 1:
        return pkg1
    else:
        return pkg2


def filter_deb_lines(line):
    if line.startswith("deb"):
        try:
            return line[:line.index("#")].strip()
        except:
            return line.strip()


def get_deb_lines():
    packagefiles = SOURCES_FILES
    for sources_dir in SOURCES_DIRS:
        sources_files = os.listdir(sources_dir)
        for sources_file in sources_files:
            packagefiles.append(os.path.join(sources_dir, sources_file))
    #packagefiles.extend([os.path.join(SOURCES_DIRS, f) for f in os.listdir(SOURCES_DIRS)])
    packagelines = []
    listfiles = [lf for lf in packagefiles if lf.endswith(".list")]
    for listfile in listfiles:
        lines = open(listfile).read().strip().split("\n")
        lines = [l for l in map(filter_deb_lines, lines) if l]
        packagelines.extend(lines)
    return packagelines


def get_packages_from_deb_line(deb_line):
    source_type, source_uri, source_release, source_components = re.split("\s+", deb_line, maxsplit=3)
    source_components = re.split(r"\s+", source_components)

    release_data = []

    print(f"{source_release}: processing...")

    for source_component in source_components:
        packages_file = os.path.join(source_uri,
                                     "dists",
                                     source_release,
                                     source_component,
                                     "binary-amd64/Packages.gz"
        )
        local_file_name = URL_MATCHER.match(packages_file).group(1).replace("/", "_")
        local_file_path = os.path.join("/var/lib/apt/lists", local_file_name)

        if os.path.isfile(local_file_path):
            print(f"{source_release}/{source_component}: cached...", end="\r", flush=True)
            data = open(local_file_path).read()
            status = "C"
        else:
            print(f"{source_release}/{source_component}: fetching...", end="\r", flush=True)
            req = requests.get(packages_file)
            if req.status_code == 200:
                data = gzip.decompress(req.content).decode()
                status = "R"
            else:
                print(f"Got status code {req.status_code} from URL {packages_file}")
                raise(ValueError)

        component_data = [d.strip("\n") + f"\nuri: {source_uri}" for d in data.strip("\n").split("\n\n") if d]
        print(f"{source_release}/{source_component}: {len(component_data)} entries  ({status})")
        release_data.extend(component_data)

    print(f"{source_release}: {len(release_data)} entries")
    return release_data


if len(sys.argv) == 1:
    print("Parsing default sources lists")
    SOURCES_FILES.append(SOURCES_LIST)
    SOURCES_DIRS.append(SOURCES_LISTDIR)
else:
    for arg in sys.argv[1:]:
        print(f"Processing argv {arg}")
        if os.path.isfile(arg):
            print(f"argv {arg} is a file")
            SOURCES_FILES.append(arg)
        elif os.path.isdir(arg):
            print(f"argv {arg} is a dir")
            SOURCES_DIRS.append(arg)
        else:
            print(f"Ignoring argument because it's not a file or dir: {arg}")

apt_pkg.init()

fetch = True
output_dir = "/tmp/apt-download"

package_data = []

for deb_line in get_deb_lines():
    release_data = get_packages_from_deb_line(deb_line)
    package_data.extend(release_data)

packages = {}

for package in package_data:
    if not package:
        continue
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
    package = SimpleNamespace(**pkg)

    try:
        name = package.package
    except:
        print(package)
    if name in packages.keys():
        packages[name] = get_larger_version(packages[name], package)
    else:
        packages[name] = package

pkg_len = ver_len = 0

for p,v in [(len(package.package), len(package.version)) for package in packages.values()]:
    if p > pkg_len:
        pkg_len = p
    if v > ver_len:
        ver_len = v

pkg_len = min(pkg_len, 30)
ver_len = min(ver_len, 30)

sizes = [package.size for package in packages.values()]
print("Total size: " + humanfriendly.format_size(sum(sizes), binary=False))

for package_name, package in packages.items():
    label = f"{package.package:{pkg_len}}   {package.version:{ver_len}}   "

    if fetch:
        url = f"{package.uri}/{package.filename}"
        target = f"{output_dir}/{package.filename}"

        if os.path.isfile(target) and os.stat(target).st_size == package.size:
            print(f"Package {package_name} already downloaded, skipping")
            continue

        pathlib.Path(target).parent.mkdir(parents=True, exist_ok=True)

        req = requests.get(url, stream=True)
        with open(target,"wb") as output:
            total_length = int(req.headers.get('content-length'))
            bar = progress.Bar(expected_size=total_length,
                               label=label,
                               empty_char=colored.red("\u2588"),
                               filled_char=colored.blue("\u2588"),
                               )
            for chunk in req.iter_content(chunk_size=1024*1024): # 1 MB
                if chunk:
                    output.write(chunk)
                    output.flush()
                    bar.show(output.tell())
            bar.done()
    else:
        print(f"{label} {humanfriendly.format_size(package.size, binary=False):>12}")
