#!/usr/bin/env python3
"""Выгрузка публичных программ ris61edu.ru. Python 3.10+, без зависимостей."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, HTTPCookieProcessor
import uuid


VERSION = "1.0.1"
SCHEMA_VERSION = 1
BASE_URL = "https://new-portal.ris61edu.ru"
LOG = logging.getLogger("navigator")
WEEKDAYS = {1: "понедельник", 2: "вторник", 3: "среда", 4: "четверг",
            5: "пятница", 6: "суббота", 7: "воскресенье"}


class ExportError(Exception):
    """Ожидаемая ошибка: рабочая база не должна быть заменена."""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def numeric_id(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ExportError(f"Ожидался числовой ID, получено {value!r}")
    return str(int(value))


def read_municipalities(path):
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251")
    result = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(.+),\s*([0-9]+)\s*", line)
        if not match:
            raise ExportError(f"{path}, строка {number}: нужен формат 'Название, ID'")
        mid = numeric_id(match[2])
        if mid in result:
            raise ExportError(f"Повтор муниципалитета {mid} в строке {number}")
        result[mid] = {"id": mid, "name": match[1].strip()}
    if not result:
        raise ExportError("Файл муниципалитетов пуст")
    return result


def atomic_json(path, value):
    """Временный файл на том же диске + fsync + атомарное переименование."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp",
                                         delete=False) as stream:
            name = stream.name
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


class FileLock:
    """Межпроцессная блокировка: автоматически снимается ОС после сбоя."""
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise ExportError(f"Уже запущена выгрузка с блокировкой {self.path}") from exc
        return self

    def __exit__(self, *args):
        # Закрытие дескриптора освобождает блокировку, в том числе в Windows.
        self.file.close()


