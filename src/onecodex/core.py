from __future__ import annotations

import contextlib
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath


class BridgeError(Exception):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def safe_path(root: Path, relative: str) -> Path:
    relative = relative.replace("\\", "/")
    parts = PurePosixPath(relative).parts
    if not parts or any(p in {".", ".."} or ":" in p for p in parts):
        raise BridgeError("Нужен относительный путь без '..' и буквы диска.")
    if relative.startswith("/") or any(c in relative for c in "\r\n\x00"):
        raise BridgeError("Недопустимый путь.")
    root = root.resolve()
    result = (root / relative).resolve()
    if not result.is_relative_to(root) or result == root:
        raise BridgeError("Путь выходит за каталог исходников.")
    return result


def tag(node):
    return node.tag.rsplit("}", 1)[-1]


def child(node, name):
    return next((n for n in node if tag(n) == name), None)


def xml_value(node):
    if not len(node):
        return node.text or ""
    result = {}
    for item in node:
        name, value = tag(item), xml_value(item)
        if name not in result:
            result[name] = value
        elif isinstance(result[name], list):
            result[name].append(value)
        else:
            result[name] = [result[name], value]
    return result


def parse_metadata(path: Path, *, detailed=True):
    # DTD/entity expansion isn't needed in Designer dumps.
    raw = path.read_bytes()
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise BridgeError("DTD и сущности в XML не поддерживаются.")
    if not detailed:
        stack = []
        for event, node in ET.iterparse(io.BytesIO(raw), events=("start", "end")):
            if event == "start":
                stack.append(node)
            else:
                if len(stack) == 3 and tag(stack[0]) == "MetaDataObject" and tag(node) == "Properties":
                    obj = stack[1]
                    name_node = child(node, "Name")
                    if name_node is None or not name_node.text:
                        return None
                    synonym = child(node, "Synonym")
                    version = child(node, "Version")
                    return {"address": f"{tag(obj)}.{name_node.text}", "kind": tag(obj),
                            "name": name_node.text, "uuid": obj.attrib.get("uuid"),
                            "properties": {"Synonym": xml_value(synonym) if synonym is not None else "",
                                           "Version": version.text if version is not None else None}}
                stack.pop()
        return None
    root = ET.fromstring(raw)
    if tag(root) != "MetaDataObject":
        return None
    obj = next((n for n in root if child(n, "Properties") is not None), None)
    if obj is None:
        return None
    props = child(obj, "Properties")
    name_node = child(props, "Name")
    if name_node is None or not name_node.text:
        return None
    name = name_node.text
    return {"address": f"{tag(obj)}.{name}", "kind": tag(obj), "name": name,
            "uuid": obj.attrib.get("uuid"), "properties": xml_value(props),
            "children": xml_value(child(obj, "ChildObjects"))
            if child(obj, "ChildObjects") is not None else {}}


