"""Discover registered bases without connecting to them or exporting their sources."""
from __future__ import annotations

import hashlib
import ntpath
import os
from pathlib import Path


def read_list_text(path: Path):
    if path.stat().st_size > 10_000_000:
        raise ValueError("Файл списка превышает 10 МБ.")
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeError:
        return data.decode("cp1251")


def connection_fields(text: str):
    """Parse key=value; with quoted values, semicolons and doubled quotes."""
    fields, pos = {}, 0
    while pos < len(text):
        while pos < len(text) and text[pos] in " ;\t\r\n":
            pos += 1
        if pos == len(text):
            break
        equals = text.find("=", pos)
        if equals < 0:
            raise ValueError("Некорректная строка подключения.")
        key = text[pos:equals].strip().casefold()
        if not key or ";" in key:
            raise ValueError("Некорректная строка подключения.")
        pos = equals + 1
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos < len(text) and text[pos] == '"':
            pos += 1
            value, closed = [], False
            while pos < len(text):
                if text[pos] == '"':
                    if pos + 1 < len(text) and text[pos+1] == '"':
                        value.append('"')
                        pos += 2
                    else:
                        closed = True
                        pos += 1
                        break
                else:
                    value.append(text[pos])
                    pos += 1
            if not closed:
                raise ValueError("Незакрытая кавычка в строке подключения.")
            value = "".join(value)
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos < len(text) and text[pos] != ";":
                raise ValueError("После кавычки ожидается ';'.")
        else:
            end = text.find(";", pos)
            end = end if end >= 0 else len(text)
            value, pos = text[pos:end].strip(), end
        if key in fields:
            raise ValueError("Повторный параметр в строке подключения.")
        fields[key] = value
        if pos < len(text):
            pos += 1
    return fields


def identity(kind, location):
    value = ntpath.normcase(ntpath.normpath(location)) if kind == "file" else location.casefold()
    return kind + ":" + value


def base_id(kind, location):
    return "base-" + hashlib.sha256(identity(kind, location).encode()).hexdigest()[:16]


def parse_v8i(text, source):
    records, current = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = {"name": line[1:-1]}
            records.append(current)
        elif current is not None and "=" in line:
            key, value = line.split("=", 1)
            current[key.strip().casefold()] = value.strip()
    result, warnings = [], []
    for record in records:
        if not record.get("connect"):
            continue  # Folders in the launcher's tree.
        try:
            values = connection_fields(record["connect"])
            if values.get("file"):
                kind, location = "file", values["file"]
            elif values.get("srvr") and values.get("ref"):
                kind, location = "server", values["srvr"] + "\\" + values["ref"]
            elif values.get("ws"):
                # Don't expose credentials or query parameters of an arbitrary web URL.
                warnings.append({"source": str(source), "name": record["name"],
                                 "reason": "Веб-подключение не поддерживает пакетный конфигуратор."})
                continue
            else:
                raise ValueError("Не найден File или Srvr/Ref.")
            result.append({"base_id": base_id(kind, location), "name": record["name"],
                "kind": kind, "location": location, "folder": record.get("folder", "/"),
                "version_hint": record.get("version", ""), "source": str(source)})
        except ValueError as error:
            # Never return the raw connection string: it might contain Usr/Pwd.
            warnings.append({"source": str(source), "name": record["name"], "reason": str(error)})
    return result, warnings


def launcher_sources(extra_lists=()):
    roaming = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "1C/1CEStart"
    system = Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "1C/1CEStart"
    lists = [roaming / "ibases.v8i"] + [Path(os.path.expandvars(p)) for p in extra_lists]
    configs = [roaming / "1cestart.cfg", system / "1cestart.cfg", system / "1cescmn.cfg"]
    warnings, seen = [], set()
    while configs and len(seen) < 16:
        path = configs.pop(0)
        key = str(path.resolve()).casefold()
        if key in seen:
            continue
        seen.add(key)
        if not path.exists():
            continue
        try:
            for line in read_list_text(path).splitlines():
                if "=" not in line or line.lstrip().startswith(("#", ";")):
                    continue
                name, value = line.split("=", 1)
                if name.strip().casefold() not in {"commoninfobases", "commoncfglocation"}:
                    continue
                value = os.path.expandvars(value.strip().strip('"'))
                if value.lower().startswith(("http://", "https://")):
                    warnings.append({"source": str(path), "reason": "Удалённый URL списка не читается; добавьте локальный .v8i."})
                    continue
                target = Path(value)
                if not target.is_absolute():
                    target = path.parent / target
                if name.strip().casefold() == "commoninfobases":
                    lists.append(target)
                else:
                    configs.append(target)
        except (OSError, UnicodeError, ValueError) as error:
            warnings.append({"source": str(path), "reason": type(error).__name__})
    return list(dict.fromkeys(lists)), warnings


def discover(extra_lists=()):
    paths, warnings = launcher_sources(extra_lists)
    entries, sources = {}, []
    for path in paths:
        try:
            records, errors = parse_v8i(read_list_text(path), path)
            sources.append(str(path))
            warnings += errors
            for record in records:
                key = identity(record["kind"], record["location"])
                if key not in entries:
                    entries[key] = record | {"aliases": [record["name"]], "sources": [str(path)]}
                else:
                    existing = entries[key]
                    if record["name"] not in existing["aliases"]:
                        existing["aliases"].append(record["name"])
                    if str(path) not in existing["sources"]:
                        existing["sources"].append(str(path))
        except FileNotFoundError:
            warnings.append({"source": str(path), "reason": "Файл списка не найден."})
        except (OSError, UnicodeError, ValueError) as error:
            warnings.append({"source": str(path), "reason": type(error).__name__})
    return {"bases": list(entries.values()), "sources": sources, "warnings": warnings,
            "note": "Это зарегистрированные базы из списка запуска 1С; доступ к самим базам ещё не проверялся."}