class State:
    """Контрольная точка одного незавершённого обхода, а не вечный HTTP-кэш."""
    def __init__(self, path, config, restart=False):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS responses "
                        "(url TEXT PRIMARY KEY, body TEXT NOT NULL, fetched_at TEXT NOT NULL)")
        row = self.db.execute("SELECT value FROM meta WHERE key='run'").fetchone()
        previous = json.loads(row[0]) if row else None
        if previous and previous["status"] == "running" and not restart:
            previous_config = previous["config"]
            compatible_patch = (
                previous_config.get("version") == "1.0.0" and config.get("version") == "1.0.1"
                and {k: v for k, v in previous_config.items() if k != "version"}
                == {k: v for k, v in config.items() if k != "version"}
            )
            if previous_config != config and not compatible_patch:
                self.db.close()
                raise ExportError("Незавершённый обход имеет другие параметры. "
                                  "Верните их, задайте другой --state или используйте --restart.")
            self.run = previous
            if compatible_patch:
                self.run["config"] = config
                self.save()
                LOG.info("Контрольная точка 1.0.0 совместима с 1.0.1; загруженные ответы сохранены")
            LOG.info("Продолжаю обход %s, начатый %s", previous["id"], previous["started_at"])
        else:
            self.db.execute("DELETE FROM responses")
            self.run = {"id": str(uuid.uuid4()), "started_at": utc_now(),
                        "status": "running", "config": config}
            self.save()
        self.db.commit()

    def save(self):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('run', ?)",
                            (json.dumps(self.run, ensure_ascii=False),))

    def get(self, url):
        row = self.db.execute("SELECT body, fetched_at FROM responses WHERE url=?", (url,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    def put(self, url, body, fetched_at):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO responses VALUES (?, ?, ?)",
                            (url, json.dumps(body, ensure_ascii=False, allow_nan=False), fetched_at))

    def complete(self):
        self.run["status"] = "complete"
        self.run["finished_at"] = utc_now()
        self.save()

    def close(self):
        self.db.close()


def retry_after_seconds(value):
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, dt.timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0.0


class Client:
    def __init__(self, base_url, state, delay=1.5, jitter=0.5, timeout=45, retries=4):
        self.base_url = base_url.rstrip("/")
        self.state = state
        self.delay, self.jitter, self.timeout, self.retries = delay, jitter, timeout, retries
        self.opener = build_opener(HTTPCookieProcessor())
        self.next_request = 0.0
        self.requests = 0
        self.cache_hits = 0
        self.municipality_mismatches = []

    def url(self, path, **params):
        return self.base_url + path + ("?" + urlencode(params) if params else "")

    def fetch(self, url, parser):
        cached = self.state.get(url)
        if cached is not None:
            self.cache_hits += 1
            return cached
        for attempt in range(self.retries + 1):
            time.sleep(max(0, self.next_request - time.monotonic()))
            wait = None
            try:
                self.requests += 1
                request = Request(url, headers={
                    "User-Agent": f"Ris61PublicCatalogExporter/{VERSION}",
                    "Accept": "application/json, text/html;q=0.9",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                })
                with self.opener.open(request, timeout=self.timeout) as response:
                    text = response.read().decode("utf-8-sig")
                body = parser(text)
                fetched_at = utc_now()
                self.state.put(url, body, fetched_at)
                return body, fetched_at
            except HTTPError as exc:
                if exc.code not in {408, 425, 429, 500, 502, 503, 504}:
                    raise ExportError(f"HTTP {exc.code}: {url}. Обход остановлен.") from exc
                wait = max(retry_after_seconds(exc.headers.get("Retry-After")),
                           (10 if exc.code == 429 else 3) * 2 ** attempt)
                reason = f"HTTP {exc.code}"
            except (URLError, TimeoutError, OSError) as exc:
                wait = 3 * 2 ** attempt
                reason = str(exc)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                wait = 3 * 2 ** attempt
                reason = f"Некорректный ответ: {exc}"
            finally:
                # Пауза после завершения каждого сетевого запроса, включая повторы.
                self.next_request = time.monotonic() + self.delay + random.uniform(0, self.jitter)
            if attempt == self.retries:
                raise ExportError(f"Запрос не выполнен после {attempt + 1} попыток: {url}; {reason}")
            LOG.warning("%s. Повтор через %.1f с: %s", reason, wait, url)
            self.next_request = max(self.next_request, time.monotonic() + wait)
        raise AssertionError("unreachable")

    def api(self, path, **params):
        def parse(text):
            body = json.loads(text)
            if not isinstance(body, dict) or body.get("success") not in (True, 1):
                raise ExportError(f"API не подтвердил успех: {text[:300]}")
            if body.get("err_code") not in (None, 0, "0"):
                raise ExportError(f"API вернул err_code={body['err_code']}")
            if not isinstance(body.get("data"), list):
                raise ExportError("Формат API изменился: data должен быть массивом")
            return body
        return self.fetch(self.url(path, **params), parse)

    def detail(self, catalog):
        # Полная карточка нового портала содержит серверный Nuxt JSON.
        eid = numeric_id(catalog["id"])
        slug = quote(str(catalog.get("url_code") or ""), safe="")
        url = self.base_url + f"/program/{eid}" + ("-" + slug if slug else "")
        data, fetched_at = self.fetch(url, lambda html: parse_detail(html, eid))
        return data, fetched_at, url


def total_from(body):
    # total в organizationlist — число во всей области; filtered — по фильтру.
    for key in ("filtered", "total"):
        if key in body and body[key] is not None:
            try:
                value = int(body[key])
                if value < 0:
                    raise ValueError
                return value
            except (TypeError, ValueError) as exc:
                raise ExportError(f"Некорректное поле {key}: {body[key]!r}") from exc
    return None


def collect_pages(client, path, params, limit, sample=None):
    """Не заканчиваем по len(data) < запрошенного limit: сервер может его урезать."""
    records, seen = [], set()
    expected = None
    for page in range(1, 100001):
        body, fetched_at = client.api(path, **params, page=page, limit=limit)
        if "page" in body and int(body["page"]) != page:
            raise ExportError(f"API игнорирует номер страницы {page}: {path}")
        total = total_from(body)
        if total is not None:
            if expected is None:
                expected = total
            elif total != expected:
                raise ExportError(f"Число записей меняется во время обхода {path}: "
                                  f"{expected} → {total}. Повторите с --restart.")
        items = body["data"]
        if not items:
            if expected is not None and len(records) != expected:
                raise ExportError(f"Неполный список {path}: {len(records)} из {expected}")
            return records
        for item in items:
            if not isinstance(item, dict) or "id" not in item:
                raise ExportError(f"Запись без id в {path}")
            key = numeric_id(item["id"])
            if key in seen:
                raise ExportError(f"Повтор ID {key} при пагинации {path}; "
                                  "возможен сдвиг каталога. Повторите с --restart.")
            seen.add(key)
            records.append({"data": item, "fetched_at": fetched_at})
            if sample is not None and len(records) >= sample:
                return records
        if expected is not None:
            if len(records) > expected:
                raise ExportError(f"API вернул больше записей, чем filtered: {path}")
            if len(records) == expected:
                return records
    raise ExportError(f"Слишком много страниц: {path}")


class PayloadParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.payloads = []
        self.buffer = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("type", "").lower() == "application/json":
            self.buffer = []

    def handle_data(self, data):
        if self.buffer is not None:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.buffer is not None:
            self.payloads.append("".join(self.buffer))
            self.buffer = None


def decode_nuxt(values):
    """Декодер ссылочного JSON Nuxt/devalue; никакого eval/исполнения JS."""
    if not isinstance(values, list) or not values:
        raise ExportError("Неизвестный формат Nuxt payload")
    memo = {}
    active = set()

    def deref(index):
        if not isinstance(index, int) or isinstance(index, bool):
            raise ExportError("Неверная ссылка в Nuxt JSON")
        if index < 0:
            # Спецзначения devalue представлены явно, чтобы не потерять их в JSON.
            specials = {-1: {"$nuxt": "undefined"}, -2: {"$nuxt": "hole"},
                        -3: {"$nuxt": "NaN"}, -4: {"$nuxt": "Infinity"},
                        -5: {"$nuxt": "-Infinity"}, -6: -0.0}
            if index not in specials:
                raise ExportError(f"Неизвестное спецзначение Nuxt: {index}")
            return specials[index]
        if index >= len(values):
            raise ExportError("Ссылка Nuxt за пределами массива")
        if index in active:
            raise ExportError("Циклическая структура Nuxt: формат требует адаптации")
        if index in memo:
            return memo[index]
        active.add(index)
        value = values[index]
        if isinstance(value, dict):
            result = {key: deref(ref) for key, ref in value.items()}
        elif isinstance(value, list):
            if value and isinstance(value[0], str):
                tag = value[0]
                if tag in {"Reactive", "ShallowReactive", "Ref", "ShallowRef", "EmptyRef", "EmptyShallowRef"}:
                    if len(value) != 2:
                        raise ExportError(f"Некорректная оболочка Nuxt: {tag}")
                    result = deref(value[1])
                    if tag in {"EmptyRef", "EmptyShallowRef"} and isinstance(result, str):
                        try:
                            result = json.loads(result)
                        except json.JSONDecodeError:
                            pass
                elif tag in {"Date", "BigInt", "RegExp"}:
                    result = {"$nuxt_type": tag, "value": value[1:]}
                elif tag in {"Set", "Map", "null"}:
                    result = {"$nuxt_type": tag, "value": [deref(x) for x in value[1:]]}
                else:
                    raise ExportError(f"Неподдерживаемый тип Nuxt: {tag}")
            else:
                result = [deref(ref) for ref in value]
        else:
            result = value
        active.remove(index)
        memo[index] = result
        return result

    return deref(0)


def parse_detail(html, event_id):
    parser = PayloadParser()
    parser.feed(html)
    for text in parser.payloads:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        # Выбираем именно Nuxt payload, а не произвольный JSON сторонних виджетов.
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
            continue
        if payload[0][0] not in {"Reactive", "ShallowReactive"}:
            continue
        root = decode_nuxt(payload)
        data = root.get("data", {}) if isinstance(root, dict) else {}
        if not isinstance(data, dict):
            continue
        for event in data.values():
            if (isinstance(event, dict) and str(event.get("id")) == str(event_id)
                    and event.get("partner_id") is not None):
                numeric_id(event["partner_id"])
                return event
    raise ExportError(f"В карточке {event_id} не найден полный Nuxt JSON с partner_id. "
                      "Возможны изменение сайта или недоступность программы; база не заменена.")


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "li", "div", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "li", "div", "h1", "h2", "h3"}:
            self.parts.append("\n")