class Bridge:
    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path).resolve()
        self.config = read_json(self.config_path)
        if not self.config:
            raise BridgeError(f"Нет файла подключений: {self.config_path}")
        self.state = (self.config_path.parent / self.config.get("state_dir", "state")).resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="onecodex")
        self.jobs = {}
        self.jobs_lock = threading.Lock()

    def profile(self, connection):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", connection):
            raise BridgeError("Недопустимое имя подключения.")
        profiles = self.config.get("connections", {})
        if connection not in profiles:
            raise BridgeError(f"Нет подключения {connection}.")
        p = profiles[connection]
        if p.get("kind") not in {"file", "server"}:
            raise BridgeError("kind должен быть file или server.")
        if not p.get("location"):
            raise BridgeError("Не задан location.")
        return p

    def directory(self, connection):
        self.profile(connection)
        return self.state / connection

    def sources(self, connection):
        return self.directory(connection) / "sources"

    def list_connections(self):
        self.config = read_json(self.config_path, self.config)
        discovery = self.discover_bases()
        registered = [{"id": name, "kind": self.profile(name)["kind"],
                 "name": self.profile(name).get("name", name), "configured": True,
                 "location": self.profile(name)["location"],
                 "extension": self.profile(name).get("extension"),
                 "allow_apply": bool(self.profile(name).get("allow_apply", False)),
                 "snapshot": read_json(self.directory(name) / "snapshot.json")}
                for name in self.config.get("connections", {})]
        for item in registered:
            item["configuration"] = None
            if item["snapshot"] and item["snapshot"].get("ready"):
                try:
                    config = parse_metadata(self.sources(item["id"]) / "Configuration.xml", detailed=False)
                    if config:
                        item["configuration"] = {"name": config["name"], "version": config["properties"].get("Version"),
                                                 "from_last_sync": True}
                except (OSError, ET.ParseError, BridgeError):
                    pass
        for base in discovery["bases"]:
            if base["connection_ids"]:
                for item in registered:
                    if item["id"] in base["connection_ids"]:
                        item["name"] = base["name"]
                        item["base_id"] = base["base_id"]
                        item["version_hint"] = base["version_hint"]
            else:
                registered.append({"id": base["base_id"], "base_id": base["base_id"],
                    "name": base["name"], "kind": base["kind"], "location": base["location"],
                    "version_hint": base["version_hint"], "configured": False, "allow_apply": False,
                    "snapshot": None, "configuration": None, "next": "import_base"})
        return registered

    def discover_bases(self):
        from .discovery import discover, identity
        self.config = read_json(self.config_path, self.config)
        result = discover(self.config.get("discovery_lists", []))
        for base in result["bases"]:
            base["connection_ids"] = [name for name, profile in self.config.get("connections", {}).items()
                if not profile.get("extension") and identity(profile["kind"], profile["location"])
                == identity(base["kind"], base["location"])]
        return result

    def add_connection(self, connection, kind, location, user="", password_env="", platform="", extension="", name="", required_version="", *, allow_apply=False):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", connection):
            raise BridgeError("ID: латинские буквы, цифры, '-' и '_'.")
        if kind not in {"file", "server"} or not location or any(c in location for c in "\r\n\x00"):
            raise BridgeError("Нужен kind file/server и путь к базе либо сервер\\имя базы.")
        lock = self.state / "connections.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise BridgeError("Изменение подключений уже выполняется.")
        try:
            os.close(fd)
            config = read_json(self.config_path)
            if connection in config.get("connections", {}):
                raise BridgeError("Подключение уже существует; существующие настройки не перезаписаны.")
            profile = {"kind": kind, "location": location, "user": user, "allow_apply": bool(allow_apply)}
            for key, value in {"password_env": password_env, "platform": platform, "extension": extension,
                               "name": name, "required_version": required_version}.items():
                if value:
                    profile[key] = value
            config.setdefault("connections", {})[connection] = profile
            write_json(self.config_path, config)
            self.config = config
            return {"added": connection, "profile": profile, "next": "sync_sources"}
        finally:
            lock.unlink(missing_ok=True)

    def import_base(self, base_id, user="", password_env="", platform=""):
        result = self.discover_bases()
        found = next((b for b in result["bases"] if b["base_id"] == base_id), None)
        if not found:
            raise BridgeError("База не найдена. Сначала выполните discover_bases.")
        if found["connection_ids"]:
            return {"connection": found["connection_ids"][0], "already_configured": True,
                    "note": "Существующие параметры пользователя, платформы и записи сохранены."}
        return self.add_connection(base_id, found["kind"], found["location"], user, password_env,
                                   platform, name=found["name"], required_version=found["version_hint"])

    @contextlib.contextmanager
    def locked(self, connection):
        folder = self.directory(connection)
        folder.mkdir(parents=True, exist_ok=True)
        lock = folder / "operation.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise BridgeError("Подключение занято. Если процесс аварийно завершился, удалите operation.lock вручную.")
        try:
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            yield
        finally:
            lock.unlink(missing_ok=True)

    def command(self, connection, operation: list[str], log: Path, *, secrets=False):
        p = self.profile(connection)
        exe = p.get("platform") or self.config.get("platform")
        if not exe or not Path(exe).is_file():
            raise BridgeError("Не найден 1cv8.exe; задайте platform в файле подключений.")
        required = p.get("required_version")
        version = Path(exe).parent.parent.name
        if required and re.fullmatch(r"\d+(?:\.\d+)+", version) and not (version == required or version.startswith(required + ".")):
            raise BridgeError(f"В списке запуска для базы указана платформа {required}, выбрана {version}. Задайте подходящий platform в профиле.")
        args = [exe, "DESIGNER", "/F" if p["kind"] == "file" else "/S", p["location"]]
        if p.get("user"):
            args += ["/N", p["user"]]
        if p.get("password_env"):
            password = os.environ.get(p["password_env"])
            if password is None:
                raise BridgeError(f"Не задана переменная {p['password_env']} с паролем.")
            args += ["/P", password if secrets else "<hidden>"]
        args += ["/DisableStartupDialogs", "/DisableStartupMessages"] + operation
        args += ["/Out", str(log)]
        return args

    def extension_args(self, connection):
        name = self.profile(connection).get("extension")
        return ["-Extension", name] if name else []

    def run(self, connection, operation, label, job=None):
        folder = self.directory(connection) / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / f"{time.time_ns()}-{label}.log"
        args = self.command(connection, operation, log, secrets=True)
        if job:
            self.progress(job, label)
        timeout = int(self.profile(connection).get("timeout_sec", 1800))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, timeout=timeout,
                                     creationflags=flags, check=False)
        except subprocess.TimeoutExpired:
            raise BridgeError(f"Таймаут {label}; состояние операции необходимо проверить. Журнал: {log}")
        text = ""
        if log.exists():
            data = log.read_bytes()
            for encoding in ("utf-8-sig", "utf-16", "cp1251"):
                try:
                    text = data.decode(encoding)
                    break
                except UnicodeError:
                    pass
        p = self.profile(connection)
        if p.get("password_env") and os.environ.get(p["password_env"]):
            text = text.replace(os.environ[p["password_env"]], "<hidden>")
        result = {"exit_code": process.returncode, "log": str(log), "output": text[-16000:]}
        if process.returncode != 0:
            raise BridgeError(f"{label}: код {process.returncode}. {result['output']} Журнал: {log}")
        return result

    def generation(self, connection, job=None):
        result = self.run(connection, ["/GetConfigGenerationID"] + self.extension_args(connection),
                          "generation", job)
        match = re.search(r"(?i)\b[0-9a-f]{40}\b", result["output"])
        if not match:
            raise BridgeError("Не удалось прочитать поколение конфигурации из журнала 1С.")
        return match.group().lower()

    def progress(self, job, phase):
        with self.jobs_lock:
            self.jobs[job]["phase"] = phase
            write_json(self.directory(self.jobs[job]["connection"]) / "jobs" / f"{job}.json", self.jobs[job])

    def submit(self, connection, operation, function):
        self.profile(connection)
        job = uuid.uuid4().hex
        with self.jobs_lock:
            self.jobs[job] = {"job_id": job, "connection": connection, "operation": operation,
                              "status": "queued", "phase": "queued", "started_at": time.time()}
        def worker():
            with self.jobs_lock:
                self.jobs[job]["status"] = "running"
            try:
                with self.locked(connection):
                    result = function(job)
                with self.jobs_lock:
                    self.jobs[job].update(status="done", phase="done", result=result)
            except Exception as error:
                with self.jobs_lock:
                    self.jobs[job].update(status="failed", phase="failed", error=str(error))
            finally:
                with self.jobs_lock:
                    write_json(self.directory(connection) / "jobs" / f"{job}.json", self.jobs[job])
        self.pool.submit(worker)
        return {"job_id": job, "status": "queued", "next": "get_job"}

    def get_job(self, job):
        if not re.fullmatch(r"[0-9a-f]{32}", job):
            raise BridgeError("Недопустимый job_id.")
        with self.jobs_lock:
            if job in self.jobs:
                return dict(self.jobs[job])
        for connection in self.config.get("connections", {}):
            saved = read_json(self.directory(connection) / "jobs" / f"{job}.json")
            if saved:
                return saved
        raise BridgeError("Задача не найдена.")

    def _sync(self, connection, job=None):
        folder, source = self.directory(connection), self.sources(connection)
        before = self.generation(connection, job)
        previous = read_json(folder / "snapshot.json")
        valid = (source / "Configuration.xml").exists() and (source / "ConfigDumpInfo.xml").exists()
        if valid and previous and previous.get("generation") == before:
            return {**previous, "unchanged": True}
        source.mkdir(parents=True, exist_ok=True)
        # Mark an in-place dump incomplete until both export and indexing succeed.
        write_json(folder / "snapshot.json", {"ready": False})
        args = ["/DumpConfigToFiles", str(source), "-Format", "Hierarchical"]
        if valid:
            args += ["-update", "-force"]
        result = self.run(connection, args + self.extension_args(connection), "export", job)
        after = self.generation(connection, job)
        if before != after:
            raise BridgeError("Конфигурация изменилась во время выгрузки. Повторите синхронизацию после сохранения правок.")
        if not (source / "Configuration.xml").exists():
            raise BridgeError("1С не создала Configuration.xml.")
        self.progress(job, "index_metadata") if job else None
        objects, errors, modules = [], [], []
        for directory, dirs, names in os.walk(source, followlinks=False):
            parent = Path(directory)
            relative_parent = parent.relative_to(source)
            for name in names:
                file = parent / name
                relative = file.relative_to(source).as_posix()
                if file.suffix == ".bsl":
                    modules.append(relative)
                elif file.suffix == ".xml" and name != "ConfigDumpInfo.xml" and "Ext" not in relative_parent.parts:
                    try:
                        obj = parse_metadata(file, detailed=False)
                        if obj:
                            objects.append({k: obj[k] for k in ("address", "kind", "name", "uuid")}
                                           | {"path": relative, "synonym": obj["properties"].get("Synonym", "")})
                            if job and len(objects) % 500 == 0:
                                self.progress(job, f"index_metadata: {len(objects)} objects, {len(modules)} modules")
                    except (ET.ParseError, BridgeError) as error:
                        errors.append({"path": relative, "error": str(error)})
        objects.sort(key=lambda o: o["path"])
        modules.sort()
        write_json(folder / "index.json", {"objects": objects, "modules": modules, "errors": errors})
        snapshot = {"ready": True, "generation": after, "synced_at": time.time(),
                    "objects": len(objects), "modules": len(modules), "index_errors": errors[:20],
                    "index_error_count": len(errors), "source_dir": str(source), "export_log": result["log"]}
        write_json(folder / "snapshot.json", snapshot)
        return snapshot

    def sync_sources(self, connection):
        return self.submit(connection, "sync", lambda job: self._sync(connection, job))

    def index(self, connection):
        snapshot = read_json(self.directory(connection) / "snapshot.json", {})
        if not snapshot.get("ready"):
            raise BridgeError("Сначала выполните sync_sources и дождитесь успешного завершения.")
        result = read_json(self.directory(connection) / "index.json", {})
        # Subordinate objects often share names (e.g. ФормаОбъекта).
        # Qualify their addresses through the metadata descriptions of their parents.
        by_path = {o["path"]: o for o in result.get("objects", [])}
        for item in sorted(by_path.values(), key=lambda o: len(PurePosixPath(o["path"]).parts)):
            item["local_address"] = item["address"]
            parts = PurePosixPath(item["path"]).parts
            for length in range(len(parts) - 1, 0, -1):
                parent_path = "/".join(parts[:length]) + ".xml"
                parent = by_path.get(parent_path)
                if parent:
                    item["address"] = parent["address"] + "." + item["local_address"]
                    break
        return result

    def search_metadata(self, connection, text="", kind="", offset=0, limit=50):
        if not 1 <= limit <= 200 or offset < 0:
            raise BridgeError("limit: 1..200; offset >= 0.")
        with self.locked(connection):
            values = [o for o in self.index(connection).get("objects", [])
                      if (not kind or o["kind"].casefold() == kind.casefold())
                      and text.casefold() in (o["address"] + json.dumps(o["synonym"], ensure_ascii=False)).casefold()]
            return {"total": len(values), "objects": values[offset:offset+limit]}

    def get_metadata(self, connection, address):
        with self.locked(connection):
            objects = self.index(connection).get("objects", [])
            found = [o for o in objects if o["address"].casefold() == address.casefold()]
            if len(found) != 1:
                raise BridgeError("Укажите точный address из search_metadata.")
            item = found[0]
            result = parse_metadata(safe_path(self.sources(connection), item["path"]))
            result["address"] = item["address"]
            result["path"] = item["path"]
            prefix = item["path"][:-4] + "/"
            result["modules"] = [p for p in self.index(connection)["modules"] if p.startswith(prefix)]
            if item["kind"] == "Configuration":
                result["modules"] = [p for p in self.index(connection)["modules"] if p.startswith("Ext/")]
            return result

    def list_modules(self, connection, text="", offset=0, limit=100):
        if not 1 <= limit <= 200 or offset < 0:
            raise BridgeError("Недопустимые параметры страницы.")
        with self.locked(connection):
            modules = [p for p in self.index(connection).get("modules", []) if text.casefold() in p.casefold()]
            return {"total": len(modules), "modules": modules[offset:offset+limit]}

    def module(self, connection, path):
        file = safe_path(self.sources(connection), path)
        relative = file.relative_to(self.sources(connection).resolve()).as_posix()
        if relative not in self.index(connection).get("modules", []) or file.suffix != ".bsl":
            raise BridgeError("Выберите существующий BSL-модуль из list_modules.")
        return relative, file

    def read_module(self, connection, path, start=1, limit=200):
        if start < 1 or not 1 <= limit <= 1000:
            raise BridgeError("start >= 1, limit: 1..1000.")
        with self.locked(connection):
            relative, file = self.module(connection, path)
            data = file.read_bytes()
            lines = data.decode("utf-8-sig").splitlines()
            return {"path": relative, "sha256": sha(data), "total_lines": len(lines),
                    "start": start, "text": "\n".join(lines[start-1:start-1+limit]),
                    "has_more": start-1+limit < len(lines)}

    def search_code(self, connection, text, path_filter="", limit=100):
        if not text or not 1 <= limit <= 300:
            raise BridgeError("Нужен непустой текст; limit: 1..300.")
        hits = []
        with self.locked(connection):
            for path in self.index(connection).get("modules", []):
                if path_filter.casefold() not in path.casefold():
                    continue
                file = safe_path(self.sources(connection), path)
                for line, value in enumerate(file.read_text(encoding="utf-8-sig").splitlines(), 1):
                    if text.casefold() in value.casefold():
                        hits.append({"path": path, "line": line, "text": value[:1500]})
                        if len(hits) >= limit:
                            return {"hits": hits, "truncated": True}
        return {"hits": hits, "truncated": False}

    def stage_module(self, connection, path, text, expected_sha256):
        if len(text.encode("utf-8")) > 2_000_000:
            raise BridgeError("Модуль превышает ограничение 2 МБ.")
        with self.locked(connection):
            relative, source = self.module(connection, path)
            original = source.read_bytes()
            if sha(original) != expected_sha256:
                raise BridgeError("Модуль изменился. Прочитайте его заново.")
            folder = self.directory(connection)
            manifest = read_json(folder / "draft.json", {"files": {}})
            generation = read_json(folder / "snapshot.json")["generation"]
            if manifest["files"] and manifest.get("generation") != generation:
                raise BridgeError("Черновик относится к другой версии базы. Просмотрите и очистите его.")
            manifest["generation"] = generation
            newline = "\r\n" if b"\r\n" in original else "\n"
            normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)
            data = normalized.encode("utf-8")
            if original.startswith(b"\xef\xbb\xbf"):
                data = b"\xef\xbb\xbf" + data
            draft = safe_path(folder / "draft", relative)
            baseline = safe_path(folder / "baseline", relative)
            draft.parent.mkdir(parents=True, exist_ok=True)
            baseline.parent.mkdir(parents=True, exist_ok=True)
            baseline.write_bytes(original)
            draft.write_bytes(data)
            if sha(data) == sha(original):
                manifest["files"].pop(relative, None)
            else:
                manifest["files"][relative] = {"base_sha256": sha(original), "draft_sha256": sha(data)}
            write_json(folder / "draft.json", manifest)
            return {"path": relative, "staged": relative in manifest["files"], "sha256": sha(data)}

    def patch_module(self, connection, path, old_text, new_text, expected_sha256):
        if not old_text:
            raise BridgeError("Нужен непустой фрагмент для замены.")
        with self.locked(connection):
            _, source = self.module(connection, path)
            data = source.read_bytes()
            if sha(data) != expected_sha256:
                raise BridgeError("Модуль изменился. Прочитайте его заново.")
            # Patch the synchronized baseline, not an unrelated text page or hidden draft.
            text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
            old = old_text.replace("\r\n", "\n").replace("\r", "\n")
            new = new_text.replace("\r\n", "\n").replace("\r", "\n")
            if text.count(old) != 1:
                raise BridgeError("Исходный фрагмент должен встречаться ровно один раз.")
            revised = text.replace(old, new, 1)
        return self.stage_module(connection, path, revised, expected_sha256)

    def review_changes(self, connection):
        with self.locked(connection):
            folder = self.directory(connection)
            manifest = read_json(folder / "draft.json", {"files": {}})
            changes = []
            for relative, hashes in manifest["files"].items():
                old = safe_path(folder / "baseline", relative).read_bytes()
                new = safe_path(folder / "draft", relative).read_bytes()
                if sha(old) != hashes["base_sha256"] or sha(new) != hashes["draft_sha256"]:
                    raise BridgeError("Файлы черновика изменены вне моста. Подготовьте модуль заново.")
                diff = "".join(difflib.unified_diff(old.decode("utf-8-sig").splitlines(True),
                                                  new.decode("utf-8-sig").splitlines(True),
                                                  fromfile="base/"+relative, tofile="draft/"+relative))
                changes.append({"path": relative, "diff": diff})
            plan = uuid.uuid4().hex
            write_json(folder / "plans" / f"{plan}.json", manifest)
            return {"plan_id": plan, "changes": changes,
                    "allow_apply": bool(self.profile(connection).get("allow_apply", False)),
                    "note": "План содержит только BSL-модули. Обновление конфигурации БД не выполняется."}

    def discard_changes(self, connection):
        with self.locked(connection):
            write_json(self.directory(connection) / "draft.json", {"files": {}})
        return {"discarded": True}

    def _apply(self, connection, plan_id, job=None):
        folder = self.directory(connection)
        plan = read_json(folder / "plans" / f"{plan_id}.json")
        draft = read_json(folder / "draft.json", {"files": {}})
        if not plan or not plan.get("files") or plan != draft:
            raise BridgeError("План устарел или пуст. Вызовите review_changes заново.")
        for relative, hashes in plan["files"].items():
            if sha(safe_path(folder / "draft", relative).read_bytes()) != hashes["draft_sha256"]:
                raise BridgeError("Черновик изменён после просмотра плана.")
        current = self._sync(connection, job)
        if current["generation"] != plan["generation"]:
            raise BridgeError("База изменилась после подготовки правок. Загрузка отменена; черновик сохранён.")
        for relative, hashes in plan["files"].items():
            _, source = self.module(connection, relative)
            if sha(source.read_bytes()) != hashes["base_sha256"]:
                raise BridgeError(f"Конфликт: {relative}. Загрузка отменена.")
        operation = folder / "applied" / plan_id
        operation.mkdir(parents=True, exist_ok=True)
        backup = operation / ("before.cfe" if self.profile(connection).get("extension") else "before.cf")
        self.run(connection, ["/DumpCfg", str(backup)] + self.extension_args(connection), "backup", job)
        if not backup.exists() or backup.stat().st_size == 0:
            raise BridgeError("Не создана резервная копия конфигурации. Загрузка отменена.")
        overlay = operation / "files"
        for relative in plan["files"]:
            dest = safe_path(overlay, relative)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(safe_path(folder / "draft", relative), dest)
        file_list = operation / "load-list.txt"
        file_list.write_text("\r\n".join(p.replace("/", "\\") for p in plan["files"]) + "\r\n", encoding="utf-8")
        if self.generation(connection, job) != current["generation"]:
            raise BridgeError("База изменилась перед загрузкой. Черновик сохранён.")
        args = ["/LoadConfigFromFiles", str(overlay), "-Format", "Hierarchical", "-partial",
                "-listFile", str(file_list)] + self.extension_args(connection)
        # From this point a timeout/error may leave an unknown database state.
        write_json(folder / "snapshot.json", {"ready": False, "apply_started": plan_id, "backup": str(backup)})
        loaded = self.run(connection, args, "load_modules", job)
        write_json(operation / "result.json", {"loaded": True, "backup": str(backup), "load": loaded})
        # Clear the draft only after a successful import; retain plan and originals for recovery.
        write_json(folder / "draft.json", {"files": {}})
        check = None
        try:
            check = self._check(connection, job)
        except BridgeError as error:
            check = {"passed": False, "error": str(error)}
        try:
            synced = self._sync(connection, job)
        except BridgeError as error:
            synced = {"ready": False, "error": str(error)}
        result = {"loaded": True, "database_updated": False, "files": list(plan["files"]),
                  "backup": str(backup), "check": check, "snapshot": synced,
                  "note": "Модули загружены в основную конфигурацию. Проверка не откатывает загрузку."}
        write_json(operation / "result.json", result)
        return result

    def apply_changes(self, connection, plan_id):
        if not self.profile(connection).get("allow_apply", False):
            raise BridgeError("Загрузка выключена для этого подключения: allow_apply=false.")
        if not re.fullmatch(r"[0-9a-f]{32}", plan_id):
            raise BridgeError("Нужен plan_id из review_changes.")
        return self.submit(connection, "apply", lambda job: self._apply(connection, plan_id, job))

    def _check(self, connection, job=None):
        result = self.run(connection, ["/CheckConfig", "-ConfigLogIntegrity", "-IncorrectReferences",
                                     "-ThinClient", "-Server"] + self.extension_args(connection), "check", job)
        # Some platform checks report diagnostics with a zero process exit code.
        # Expose the log instead of equating exit code with absence of diagnostics.
        return {"completed": True, **result, "note": "Проверьте сообщения журнала; код 0 означает завершение команды."}

    def check_configuration(self, connection):
        return self.submit(connection, "check", lambda job: self._check(connection, job))

    def close(self):
        self.pool.shutdown(wait=True)
