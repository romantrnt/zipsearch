"""Create five deterministic, entirely fictional ZIP archives for ZipSearch demos.

The output is deliberately kept beside, not inside, the repository. Run from
the repository root:
    python benchmarks/generate_realistic_dataset.py
"""

from __future__ import annotations

import csv
import io
import json
import random
import shutil
import sqlite3
import zipfile
from datetime import date, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

SEED = 20260312
# The intentionally disposable fixture collection is a sibling of this standalone
# repository.  Keeping it out of the repository prevents generated archives from
# becoming release artifacts while retaining a stable, discoverable local location.
ROOT = Path(__file__).resolve().parents[2] / "testdata" / "realistic"
NEEDLE = {
    "name": "Аркадий Плющеватый-Задорнов",
    "email": "arcady.plush@synthetic.invalid",
    "phone": "+7 (999) 555-01-73",
    "contract": "KPD-2024-NEEDLE-771",
    "vehicle": "ХУЕВ777",
}

FIRST = ["Аглая", "Борис", "Венера", "Глеб", "Дарья", "Елисей", "Жанна", "Зоя", "Игорь", "Кира"]
LAST = ["Бубликов", "Карандашников", "Понедельников", "Скрепкин", "Тараканов", "Фонарёв"]
CITIES = ["Москва", "Тверь", "Казань", "Омск", "Санкт-Петербург", "Нижний Новгород"]
DEPARTMENTS = ["Отдел перекладывания бумаг", "Сектор случайных решений", "Архив несрочных дел"]


def synthetic_record(number: int, rng: random.Random) -> dict[str, str]:
    first = rng.choice(FIRST)
    last = rng.choice(LAST)
    birth = date(1965, 1, 1) + timedelta(days=rng.randrange(18_000))
    return {
        "full_name": f"{last} {first} {rng.choice(['А.', 'Б.', 'В.', ''])}".strip(),
        "birth_date": birth.isoformat(),
        "phone": (
            f"+7 (9{rng.randrange(10)}{rng.randrange(10)}) {rng.randrange(100, 1000):03}"
            f"-{rng.randrange(10, 100):02}-{rng.randrange(10, 100):02}"
        ),
        "email": f"{last.lower()}.{number}@example.invalid",
        "city": rng.choice(CITIES),
        "address": (
            f"ул. {rng.choice(['Кривая', 'Непонятная', 'Тихая'])}, "
            f"д. {rng.randrange(1, 180)}, кв. {rng.randrange(1, 500)}"
        ),
        "department": rng.choice(DEPARTMENTS),
        "employee_id": f"EMP-{number:06}",
        "record_id": f"REC-{2020 + number % 5}-{number:07}",
        "registered_at": f"2024-{number % 12 + 1:02}-{number % 27 + 1:02}",
        "status": rng.choice(["active", "ACTIVE", "архив", "  pending ", ""]),
        "notes": rng.choice(
            ["", "проверить адрес", "дубликат в старом выгрузе", "перенесено вручную"]
        ),
        "vehicle": f"А{rng.randrange(100, 1000):03}АА{rng.randrange(10, 200):03}",
        "contract": f"CTR-{2024 + number % 2}-{number:07}",
    }


def needle_record(variant: str) -> dict[str, str]:
    result = {
        "full_name": NEEDLE["name"] if variant == "canonical" else "  аркадий плющеватый-задорнов ",
        "birth_date": "1987-03-14",
        "phone": NEEDLE["phone"],
        "email": NEEDLE["email"],
        "city": "Нижний Новгород",
        "address": "ул. Несуществующая, д. 73, кв. 17",
        "department": "Сектор случайных решений",
        "employee_id": "EMP-NEEDLE-0173",
        "record_id": "REC-NEEDLE-0000173",
        "registered_at": "2024-11-19",
        "status": "active" if variant == "canonical" else "ACTIVE ",
        "notes": "Синтетическая контрольная запись; не реальный человек.",
        "vehicle": NEEDLE["vehicle"],
        "contract": NEEDLE["contract"],
    }
    return result


def csv_bytes(
    records: list[dict[str, str]], delimiter: str = ",", encoding: str = "utf-8"
) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=list(records[0]), delimiter=delimiter, quoting=csv.QUOTE_MINIMAL
    )
    writer.writeheader()
    writer.writerows(records)
    return output.getvalue().encode(encoding)


def jsonl_bytes(records: list[dict[str, str]]) -> bytes:
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode()


def sqlite_bytes(records: list[dict[str, str]]) -> bytes:
    # sqlite3 needs a path, but the temporary source is removed before returning.
    temp = ROOT.parent / "_temporary_source.sqlite"
    connection = sqlite3.connect(temp)
    fields = list(records[0])
    columns = ", ".join(f'"{field}" TEXT' for field in fields)
    connection.execute(f"CREATE TABLE registry ({columns})")
    placeholders = ", ".join("?" for _ in fields)
    connection.executemany(
        f"INSERT INTO registry VALUES ({placeholders})",
        [[record[field] for field in fields] for record in records],
    )
    connection.commit()
    connection.close()
    data = temp.read_bytes()
    temp.unlink()
    return data


def xlsx_bytes(records: list[dict[str, str]]) -> bytes:
    """Minimal XLSX with inline strings; enough to test the built-in dependency-free reader."""
    fields = ["full_name", "email", "phone", "contract", "city", "status"]
    rows = [fields] + [[record[field] for field in fields] for record in records]
    sheet_rows = []
    for row_number, values in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{chr(65 + column)}{row_number}" t="inlineStr">'
            f"<is><t>{escape(value)}</t></is></c>"
            for column, value in enumerate(values)
        )
        sheet_rows.append(f'<row r="{row_number}">{cells}</row>')
    with io.BytesIO() as output:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as book:
            book.writestr(
                "[Content_Types].xml",
                "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>",
            )
            book.writestr(
                "xl/workbook.xml",
                "<workbook xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'/>",
            )
            book.writestr(
                "xl/worksheets/sheet1.xml",
                "<worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'><sheetData>"
                + "".join(sheet_rows)
                + "</sheetData></worksheet>",
            )
        return output.getvalue()