def plain_text(value):
    if not isinstance(value, str):
        return value
    parser = TextParser()
    parser.feed(value)
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def minutes(value):
    match = re.fullmatch(r"([0-9]{1,2}):([0-9]{2})(?::[0-9]{2})?", str(value))
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        return None
    return int(match[1]) * 60 + int(match[2])


def normalize_group(data, program_url, fetched_at):
    schedules = []
    for schedule in data.get("schedule") or []:
        if not isinstance(schedule, dict):
            raise ExportError(f"Некорректное расписание группы {data.get('id')}")
        days = schedule.get("week_days") or []
        if not isinstance(days, list):
            raise ExportError(f"week_days должен быть массивом, группа {data.get('id')}")
        day_numbers = [int(x) for x in days if str(x).isdigit() and int(x) in WEEKDAYS]
        schedules.append({
            "id": str(schedule.get("id", "")),
            "schedule_type": schedule.get("schedule_type"),
            "week_days": day_numbers,
            "week_day_names": [WEEKDAYS[x] for x in day_numbers],
            "unrecognized_week_days": [x for x in days if str(x) not in {str(n) for n in WEEKDAYS}],
            "time_start": schedule.get("time_start"), "time_end": schedule.get("time_end"),
            "start_minutes": minutes(schedule.get("time_start")),
            "end_minutes": minutes(schedule.get("time_end")),
            "period_from": schedule.get("period_from"), "period_to": schedule.get("period_to"),
            "academic_year": schedule.get("academic_year"),
            "is_deleted": schedule.get("is_deleted"),
        })
    return {"id": numeric_id(data["id"]), "teacher": data.get("teacher"),
            "name": data.get("name"), "enrollment_url": program_url,
            "schedule_normalized": schedules, "data": data, "fetched_at": fetched_at}


