"""Verify a native build's locked wheels and installed inputs without importing them.

The wheelhouse is downloaded separately with hash checking. This verifier never
downloads, installs, repairs metadata, or interprets package setup code.
"""

from email.parser import BytesParser
import hashlib
import importlib.machinery
import importlib.metadata
import importlib.util
import json
import marshal
from pathlib import Path, PurePosixPath
import re
import site
import stat
import sys
import sysconfig
import types
import zipfile

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import parse_tag, sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version


MEDIAPIPE_WHEEL = "mediapipe-0.10.21-cp311-cp311-macosx_11_0_universal2.whl"
MEDIAPIPE_SHA256 = "b4946abad54d62812ce4c543187373a4872985fd66cd43fe7437f4b95d53d9ec"
BOOTSTRAP_DISTRIBUTIONS = frozenset({"pip"})
GENERATED_METADATA = frozenset({"RECORD", "INSTALLER", "REQUESTED", "direct_url.json"})


def _digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _relative_path(value):
    path = PurePosixPath(value)
    if (not value or "\\" in value or "\x00" in value or path.is_absolute()
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError("Unsafe wheel path: " + value)
    return path


def _regular(path, prefix):
    """Reject package symlinks, including ancestors below the trusted root."""
    path, prefix = Path(path), Path(prefix).resolve()
    try:
        relative = path.relative_to(prefix)
    except ValueError:
        raise ValueError("Installed dependency escaped the isolated venv.") from None
    cursor = prefix
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError("Dependency input must not be a symlink: " + str(path))
    if not path.is_file():
        raise ValueError("Missing installed dependency file: " + str(path))
    return path


def _read_lock(path):
    rows = Path(path).read_text(encoding="utf-8").replace("\\\n", " ")
    locked = {}
    for row in rows.splitlines():
        row = row.split("#", 1)[0].strip()
        if not row:
            continue
        chunks = re.split(r"\s+--hash=", row)
        requirement = Requirement(chunks[0])
        specs = list(requirement.specifier)
        if (requirement.url or requirement.extras or requirement.marker or len(specs) != 1
                or specs[0].operator != "==" or "*" in specs[0].version or len(chunks) < 2
                or any(not re.fullmatch(r"sha256:[0-9a-f]{64}", item) for item in chunks[1:])):
            raise ValueError("Expected only exact versions and SHA-256 hashes in native lock.")
        name = canonicalize_name(requirement.name)
        if name in locked:
            raise ValueError("Duplicate locked distribution: " + name)
        locked[name] = {"version": Version(specs[0].version),
                        "hashes": {item.removeprefix("sha256:") for item in chunks[1:]}}
    if not locked:
        raise ValueError("Native dependency lock is empty.")
    return locked


def _environment():
    prefix = Path(sys.prefix).resolve()
    if prefix == Path(sys.base_prefix).resolve():
        raise ValueError("An isolated venv is required for native inputs.")
    config = prefix / "pyvenv.cfg"
    entries = {key.strip(): value.strip() for key, value in
               (line.lower().split("=", 1) for line in config.read_text().splitlines() if "=" in line)}
    if entries.get("include-system-site-packages") != "false":
        raise ValueError("System site packages must be explicitly disabled in pyvenv.cfg.")
    if site.ENABLE_USER_SITE:
        raise ValueError("User site packages must be disabled.")
    paths = {name: Path(sysconfig.get_path(name)).resolve()
             for name in ("purelib", "platlib", "scripts", "data")}
    paths["headers"] = prefix / "include" / "site" / ("python" + sysconfig.get_python_version())
    if any(path != prefix and prefix not in path.parents for path in paths.values()):
        raise ValueError("Python installation scheme escapes the isolated venv.")
    roots = {paths["purelib"], paths["platlib"]}
    for entry in sys.path:
        path = Path(entry or ".").resolve()
        if any(part in {"site-packages", "dist-packages"} for part in path.parts) and path not in roots:
            raise ValueError("An external site-packages directory is visible.")
    return {"prefix": prefix, "paths": paths, "distributions": list(importlib.metadata.distributions()),
            "tags": set(sys_tags()), "markers": default_environment()}


def _installed_distributions(environment, locked):
    result, bootstrap = {}, {}
    roots = {environment["paths"]["purelib"], environment["paths"]["platlib"]}
    for distribution in environment["distributions"]:
        name = canonicalize_name(distribution.metadata["Name"], validate=True)
        if name in result or name in bootstrap:
            raise ValueError("Duplicate installed distribution: " + name)
        if Path(distribution.locate_file("")).resolve() not in roots:
            raise ValueError("Distribution outside the isolated site: " + name)
        if name not in locked:
            if name not in BOOTSTRAP_DISTRIBUTIONS:
                raise ValueError("Unapproved extra distribution: " + name)
            bootstrap[name] = distribution
        else:
            if Version(distribution.version) != locked[name]["version"]:
                raise ValueError("Installed version differs from lock: " + name)
            result[name] = distribution
    missing = locked.keys() - result.keys()
    if missing:
        raise ValueError("Missing installed dependencies: " + ", ".join(sorted(missing)))
    return result, bootstrap


def _wheels(wheelhouse, locked, supported):
    directory = Path(wheelhouse)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("Wheelhouse must be a regular directory.")
    result = {}
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file() or path.suffix != ".whl":
            raise ValueError("Wheelhouse must contain only regular wheel files.")
        name, version, _, tags = parse_wheel_filename(path.name)
        if name not in locked or version != locked[name]["version"]:
            raise ValueError("Unapproved wheel: " + path.name)
        if name in result:
            raise ValueError("Wheelhouse must contain exactly one wheel per distribution: " + name)
        checksum = _digest(path)
        if checksum not in locked[name]["hashes"]:
            raise ValueError("Wheel hash differs from lock: " + path.name)
        if not tags & supported:
            raise ValueError("Wheel filename is incompatible with this interpreter: " + path.name)
        result[name] = {"path": path, "sha256": checksum, "tags": tags}
    if result.keys() != locked.keys():
        raise ValueError("Wheelhouse is missing locked dependencies.")
    return result


def _member_destination(name, dist_info, data_dir, pure, paths, distribution_name):
    relative = _relative_path(name)
    if relative.parts[0] == data_dir:
        if len(relative.parts) < 3 or relative.parts[1] not in paths:
            raise ValueError("Unsupported wheel .data scheme: " + name)
        scheme = relative.parts[1]
        root = paths[scheme]
        if scheme == "headers":
            root /= distribution_name
        return root.joinpath(*relative.parts[2:]), scheme
    if relative.parts[0].endswith((".dist-info", ".data")) and relative.parts[0] != dist_info:
        raise ValueError("Wheel contains another distribution's metadata/data directory.")
    return paths["purelib" if pure else "platlib"].joinpath(*relative.parts), None


def _bind_wheel(name, wheel, distribution, environment, claimed):
    path, prefix, paths = wheel["path"], environment["prefix"], environment["paths"]
    files, generated, issues = [], set(), []
    with zipfile.ZipFile(path) as archive:
        members = [entry for entry in archive.infolist() if not entry.is_dir()]
        if len({entry.filename for entry in members}) != len(members):
            raise ValueError("Wheel contains duplicate archive paths.")
        dist_infos = {PurePosixPath(entry.filename).parts[0] for entry in members
                      if entry.filename.endswith(".dist-info/METADATA")
                      and len(PurePosixPath(entry.filename).parts) == 2}
        if len(dist_infos) != 1:
            raise ValueError("Wheel must contain one top-level metadata directory: " + path.name)
        dist_info = dist_infos.pop()
        data_dir = dist_info.removesuffix(".dist-info") + ".data"
        metadata = BytesParser().parsebytes(archive.read(dist_info + "/METADATA"))
        if (canonicalize_name(metadata["Name"]) != name
                or Version(metadata["Version"]) != Version(distribution.version)):
            raise ValueError("Wheel metadata does not match filename/installed distribution.")
        wheel_metadata = BytesParser().parsebytes(archive.read(dist_info + "/WHEEL"))
        tags = set().union(*(parse_tag(value) for value in wheel_metadata.get_all("Tag", [])))
        if not tags:
            raise ValueError("Wheel metadata has no compatibility tags.")
        if tags != wheel["tags"] or not tags & environment["tags"]:
            known = (path.name == MEDIAPIPE_WHEEL and wheel["sha256"] == MEDIAPIPE_SHA256
                     and tags == set(parse_tag("cp311-cp311-macosx_14_0_x86_64")))
            if not known:
                raise ValueError("Wheel filename and internal compatibility tags disagree: " + name)
            issues.append({"code": "mediapipe-0.10.21-internal-wheel-tag", "distribution": name,
                           "wheelSha256": wheel["sha256"], "internalTags": sorted(map(str, tags)),
                           "releaseEligible": False,
                           "source": "https://github.com/google-ai-edge/mediapipe/issues/6112"})
        pure_value = wheel_metadata.get("Root-Is-Purelib", "").lower()
        if pure_value not in {"true", "false"}:
            raise ValueError("Wheel does not declare Root-Is-Purelib.")
        for entry in members:
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("Wheel must not contain symlinks.")
            destination, scheme = _member_destination(entry.filename, dist_info, data_dir,
                                                      pure_value == "true", paths, name)
            destination = _regular(destination, prefix)
            if destination in claimed:
                raise ValueError("Two approved inputs overlap: " + str(destination))
            claimed.add(destination)
            if entry.filename == dist_info + "/RECORD":
                generated.add(destination)
                continue
            expected = archive.read(entry)
            actual = destination.read_bytes()
            if scheme == "scripts" and expected.split(b"\n", 1)[0] in {b"#!python", b"#!pythonw"}:
                # PEP 427 permits the installer to rewrite this interpreter line.
                if (b"\n" not in actual or not actual.startswith(b"#!")
                        or expected.split(b"\n", 1)[1] != actual.split(b"\n", 1)[1]):
                    raise ValueError("Installed wheel script content differs: " + entry.filename)
            elif actual != expected:
                raise ValueError("Installed wheel content differs: " + entry.filename)
            files.append({"path": str(destination.relative_to(prefix)), "sha256": hashlib.sha256(actual).hexdigest()})
        info_root = (paths["purelib"] if pure_value == "true" else paths["platlib"]) / dist_info
        for filename in GENERATED_METADATA:
            candidate = info_root / filename
            if candidate.exists():
                generated.add(_regular(candidate, prefix))
        for entry in distribution.entry_points:
            if entry.group in {"console_scripts", "gui_scripts"}:
                if len(_relative_path(entry.name).parts) != 1:
                    raise ValueError("Entry point has an unsafe script name.")
                generated.add(_regular(paths["scripts"] / entry.name, prefix))
    return {"name": name, "version": distribution.version, "wheel": path.name,
            "wheelSha256": wheel["sha256"], "files": sorted(files, key=lambda item: item["path"])}, generated, issues


def _closure(distributions, environment):
    """Resolve active Requires-Dist markers, including transitively selected extras."""
    selected = {name: {""} for name in distributions}
    changed = True
    while changed:
        changed = False
        for name, distribution in distributions.items():
            requires_python = distribution.metadata.get("Requires-Python")
            if requires_python and not SpecifierSet(requires_python).contains(environment["python_full_version"]):
                raise ValueError("Dependency does not support this Python: " + name)
            for value in distribution.requires or []:
                requirement = Requirement(value)
                if requirement.marker and not any(requirement.marker.evaluate({**environment, "extra": extra})
                                                   for extra in selected[name]):
                    continue
                dependency = canonicalize_name(requirement.name)
                if requirement.url or dependency not in distributions:
                    raise ValueError("Missing or unapproved transitive dependency: " + name + " -> " + value)
                if not requirement.specifier.contains(distributions[dependency].version):
                    raise ValueError("Transitive dependency version conflict: " + name + " -> " + value)
                extras = {canonicalize_name(item) for item in requirement.extras}
                provided = {canonicalize_name(item) for item in distributions[dependency].metadata.get_all("Provides-Extra", [])}
                if extras - provided:
                    raise ValueError("Required dependency extra is not provided: " + value)
                if extras - selected[dependency]:
                    selected[dependency].update(extras)
                    changed = True


def _bytecode(path, allowed_sources):
    try:
        source = Path(importlib.util.source_from_cache(str(path)))
    except ValueError:
        raise ValueError("Unapproved legacy/orphan bytecode: " + str(path)) from None
    if source not in allowed_sources:
        raise ValueError("Bytecode has no approved source: " + str(path))
    content = path.read_bytes()
    if content[:4] != importlib.util.MAGIC_NUMBER or len(content) < 16:
        raise ValueError("Bytecode has an invalid interpreter header.")
    code = marshal.loads(content[16:])
    if not isinstance(code, types.CodeType) or Path(code.co_filename).resolve() != source.resolve():
        raise ValueError("Bytecode points to a different source file.")
    match = re.search(r"\.opt-([12])\.pyc$", path.name)
    expected = compile(source.read_bytes(), code.co_filename, "exec", optimize=int(match[1]) if match else 0)
    if code != expected:
        raise ValueError("Installed bytecode differs from approved source: " + str(path))


def _sdk_inputs(root, prefix):
    """Bind only the pinned SDK's two module names and local CMake aliases."""
    files, aliases = set(), []
    for name in ("pyrealsense2", "pyrsutils"):
        targets = set()
        for suffix in importlib.machinery.EXTENSION_SUFFIXES:
            candidates = {name + suffix, name + ".2.54" + suffix, name + ".2.54.2" + suffix}
            for filename in sorted(candidates):
                path = root / filename
                if not path.exists() and not path.is_symlink():
                    continue
                if path.is_symlink():
                    target = path.readlink()
                    if target.is_absolute() or len(target.parts) != 1 or target.name not in candidates:
                        raise ValueError("SDK alias escapes its approved module family.")
                    resolved = path.resolve(strict=True)
                    if resolved.parent != root or resolved.name != name + ".2.54.2" + suffix:
                        raise ValueError("SDK alias does not resolve to the pinned 2.54.2 module.")
                    _regular(resolved, prefix)
                    aliases.append({"path": str(path.relative_to(prefix)), "target": str(target),
                                    "targetSha256": _digest(resolved)})
                else:
                    if filename == name + ".2.54" + suffix:
                        raise ValueError("SDK minor-version name must be an alias, not another input.")
                    resolved = _regular(path, prefix)
                    files.add(resolved)
                targets.add(resolved)
        if len(targets) > 1:
            raise ValueError("Ambiguous installed SDK module: " + name)
    return files, aliases


def verify_dependencies(lock, wheelhouse):
    """Return a deterministic input binding; every mismatch raises ValueError.

    The precisely hash-bound upstream MediaPipe WHEEL defect is reported rather
    than repaired. Its presence always makes this result release-ineligible.
    SDK extension files are separately fingerprinted here; the caller MUST also
    validate both against the existing SDK source/binary provenance contract.
    """
    lock, wheelhouse = Path(lock), Path(wheelhouse)
    if lock.is_symlink() or not lock.is_file():
        raise ValueError("Native lock must be a regular file.")
    lock_hash = _digest(lock)
    locked = _read_lock(lock)
    environment = _environment()
    prefix, paths = environment["prefix"], environment["paths"]
    distributions, bootstrap = _installed_distributions(environment, locked)
    wheels = _wheels(wheelhouse, locked, environment["tags"])
    claimed, generated, issues, bindings = set(), set(), [], []
    for name in sorted(distributions):
        binding, additions, problems = _bind_wheel(name, wheels[name], distributions[name], environment, claimed)
        bindings.append(binding)
        generated.update(additions)
        issues.extend(problems)
    _closure(distributions, environment["markers"])
    bootstrap_files = set()
    for name, distribution in bootstrap.items():
        for item in distribution.files or []:
            path = Path(distribution.locate_file(item)).resolve()
            # The sole bootstrap exception cannot claim another package's files.
            relative = path.relative_to(prefix)
            in_package = any(path.is_relative_to(root / "pip") or any(
                part.startswith("pip-") and part.endswith(".dist-info") for part in path.relative_to(root).parts[:1])
                for root in {paths["purelib"], paths["platlib"]} if path.is_relative_to(root))
            in_script = path.parent == paths["scripts"] and re.fullmatch(r"pip(?:3(?:\.11)?)?", path.name)
            if not in_package and not in_script:
                raise ValueError("Bootstrap distribution claimed an unrelated file: " + str(relative))
            bootstrap_files.add(_regular(path, prefix))
    generated.update(bootstrap_files)
    sdk_modules, sdk_aliases, bytecode = set(), [], set()
    sources = {path for path in claimed | bootstrap_files if path.suffix == ".py"}
    for root in {paths["purelib"], paths["platlib"]}:
        native_files, native_aliases = _sdk_inputs(root, prefix)
        sdk_modules.update(native_files)
        sdk_aliases.extend(native_aliases)
        alias_paths = {prefix / item["path"] for item in native_aliases}
        for path in root.rglob("*"):
            if path in alias_paths:
                continue
            if path.is_symlink():
                raise ValueError("Site packages contains a symlink: " + str(path))
            if not path.is_file():
                continue
            if path in claimed or path in generated:
                continue
            if path in native_files:
                continue
            elif path.suffix == ".pyc":
                _bytecode(path, sources)
                bytecode.add(path)
            else:
                raise ValueError("Unapproved installed input: " + str(path.relative_to(prefix)))
    extra_inputs = [{"path": str(path.relative_to(prefix)), "sha256": _digest(path)}
                    for path in sorted(generated | sdk_modules | bytecode)]
    if _digest(lock) != lock_hash or any(_digest(item["path"]) != item["sha256"] for item in wheels.values()):
        raise ValueError("Dependency inputs changed during verification.")
    result = {"schemaVersion": 1, "lockSha256": lock_hash,
              "versions": {name: distribution.version for name, distribution in sorted(distributions.items())},
              "wheels": [{"filename": item["path"].name, "sha256": item["sha256"]}
                         for _, item in sorted(wheels.items())],
              "distributions": bindings, "installerGeneratedAndSdkInputs": extra_inputs,
              "sdkAliases": sorted(sdk_aliases, key=lambda item: item["path"]),
              "bootstrapExceptions": {name: item.version for name, item in sorted(bootstrap.items())},
              "sdkRequiresSeparateProvenance": True, "metadataIssues": issues,
              "releaseEligible": not issues}
    result["fingerprint"] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result