def write_archive(name: str, entries: dict[str, bytes | str]) -> None:
    with zipfile.ZipFile(ROOT / name, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in entries.items():
            archive.writestr(path, data)


def generate() -> dict[str, int]:
    rng = random.Random(SEED)
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True)
    groups = [
        [synthetic_record(group * 2_000 + row, rng) for row in range(1_200)] for group in range(5)
    ]
    n1, n2, n3, n4, n5 = [needle_record("canonical" if i % 2 == 0 else "variant") for i in range(5)]
    groups[0][77] = n1
    groups[1][333] = n2
    groups[2][702] = n3
    groups[3][111] = n4
    groups[4][998] = n5
    # Controlled smart-search records: token order, punctuation, and names with
    # extra components all represent the same record-aware investigation shape.
    for group, index, full_name in (
        (0, 10, "Скрепкин Глеб А."),
        (1, 20, "Глеб Скрепкин"),
        (2, 30, "Скрепкин Глеб Иванович"),
        (3, 40, "Глеб Скрепкин"),
        (4, 50, "Скрепкин Глеб"),
    ):
        groups[group][index]["full_name"] = full_name

    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "вложения/контрольная.jsonl", jsonl_bytes([needle_record("canonical")] + groups[0][:30])
        )
        archive.writestr(
            "вложения/прочти.txt", "Вложение создано только для стресс-теста ZipSearch.\n"
        )

    write_archive(
        "ministerstvo_huevyh_del_2024.zip",
        {
            "README.txt": "Министерство Хуёвых Дел — полностью вымышленный тестовый экспорт.\n",
            "экспорт/кадры_2024.csv": csv_bytes(groups[0] + [groups[0][77]], encoding="cp1251"),
            "логи/ingest.log": "WARN 2024-11-19 duplicate employee EMP-NEEDLE-0173\n"
            + "".join(
                f"2024-11-19T08:{number % 60:02}:00 event={number:05} accepted\n"
                for number in range(5_000)
            ),
            "архивы/вложения_ноябрь.zip": nested.getvalue(),
            "мусор/old.bin": b"\x00\x01not searchable\x00",
        },
    )
    write_archive(
        "federalnaya_sluzhba_poteri_dokumentov.zip",
        {
            "exports/claims.jsonl": jsonl_bytes(groups[1]),
            "exports/legacy_contacts.tsv": csv_bytes(groups[1][:600], delimiter="\t"),
            "config/production.ini": "[export]\nowner = fictional\nretry = 3\n",
            "notes/сводка.txt": "Записи иногда содержат лишние пробелы и повторяются.\n",
            "tiny/" + "x" * 30 + "/marker.txt": "irrelevant deep path\n",
        },
    )
    write_archive(
        "edinyy_reestr_komu_popalo.zip",
        {
            "data/registry.sqlite": sqlite_bytes(groups[2] + [groups[2][702]]),
            "data/partial_dump.csv": csv_bytes(groups[2][:250]),
            "readme.md": (
                "# Единый Реестр Кого Попало\n"
                "Синтетические данные, не использовать как настоящие.\n"
            ),
            **{
                f"attachments/{number:03}/note.txt": f"служебная заметка {number}\n"
                for number in range(80)
            },
        },
    )
    xml_records = "\n".join(
        "<record>"
        + "".join(f"<{key}>{escape(value)}</{key}>" for key, value in record.items())
        + "</record>"
        for record in groups[3]
    )
    write_archive(
        "departament_bespoleznoy_analitiki.zip",
        {
            "xml/2024/оперативная_выгрузка.xml": (
                "<?xml version='1.0' encoding='utf-8'?>\n<records>\n"
            )
            + xml_records
            + "\n</records>\n",
            "json/metadata.json": json.dumps(
                {"organization": "Департамент Бесполезной Аналитики", "synthetic": True},
                ensure_ascii=False,
            ),
            "logs/large_audit.log": "".join(
                f"2024-11-19 audit={number:05} analysis completed without meaning\n"
                for number in range(12_000)
            ),
            "config/paths.conf": "root=/fictional/department\n",
        },
    )
    write_archive(
        "byuro_ochen_vazhnyh_tablic.zip",
        {
            "таблицы/итоговый_реестр.xlsx": xlsx_bytes(groups[4]),
            "таблицы/резерв.tsv": csv_bytes(groups[4][:450], delimiter="\t"),
            "docs/README.md": "Бюро Очень Важных Таблиц. Все лица и идентификаторы вымышлены.\n",
            "deep/a/b/c/d/e/служебный_файл.txt": "ничего интересного\n",
        },
    )
    archives = sorted(ROOT.glob("*.zip"))
    members = sum(len(zipfile.ZipFile(archive).infolist()) for archive in archives)
    compressed = sum(archive.stat().st_size for archive in archives)
    expanded = sum(
        info.file_size for archive in archives for info in zipfile.ZipFile(archive).infolist()
    )
    return {
        "archives": len(archives),
        "members": members,
        "records": 7_333,
        "compressed": compressed,
        "expanded": expanded,
    }


if __name__ == "__main__":
    print(json.dumps(generate(), ensure_ascii=False, sort_keys=True))