def record_municipality_mismatch(client, data, requested_id, entity_type):
    """Фильтр выдачи и поле одной записи могут расходиться: данные не теряем."""
    reported = data.get("municipality_id")
    if reported is None:
        return
    reported_id = numeric_id(reported)
    if reported_id == requested_id:
        return
    entity_id = numeric_id(data["id"])
    client.municipality_mismatches.append({
        "entity_type": entity_type, "entity_id": entity_id,
        "requested_municipality_id": requested_id,
        "reported_municipality_id": reported_id,
    })
    LOG.warning("%s %s: в выдаче municipality=%s поле municipality_id=%s. "
                "Сохраняю запись и исходное значение; расхождение отмечено в _meta.municipality_mismatches",
                entity_type, entity_id, requested_id, reported_id)


def export_catalog(client, municipalities, sample=None):
    organizations, programs = {}, {}
    for mid, municipality in sorted(municipalities.items(), key=lambda x: int(x[0])):
        LOG.info("Муниципалитет %s — %s", mid, municipality["name"])
        orgs = collect_pages(client, "/api/organizationlist/get", {"municipality": mid}, 4000)
        for row in orgs:
            data = row["data"]
            oid = numeric_id(data["id"])
            record_municipality_mismatch(client, data, mid, "organization")
            if oid not in organizations:
                organizations[oid] = {"id": oid, "data": data, "source": "organizationlist",
                                      "municipality_ids": [], "fetched_at": row["fetched_at"],
                                      "programs": []}
            else:
                organizations[oid].update(data=data, source="organizationlist",
                                          fetched_at=row["fetched_at"])
            organizations[oid]["municipality_ids"].append(mid)
        events = collect_pages(client, "/api/events/get", {"municipality": mid}, 100, sample)
        LOG.info("Организаций: %s; программ: %s", len(orgs), len(events))
        for number, row in enumerate(events, 1):
            catalog = row["data"]
            eid = numeric_id(catalog["id"])
            record_municipality_mismatch(client, catalog, mid, "program")
            if eid in programs:
                programs[eid]["municipality_ids"].append(mid)
                continue
            details, fetched_at, url = client.detail(catalog)
            oid = numeric_id(details["partner_id"])
            if oid not in organizations:
                # Организация может находиться за пределами выбранной территории.
                organizations[oid] = {
                    "id": oid, "municipality_ids": [], "source": "program_details",
                    "data": {"id": oid, "name": details.get("partner_short_name"),
                             "contacts": {"email": details.get("partner_email"),
                                          "phone": details.get("partner_phone")},
                             "site": details.get("partner_site")},
                    "fetched_at": fetched_at, "programs": []}
            group_rows = collect_pages(client, "/api/event/groups/get", {"event_id": eid}, 100)
            groups = []
            for group in group_rows:
                if numeric_id(group["data"].get("event_id")) != eid:
                    raise ExportError(f"Неверный event_id в группе программы {eid}")
                groups.append(normalize_group(group["data"], url, group["fetched_at"]))
            # Несовпадение двух публичных представлений — повод повторить обход.
            if details.get("groupsFiltered") is not None and int(details["groupsFiltered"]) != len(groups):
                raise ExportError(f"Число групп программы {eid} различается в карточке и API: "
                                  f"{details['groupsFiltered']} и {len(groups)}. Повторите с --restart.")
            program = {
                "id": eid, "organization_id": oid, "municipality_ids": [mid], "url": url,
                "catalog": catalog, "details": details,
                "text": {key: plain_text(details[key]) for key in (
                    "description", "education_plan", "program_target", "expected_results",
                    "teachers", "teachers_description", "special_conditions", "material_base",
                    "price_description", "schedule_description") if key in details},
                "groups": sorted(groups, key=lambda g: int(g["id"])),
                "fetched_at": {"catalog": row["fetched_at"], "details": fetched_at},
            }
            programs[eid] = program
            organizations[oid]["programs"].append(program)
            LOG.info("[%s %d/%d] %s: %s; групп %d", mid, number, len(events), eid,
                     catalog.get("name", ""), len(groups))
    for org in organizations.values():
        org["programs"].sort(key=lambda p: int(p["id"]))
        org["municipality_ids"] = sorted(set(org["municipality_ids"]), key=int)
        for program in org["programs"]:
            program["municipality_ids"] = sorted(set(program["municipality_ids"]), key=int)
    return sorted(organizations.values(), key=lambda o: int(o["id"]))


def entities(snapshot):
    result = {"organizations": {}, "programs": {}, "groups": {}}
    if not snapshot:
        return result
    for org in snapshot.get("organizations", []):
        result["organizations"][org["id"]] = digest({"data": org["data"], "source": org["source"],
                                                     "municipality_ids": org["municipality_ids"]})
        for program in org["programs"]:
            detail = {k: v for k, v in program["details"].items() if k not in {"groups", "groupsFiltered"}}
            result["programs"][program["id"]] = digest({
                "catalog": program["catalog"], "details": detail, "url": program["url"],
                "organization_id": program["organization_id"], "municipality_ids": program["municipality_ids"]})
            for group in program["groups"]:
                result["groups"][group["id"]] = digest(group["data"])
    return result


def changes_between(old, new):
    before, after = entities(old), entities(new)
    report = {}
    for kind in before:
        a, b = before[kind], after[kind]
        report[kind] = {
            "added": sorted(b.keys() - a.keys(), key=int),
            "changed": sorted((k for k in a.keys() & b.keys() if a[k] != b[k]), key=int),
            "not_seen": sorted(a.keys() - b.keys(), key=int),
        }
    return report


def load_old(path, scope):
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as stream:
            old = json.load(stream)
    except (ValueError, OSError) as exc:
        raise ExportError(f"Не удалось прочитать прежнюю базу {path}; она не будет затронута") from exc
    if old.get("_meta", {}).get("schema_version") != SCHEMA_VERSION:
        raise ExportError("Другая версия схемы существующего JSON. Задайте новый --output.")
    if old["_meta"].get("scope") != scope:
        raise ExportError("Область выгрузки отличается от существующего JSON. "
                          "Для выборки/другого набора территорий задайте другой --output.")
    return old


def publish(path, snapshot, old):
    # Сначала убеждаемся, что новый JSON полностью сериализуется на диске.
    pending = path.with_name(path.name + ".pending")
    atomic_json(pending, snapshot)
    try:
        if old is not None:
            backup = path.with_name(path.name + ".bak")
            temp_backup = backup.with_name(backup.name + ".tmp")
            try:
                with path.open("rb") as source, temp_backup.open("wb") as stream:
                    shutil.copyfileobj(source, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_backup, backup)
            finally:
                if temp_backup.exists():
                    temp_backup.unlink()
        os.replace(pending, path)
    finally:
        if pending.exists():
            pending.unlink()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--municipalities", type=Path, default=Path(__file__).with_name("municipality.txt"),
                        help="Файл территорий: Название, ID")
    parser.add_argument("--output", type=Path, default=Path("navigator.json"), help="Итоговый JSON")
    parser.add_argument("--state", type=Path, help="SQLite-контрольная точка; по умолчанию <output>.state.sqlite3")
    parser.add_argument("--municipality", help="Только указанные ID через запятую, например 1,2")
    parser.add_argument("--sample", type=int, help="Первые N программ одной территории; неполная пробная выгрузка")
    parser.add_argument("--restart", action="store_true", help="Начать заново незавершённый обход")
    parser.add_argument("--delay", type=float, default=1.5, help="Минимальная пауза ПОСЛЕ запроса, секунд")
    parser.add_argument("--jitter", type=float, default=0.5, help="Дополнительная случайная пауза 0..N секунд")
    parser.add_argument("--timeout", type=float, default=45, help="Тайм-аут сетевой операции, секунд")
    parser.add_argument("--retries", type=int, default=4, help="Повторы временных сетевых ошибок")
    return parser


def run(args):
    if (not all(math.isfinite(x) for x in (args.delay, args.jitter, args.timeout))
            or args.delay < 1 or args.jitter < 0 or args.timeout <= 0 or args.retries < 0):
        raise ExportError("Нужны --delay >= 1, --jitter >= 0, --timeout > 0 и --retries >= 0")
    municipalities = read_municipalities(args.municipalities)
    if args.municipality:
        chosen = {numeric_id(x.strip()) for x in args.municipality.split(",")}
        unknown = chosen - municipalities.keys()
        if unknown:
            raise ExportError("В файле нет муниципалитетов: " + ", ".join(sorted(unknown, key=int)))
        municipalities = {key: value for key, value in municipalities.items() if key in chosen}
    output = args.output.resolve()
    if args.sample is not None:
        if args.sample <= 0 or len(municipalities) != 1:
            raise ExportError("--sample N требует N > 0 и ровно одну --municipality")
        if output.name == "navigator.json":
            raise ExportError("Для --sample задайте отдельный --output, например sample.json")
    scope = {"source": BASE_URL, "municipality_ids": sorted(municipalities, key=int), "sample": args.sample}
    output.parent.mkdir(parents=True, exist_ok=True)
    state_path = (args.state or output.with_name(output.name + ".state.sqlite3")).resolve()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if state_path == output:
        raise ExportError("--state и --output должны быть разными файлами")
    # Две блокировки защищают и выходной файл, и контрольную точку.
    with FileLock(str(output) + ".lock"), FileLock(str(state_path) + ".lock"):
        old = load_old(output, scope)
        config = {"scope": scope, "output": str(output), "version": VERSION,
                  "municipalities": list(municipalities.values())}
        state = State(state_path, config, args.restart)
        try:
            client = Client(BASE_URL, state, args.delay, args.jitter, args.timeout, args.retries)
            organizations = export_catalog(client, municipalities, args.sample)
            counts = {"municipalities": len(municipalities), "organizations": len(organizations),
                      "programs": sum(len(o["programs"]) for o in organizations),
                      "groups": sum(len(p["groups"]) for o in organizations for p in o["programs"])}
            if counts["programs"] == 0:
                raise ExportError("Не найдено ни одной программы: пустая база не публикуется")
            snapshot = {
                "_meta": {"schema_version": SCHEMA_VERSION, "exporter_version": VERSION,
                          "run_id": state.run["id"], "started_at": state.run["started_at"],
                          "finished_at": utc_now(), "source": BASE_URL, "scope": scope,
                          "complete": args.sample is None, "counts": counts,
                          "schedule_timezone": "Europe/Moscow", "week_day_numbering": "1=понедельник … 7=воскресенье",
                          "http_requests_this_process": client.requests, "cache_hits_this_process": client.cache_hits,
                          "municipality_mismatches": client.municipality_mismatches,
                          "organizations_from_details_only": [o["id"] for o in organizations if o["source"] != "organizationlist"],
                          "notes": ["Снимок публичного каталога, не транзакционный снимок сервера.",
                                    "complete означает завершённый обход выбранных территорий.",
                                    "not_seen означает отсутствие в новом публичном снимке, а не доказанное удаление.",
                                    "municipality_ids у организаций и программ — территории выдачи; исходные municipality_id сохраняются в data/catalog/details.",
                                    "Картинки и документы сохранены как ссылки, их содержимое не скачивается."]},
                "municipalities": sorted(municipalities.values(), key=lambda m: int(m["id"])),
                "organizations": organizations,
            }
            snapshot["changes"] = {"compared_to": old["_meta"]["finished_at"] if old else None,
                                   **changes_between(old, snapshot)}
            # Если публикация прошла, а процесс оборвался перед complete(), следующий
            # запуск распознаёт этот же run_id и не портит .bak повторной публикацией.
            if old and old["_meta"].get("run_id") == state.run["id"]:
                state.complete()
                LOG.info("Снимок этого обхода уже опубликован: %s", output)
                return
            publish(output, snapshot, old)
            state.complete()
            LOG.info("ГОТОВО: %s; %s", output, counts)
        finally:
            state.close()


def main():
    args = build_parser().parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    logfile = args.output.with_name(args.output.name + ".log")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(logfile, encoding="utf-8")])
    try:
        run(args)
    except KeyboardInterrupt:
        LOG.warning("Остановлено пользователем. Повтор той же команды продолжит обход.")
        return 130
    except (ExportError, OSError, ValueError, sqlite3.Error) as exc:
        LOG.error("%s", exc)
        LOG.error("Прежний JSON сохранён. Исправьте причину и повторите команду. "
                  "Если каталог изменился во время обхода, используйте --restart.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
