"""
Веб-интерфейс для обработки сканов путевых листов ЭСМ-2.

Зависимости:
    pip install fastapi uvicorn watchdog pyzbar Pillow python-multipart requests

Запуск:
    python waybill_app.py
"""

import base64
import html
import io
import json
import logging
import os
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
import pypdfium2 as pdfium
import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from PIL import Image
from pyzbar import pyzbar
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

def _load_env():
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

_load_env()

SCAN_FOLDER   = r"C:\Scans"      # <<< ИЗМЕНИТЬ: путь к папке куда сохраняет сканер
TEMPLATE_PATH = Path(__file__).parent / "template_esm2.pdf"

WEB_HOST = "127.0.0.1"
WEB_PORT = 8765

API_BASE = os.environ.get("API_BASE", "http://77.51.227.6:80/buh30_upr/hs/api")
API_USER = os.environ.get("API_USER", "")
API_PASS = os.environ.get("API_PASS", "")

SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}

# Детектирование пустых/заполненных полей через vision-модель (OpenRouter)
# вместо плотности пикселей (печатные линии формы давали ложные срабатывания)
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL     = "https://openrouter.ai/api/v1/chat/completions"
USE_AI_DETECTION   = True
AI_MODEL           = "anthropic/claude-sonnet-4.6"

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Хранилище путевых (в памяти + JSON-файл на диске)
# ---------------------------------------------------------------------------

waybills: dict[str, dict] = {}
STATE_FILE = Path(__file__).parent / "waybills_state.json"

# PyMuPDF (fitz) и pdfium, похоже, не полностью потокобезопасны при работе из
# разных потоков одновременно (фоновая обработка сканов в _handle_new_scan —
# threading.Thread — и запросы через FastAPI могут пересечься во времени;
# на живом сервере поймали разовую порчу кодировки вставляемого текста при
# таком пересечении, в изоляции не воспроизводится). Сериализуем все
# операции с PDF-библиотеками этой блокировкой на всякий случай.
# RLock — не Lock: detect_fields_with_ai() сама может вызвать
# detect_filled_fields() как fallback, а это тот же поток, что и так держит блокировку.
_pdf_lock = threading.RLock()


def _save_state() -> None:
    """Сохранить waybills на диск (атомарно, чтобы не повредить файл при сбое)."""
    try:
        tmp_path = STATE_FILE.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(waybills, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, STATE_FILE)
    except Exception as exc:
        log.error("Не удалось сохранить waybills_state.json: %s", exc)


def _load_state() -> None:
    """Восстановить waybills из JSON-файла при старте приложения."""
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        waybills.update(data)
        log.info("Восстановлено путевых из %s: %d", STATE_FILE.name, len(data))
    except Exception as exc:
        log.error("Не удалось загрузить waybills_state.json: %s", exc)


# ---------------------------------------------------------------------------
# Автокалибровка точек вставки по перетаскиваниям операторов
# ---------------------------------------------------------------------------
# Поправки хранятся отдельно от SCAN_FIELDS (которые остаются "заводскими"
# координатами) — так автокалибровка переживает перезапуск, но всегда видно,
# что в SCAN_FIELDS было изначально, а что накопилось от реального использования.

FIELD_OVERRIDES_FILE = Path(__file__).parent / "field_position_overrides.json"
_field_overrides: dict[str, tuple] = {}   # field -> (xins, yins), в координатах шаблона

CALIBRATE_MIN_SAMPLES = int(os.environ.get("CALIBRATE_MIN_SAMPLES", "20"))
CALIBRATE_MIN_DRIFT    = float(os.environ.get("CALIBRATE_MIN_DRIFT", "2.0"))   # pt, ниже — шум
CALIBRATE_MAX_STEP     = float(os.environ.get("CALIBRATE_MAX_STEP", "15.0"))   # pt, защита от резкого скачка


def _load_field_overrides() -> None:
    if not FIELD_OVERRIDES_FILE.exists():
        return
    try:
        data = json.loads(FIELD_OVERRIDES_FILE.read_text(encoding="utf-8"))
        _field_overrides.update({k: tuple(v) for k, v in data.items()})
        log.info("Загружены калибровочные поправки: %d полей", len(_field_overrides))
    except Exception as exc:
        log.error("Не удалось загрузить field_position_overrides.json: %s", exc)


def _save_field_overrides() -> None:
    try:
        tmp = FIELD_OVERRIDES_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_field_overrides, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, FIELD_OVERRIDES_FILE)
    except Exception as exc:
        log.error("Не удалось сохранить field_position_overrides.json: %s", exc)


def _effective_ins(field: str):
    """Текущая точка вставки поля — с учётом автокалибровки, если она уже накопилась."""
    if field in _field_overrides:
        return _field_overrides[field]
    coords = SCAN_FIELDS.get(field)
    if not coords or coords[4] is None:
        return None
    return coords[4], coords[5]


def _median(values: list) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _collect_position_samples() -> dict:
    """Для каждого поля — отклонения (Δx, Δy) между ТЕКУЩЕЙ эффективной точкой
    вставки и тем, куда оператор реально перетащил метку при подтверждённой печати."""
    samples: dict[str, list] = {}
    for entry in waybills.values():
        if entry.get("status") != "confirmed":
            continue
        fields = entry.get("fields", {})
        sw, sh = _get_scan_dims(entry.get("file_path", ""))
        if not sw or not sh:
            continue
        for field in SCAN_FIELDS:
            eff = _effective_ins(field)
            if eff is None:
                continue
            xins, yins = eff
            left = fields.get(f"{field}_left_pct")
            top = fields.get(f"{field}_top_pct")
            if left in (None, "") or top in (None, ""):
                continue
            try:
                left, top = float(left), float(top)
            except (TypeError, ValueError):
                continue
            disp_w, disp_h = _disp_dims(sw, sh)
            xd, yd = (left / 100.0) * disp_w, (top / 100.0) * disp_h
            xp, yp = _disp_to_raw_pt(xd, yd, sw, sh)
            x_L, y_L = _scan_to_tmpl_pt(xp, yp, sw, sh)
            samples.setdefault(field, []).append((x_L - xins, y_L - yins))
    return samples


def _auto_calibrate() -> None:
    """Проверить, набралось ли на какое-то поле достаточно перетаскиваний, и если
    да — тихо обновить его эффективную точку вставки (в памяти + на диске).
    Шаг за раз ограничен CALIBRATE_MAX_STEP, чтобы один странный прогон не увёл
    координату далеко; медиана вместо среднего гасит случайные рывки."""
    samples = _collect_position_samples()
    changed = False
    for field, pts in samples.items():
        n = len(pts)
        if n < CALIBRATE_MIN_SAMPLES:
            continue
        dx = _median([p[0] for p in pts])
        dy = _median([p[1] for p in pts])
        if abs(dx) < CALIBRATE_MIN_DRIFT and abs(dy) < CALIBRATE_MIN_DRIFT:
            continue
        dx = max(-CALIBRATE_MAX_STEP, min(CALIBRATE_MAX_STEP, dx))
        dy = max(-CALIBRATE_MAX_STEP, min(CALIBRATE_MAX_STEP, dy))
        cur_x, cur_y = _effective_ins(field)
        new_x, new_y = round(cur_x + dx), round(cur_y + dy)
        if (new_x, new_y) != (cur_x, cur_y):
            log.info("Автокалибровка: %s (%s,%s) → (%s,%s) по %d образцам (медиана Δx=%.1f Δy=%.1f)",
                      field, cur_x, cur_y, new_x, new_y, n, dx, dy)
            _field_overrides[field] = (new_x, new_y)
            changed = True
    if changed:
        _save_field_overrides()

# ---------------------------------------------------------------------------
# 1С API
# ---------------------------------------------------------------------------

def _auth_headers() -> dict:
    token = base64.b64encode(f"{API_USER}:{API_PASS}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def fetch_order_by_pl(pl_number: str) -> tuple[dict, Optional[str]]:
    """
    Получить данные смены/заказа из 1С по штрихкоду путевого листа
    (working_shift_barcode). Возвращает (fields, warning):
      - warning=None            — одна запись, всё ок
      - warning="...не найден"  — пустой список, поля пустые
      - warning="...найдено N"  — несколько записей, использована первая
    Сетевые/HTTP-ошибки пробрасываются как exception — их ловит вызывающий код.
    """
    url = f"{API_BASE}/working_shift_barcode"
    payload = {"ШК": pl_number}
    resp = requests.post(url, json=payload, headers=_auth_headers(), timeout=30)
    if resp.status_code == 405:
        resp = requests.get(url, params=payload, headers=_auth_headers(), timeout=30)
    resp.raise_for_status()
    data = resp.json()

    fields = _empty_fields()

    # 1С отвечает объектом {"status":"error","info":"..."} вместо пустого
    # списка, когда заказ/смена по ШК не найдены — это не HTTP-ошибка (200 OK).
    if isinstance(data, dict) and data.get("status") == "error":
        info = data.get("info") or f"Заказ по штрихкоду {pl_number} не найден в 1С"
        return fields, f"{info} — заполните поля вручную"

    records = data if isinstance(data, list) else ([data] if data else [])

    if not records:
        return fields, f"Заказ по штрихкоду {pl_number} не найден в 1С — заполните поля вручную"

    warning = None
    if len(records) > 1:
        warning = (f"По штрихкоду {pl_number} найдено {len(records)} записей в 1С, "
                    "использована первая — проверьте данные вручную")

    rec = records[0]

    work_date = fields["work_date"]
    work_day_1 = ""
    date_raw = str(rec.get("Дата", "")).strip()
    if date_raw:
        try:
            dt = datetime.fromisoformat(date_raw)
            work_date = dt.strftime("%d.%m.%Y")
            work_day_1 = dt.strftime("%d")
        except ValueError:
            log.warning("  Не удалось разобрать дату из 1С: %s", date_raw)

    fields.update({
        "customer":      rec.get("КлиентНаименование", ""),
        "driver_name":   rec.get("ВодительНаименование", ""),
        "vehicle_type":  rec.get("МашинаНаименование", ""),
        "work_object":   rec.get("ОбъектРаботНаименование", ""),
        "work_date":     work_date,
        "work_day_1":    work_day_1,
        "work_object_1": rec.get("ОбъектРаботНаименование", ""),
        "order_number":  rec.get("НомерЗаказа", ""),
        "order_status":  rec.get("СтатусЗаказа", ""),
        "shift_status":  rec.get("СтатусСмены", ""),
    })
    return fields, warning


def save_to_1c(pl_number: str, fields: dict) -> None:
    """
    ЗАГЛУШКА: отправить подтверждённые поля в 1С.
    Раскомментировать когда endpoint будет готов.
    """
    log.info("[STUB] Сохранение путевого %s в 1С — endpoint не готов", pl_number)
    log.info("  Поля: %s", json.dumps(fields, ensure_ascii=False))

    # TODO: раскомментировать когда endpoint готов
    # url = f"{API_BASE}/zakaz_set_waybill"
    # payload = {"pl_number": pl_number, **fields}
    # resp = requests.post(url, json=payload, headers=_auth_headers(), timeout=30)
    # resp.raise_for_status()
    # log.info("Путевой %s сохранён в 1С", pl_number)


# ---------------------------------------------------------------------------
# Заполнение PDF-бланка ЭСМ-2
# ---------------------------------------------------------------------------

# Координаты полей (x, y_baseline) в пунктах PDF (страница A4 альбом 842×595)
# y = координата подчёркивания - 2pt (текст чуть выше линии)
_FONT   = "C:\\Windows\\Fonts\\arial.ttf"
_FSIZE  = 9   # кегль основного текста

# Захардкоженные значения (одинаковы для всех путевых)
ORG_NAME = ('ООО "СПЕЦСТРОЙМЕХАНИЗАЦИЯ" ИНН 7734422140, КПП 774301001\n'
            '125493, г.Москва, вн.тер.г. Муниципальный округ Головинский, '
            'ул.Смолная, д.2, пом.7Н/3, тел.:+7(499)399-31-65, 8(495)748-19-74')
ORG_OKPO = "51716881"
_ORG_FSIZE = 8   # шрифт для реквизита организации

_P1_FIELDS = {
    # Поле:           (x,   y)   — начало текста, базовая линия
    # work_date и period_* обрабатываются отдельно в fill_pdf()
    "organization":   (140, 111),   # Организация
    "org_okpo":       (722, 111),   # ОКПО организации: квадрат x=699..785 (центр 742), 8 цифр ~40pt → старт 722
    "customer":       (115, 132),   # Заказчик
    "vehicle_type":   ( 75, 160),   # Машина (марка)
    "vehicle_plate":  (445, 160),   # Гос. номер
    "driver_name":    ( 75, 184),   # Машинист (ФИО)
    "driver_id":      (733, 215),   # Табельный номер (крайняя правая ячейка)
    # Первая строка таблицы (один рабочий день)
    "work_day":       ( 27, 358),   # Число месяца (кол. 1)
    "work_object":    ( 55, 358),   # Наименование и адрес объекта (кол. 2)
    "time_out":       (215, 358),   # Время выезда (кол. 4)
    "time_in":        (354, 358),   # Время возвращения (кол. 7)
}

# Три квадрата "Дата составления": x=699..731 (день), 731..757 (месяц), 757..785 (год)
# Скорректировано по перетаскиванию оператора в live-превью 30.07.2026
# (путевой 9b2c6931) — исходные (703,96)/(733,96)/(759,96) печатали ниже нужного.
_DATE_BOXES = [(701, 90), (731, 90), (757, 90)]

# "Период работы": "с" x=506..544 (ширина 38pt, центр 525), "по" x=544..578 (ширина 33.5pt, центр 561)
# Скорректировано по перетаскиванию оператора в live-превью 30.07.2026 (путевой 9b2c6931)
_PERIOD_FSIZE = 7
_PERIOD_FROM  = (505, 208)
_PERIOD_TO    = (541, 209)

# ---------------------------------------------------------------------------
# Зоны детектирования в координатах ЛАНДШАФТНОГО ШАБЛОНА (842×595pt)
# Формат: (x0_L, y0_L, x1_L, y1_L, x_ins_L, y_ins_L)
# x_ins/y_ins — точка вставки текста в шаблоне; None = спецобработка
# ---------------------------------------------------------------------------
SCAN_FIELDS: dict[str, tuple] = {
    # --- Шапка ---
    "work_date":       (692,  74, 728,  94,   None,  None),  # ДД (числа)
    "work_date_2":     (725,  75, 754,  92,   None,  None),  # ММ
    "work_date_3":     (754,  74, 782,  90,   None,  None),  # ГГГГ
    "company_name":    ( 61,  86, 638, 110,   None,  None),  # Организация
    "customer":        ( 93, 109, 636, 132,   103,   126),
    "vehicle_type":    ( 52, 136, 272, 156,    72,   156),
    "vehicle_plate":   (423, 136, 662, 157,   436,   150),
    "driver_name":     ( 54, 162, 395, 193,    63,   178),
    "driver_id":       (674, 198, 725, 212,   731,   208),
    "period_from":     (504, 198, 539, 213,   None,  None),
    "period_to":       (542, 197, 573, 212,   None,  None),
    # --- Таблица строк (3 дня, откалибровано по реальным сканам через calibrate_zones.py
    #     + скорректировано по фактическим перетаскиваниям оператора 24.07.2026) ---
    "work_day_1":      (  7, 336,  43, 358,      9,   353),
    "work_object_1":   ( 44, 336, 160, 356,     54,   353),
    "time_out_1":      (202, 336, 240, 355,    204,   354),
    "time_in_1":       (346, 335, 381, 356,    347,   351),

    "work_day_2":      (  7, 356,  43, 378,      9,   373),
    "work_object_2":   ( 45, 358, 160, 379,     47,   374),
    "time_out_2":      (201, 356, 240, 375,    207,   372),
    "time_in_2":       (346, 357, 380, 377,    348,   372),

    "work_day_3":      (  9, 376,  43, 399,     11,   394),
    "work_object_3":   ( 44, 378, 160, 399,     46,   394),
    "time_out_3":      (203, 375, 239, 398,    205,   393),
    "time_in_3":       (347, 377, 380, 398,    349,   393),
}
_DETECT_THRESHOLD = 0.025  # доля тёмных пикселей для признания поля заполненным

# Точки вставки для полей со "спецобработкой" в fill_scan_pdf() (work_date —
# сразу 3 квадрата, period_from/to — тесные колонки).
# Нужны и для live-превью (_overlay_positions), и как опорная точка при
# перетаскивании (см. fill_scan_pdf): "work_date" — один маркер на весь блок
# даты, день/месяц/год двигаются вместе на ту же дельту.
# company_name сюда намеренно не входит — организация печатается захардкоженным
# реквизитом, без превью и перетаскивания.
_SPECIAL_ANCHORS: dict[str, tuple] = {
    "work_date":    _DATE_BOXES[0],
    "period_from":  _PERIOD_FROM,
    "period_to":    _PERIOD_TO,
}

# ---------------------------------------------------------------------------
# Coordinate transform: ландшафтный шаблон (842×595) → реальные координаты скана
# Сканер часто создаёт портретный PDF (595×842) с содержимым, повёрнутым на 90° по часовой.
# Формула 90° CW: (x_L, y_L) → (H_L − y_L, x_L) при H_L=595
# ---------------------------------------------------------------------------
_TMPL_W = 842.0
_TMPL_H = 595.0


def _scan_is_portrait(scan_w: float, scan_h: float) -> bool:
    return scan_h > scan_w


def _tmpl_to_scan_pt(x_L: float, y_L: float, sw: float, sh: float) -> tuple:
    """Перевод точки из ландшафтного шаблона в координаты скана."""
    if not _scan_is_portrait(sw, sh):
        return (x_L * sw / _TMPL_W, y_L * sh / _TMPL_H)
    # Портрет: 90° CW → (H_L − y_L, x_L), масштабирование
    return ((_TMPL_H - y_L) * (sw / _TMPL_H), x_L * (sh / _TMPL_W))


def _scan_to_tmpl_pt(xp: float, yp: float, sw: float, sh: float) -> tuple:
    """Обратное к _tmpl_to_scan_pt: точка скана → координаты ландшафтного шаблона."""
    if not _scan_is_portrait(sw, sh):
        return (xp * _TMPL_W / sw, yp * _TMPL_H / sh)
    return (yp * _TMPL_W / sh, _TMPL_H - xp * _TMPL_H / sw)


# ---------------------------------------------------------------------------
# "Сырое" пространство скана (в котором реально вставляется текст) → "показ"
# (развёрнутая для человека картинка, что отдаёт /image). Формула проверена
# эмпирически на маркере: rotate(90°, expand=True) переводит точку (x,y) на
# картинке (W,H) в (y, W−x) на повёрнутой (H,W). Только для портретных сканов —
# ландшафтные и так читаются нормально, без поворота.
# ---------------------------------------------------------------------------

def _raw_to_disp_pt(xp: float, yp: float, sw: float, sh: float) -> tuple:
    if not _scan_is_portrait(sw, sh):
        return (xp, yp)
    return (yp, sw - xp)


def _disp_to_raw_pt(xd: float, yd: float, sw: float, sh: float) -> tuple:
    if not _scan_is_portrait(sw, sh):
        return (xd, yd)
    return (sw - yd, xd)


def _disp_dims(sw: float, sh: float) -> tuple:
    """Размер картинки после показа (для портрета — с поворотом, ширина/высота меняются местами)."""
    return (sh, sw) if _scan_is_portrait(sw, sh) else (sw, sh)


def _tmpl_region_to_scan(x0: float, y0: float, x1: float, y1: float,
                          sw: float, sh: float) -> tuple:
    """Перевод прямоугольника из ландшафтного шаблона в координаты скана."""
    if not _scan_is_portrait(sw, sh):
        return (x0 * sw / _TMPL_W, y0 * sh / _TMPL_H,
                x1 * sw / _TMPL_W, y1 * sh / _TMPL_H)
    sx = sw / _TMPL_H
    sy = sh / _TMPL_W
    # Бокс (x0,y0)→(x1,y1) в ландшафте → портрет: x=595-y, y=x
    xp0 = (_TMPL_H - y1) * sx
    yp0 = x0 * sy
    xp1 = (_TMPL_H - y0) * sx
    yp1 = x1 * sy
    return (xp0, yp0, xp1, yp1)

_P2_FIELDS = {
    # Первая строка таблицы оборотной стороны: y≈130
    "work_day2":      ( 48, 130),   # Число месяца
    "work_start":     ( 60, 125),   # Начало работы (верхняя строка)
    "work_end":       ( 60, 133),   # Окончание работы (нижняя строка)
    "work_object2":   (100, 130),   # Наименование и адрес объекта
    "hours_worked":   (355, 130),   # Отработано часов (кол. 7)
    "work_cost":      (406, 130),   # Стоимость работы руб.коп. (кол. 8)
}


def fill_pdf(pl_number: str, fields: dict) -> bytes:
    """
    Открыть шаблон ЭСМ-2, вставить данные и вернуть PDF-байты.
    """
    doc = fitz.open(str(TEMPLATE_PATH))

    # --- Страница 1 ---
    p1 = doc[0]

    # Дата составления — три отдельных квадрата: ДД | ММ | ГГГГ
    date_str = str(fields.get("work_date", "")).strip()
    if date_str:
        parts = date_str.replace("-", ".").replace("/", ".").split(".")
        parts += ["", "", ""]          # гарантируем 3 элемента
        for text, (x, y) in zip(parts[:3], _DATE_BOXES):
            if text:
                p1.insert_text(fitz.Point(x, y), text,
                               fontname="Arial", fontfile=_FONT,
                               fontsize=_FSIZE, color=(0, 0, 0))

    # Период работы — "с" и "по" в тесных квадратах, мелкий шрифт
    for key, (x, y) in [("period_from", _PERIOD_FROM), ("period_to", _PERIOD_TO)]:
        val = str(fields.get(key, "")).strip()
        if val:
            p1.insert_text(fitz.Point(x, y), val,
                           fontname="Arial", fontfile=_FONT,
                           fontsize=_PERIOD_FSIZE, color=(0, 0, 0))

    # Остальные поля шапки (organization и org_okpo — мелкий шрифт, остальные — _FSIZE)
    for key, (x, y) in _P1_FIELDS.items():
        val = str(fields.get(key, "")).strip()
        if not val:
            continue
        if key == "organization":
            # 2 строки: первая на 8pt выше подчёркивания, вторая на нём
            # i=0 → y-8, i=1 → y
            for i, line in enumerate(val.split("\n")):
                if line.strip():
                    p1.insert_text(fitz.Point(x, y + (i - 1) * 8), line,
                                   fontname="Arial", fontfile=_FONT,
                                   fontsize=_ORG_FSIZE, color=(0, 0, 0))
        else:
            fsize = _FSIZE
            p1.insert_text(
                fitz.Point(x, y),
                val,
                fontname="Arial", fontfile=_FONT,
                fontsize=fsize, color=(0, 0, 0),
            )

    # --- Страница 2 ---
    if len(doc) > 1:
        p2 = doc[1]
        for key, (x, y) in _P2_FIELDS.items():
            val = str(fields.get(key, "")).strip()
            if not val:
                continue
            p2.insert_text(
                fitz.Point(x, y),
                val,
                fontname="Arial", fontfile=_FONT,
                fontsize=_FSIZE, color=(0, 0, 0),
            )

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


# ---------------------------------------------------------------------------
# Детектирование заполненных полей на скане
# ---------------------------------------------------------------------------

def detect_filled_fields(file_path: str) -> dict:
    """Обёртка с блокировкой — см. _detect_filled_fields_impl()."""
    with _pdf_lock:
        return _detect_filled_fields_impl(file_path)


def _detect_filled_fields_impl(file_path: str) -> dict:
    """
    Рендерит первую страницу скана и проверяет наличие чернил в зонах полей.
    Возвращает {field_name: True} если поле заполнено, False если пустое.
    """
    result = {k: False for k in SCAN_FIELDS}
    try:
        if not file_path.lower().endswith(".pdf"):
            log.warning("Детектирование: только PDF, пропускаем %s", file_path)
            return result

        doc_scan = pdfium.PdfDocument(file_path)
        if len(doc_scan) == 0:
            return result

        page_scan = doc_scan[0]
        page_w = page_scan.get_width()
        page_h = page_scan.get_height()
        portrait = _scan_is_portrait(page_w, page_h)
        scale = 2
        bitmap = page_scan.render(scale=scale)
        img = bitmap.to_pil().convert("L")
        img_w, img_h = img.size
        log.info("Анализ скана %s: %dx%d px (страница %.0f×%.0f pt, %s)",
                 Path(file_path).name, img_w, img_h, page_w, page_h,
                 "портрет" if portrait else "ландшафт")

        for field, (x0, y0, x1, y1, *_) in SCAN_FIELDS.items():
            sx0, sy0, sx1, sy1 = _tmpl_region_to_scan(x0, y0, x1, y1, page_w, page_h)
            px0 = max(0, int(sx0 * scale))
            py0 = max(0, int(sy0 * scale))
            px1 = min(img_w, int(sx1 * scale))
            py1 = min(img_h, int(sy1 * scale))
            region = img.crop((px0, py0, px1, py1))
            pixels = list(region.getdata())
            if not pixels:
                continue
            dark_ratio = sum(1 for p in pixels if p < 180) / len(pixels)
            result[field] = dark_ratio > _DETECT_THRESHOLD
            log.info("  %-15s scan=(%d,%d-%d,%d) dark=%.3f → %s",
                     field, px0, py0, px1, py1, dark_ratio,
                     "заполнено" if result[field] else "ПУСТО")
    except Exception as exc:
        log.error("detect_filled_fields: %s", exc)
    return result


# Узкие поля (crop+zoom улучшает точность): ширина < ~200pt в шаблоне
_NARROW_FIELDS = frozenset({
    "work_date", "work_date_2", "work_date_3", "vehicle_plate", "driver_id",
    "period_from", "period_to",
    "work_day_1", "work_day_2", "work_day_3",
    "work_object_1", "work_object_2", "work_object_3",
    "time_out_1", "time_out_2", "time_out_3",
    "time_in_1",  "time_in_2",  "time_in_3",
})

# Группы полей для БЛОЧНЫХ вырезков — соседние поля отдаём модели одним
# изображением (видно линии таблицы и соседние ячейки целиком), это устойчивее
# к calibration-неточностям в 1-2px, чем вырезка каждого поля по отдельности:
# соседний почерк иначе может "протечь" в чужую ячейку и дать ложный True.
_CROP_GROUPS: dict[str, list[str]] = {
    "date_block":   ["work_date", "work_date_2", "work_date_3"],
    "period_block": ["period_from", "period_to"],
    "table_block":  [
        "work_day_1", "work_object_1", "time_out_1", "time_in_1",
        "work_day_2", "work_object_2", "time_out_2", "time_in_2",
        "work_day_3", "work_object_3", "time_out_3", "time_in_3",
    ],
}
_CROP_STANDALONE = frozenset({"vehicle_plate", "driver_id"})

_AI_FIELD_DESCRIPTIONS = """\
- work_date: дата составления (3 квадратика дд.мм.гг, блок "Коды" справа)
- customer: строка "Заказчик"
- vehicle_type: марка машины
- vehicle_plate: государственный номерной знак машины
- driver_name: машинист (Ф.И.О.)
- driver_id: табельный номер машиниста
- period_from: период работы "с"
- period_to: период работы "по"
- work_day_1..3: число месяца в строке 1–3 таблицы
- work_object_1..3: наименование и адрес объекта в строке 1–3
- time_out_1..3: время выезда из гаража в строке 1–3
- time_in_1..3: время возвращения в гараж в строке 1–3"""


def detect_fields_with_ai(file_path: str) -> dict:
    """
    Определяет заполненные/пустые поля скана через vision-модель на OpenRouter.
    Гибридный подход: целая страница (контекст + широкие поля) + увеличенные
    вырезки для узких полей — в одном API-запросе.
    При ошибке откатывается на detect_filled_fields().
    """
    result = {k: False for k in SCAN_FIELDS}
    try:
        if not file_path.lower().endswith(".pdf"):
            return detect_filled_fields(file_path)
        if not OPENROUTER_API_KEY:
            log.warning("OPENROUTER_API_KEY не задан — fallback на пиксельный метод")
            return detect_filled_fields(file_path)

        doc_scan = pdfium.PdfDocument(file_path)
        if len(doc_scan) == 0:
            return result
        page_scan = doc_scan[0]
        pw, ph = page_scan.get_width(), page_scan.get_height()
        portrait = _scan_is_portrait(pw, ph)

        # Целая страница — для контекста и широких полей
        bitmap_full = page_scan.render(scale=2)
        img_full = bitmap_full.to_pil().convert("RGB")
        buf_full = io.BytesIO()
        img_full.save(buf_full, format="JPEG", quality=85)
        full_b64 = base64.b64encode(buf_full.getvalue()).decode()

        # Увеличенные вырезки БЛОКАМИ полей (масштаб ×3 для чёткости) —
        # вместо тесной вырезки каждой ячейки отдельно отдаём модели блок с
        # видимой сеткой таблицы, чтобы она сама различала соседние строки.
        CROP_SCALE = 3
        bitmap_crop = page_scan.render(scale=CROP_SCALE)
        img_crop_base = bitmap_crop.to_pil().convert("RGB")
        crop_b64: dict[str, str] = {}      # label -> base64
        crop_fields: dict[str, list[str]] = {}  # label -> поля внутри этого блока

        def _make_crop(label: str, fields_in_group: list[str]) -> None:
            boxes = [SCAN_FIELDS[f][:4] for f in fields_in_group if f in SCAN_FIELDS]
            if not boxes:
                return
            x0 = min(b[0] for b in boxes)
            y0 = min(b[1] for b in boxes)
            x1 = max(b[2] for b in boxes)
            y1 = max(b[3] for b in boxes)
            sx0, sy0, sx1, sy1 = _tmpl_region_to_scan(x0, y0, x1, y1, pw, ph)
            pad = 10
            box = (
                max(0, int(sx0 * CROP_SCALE) - pad),
                max(0, int(sy0 * CROP_SCALE) - pad),
                min(img_crop_base.width,  int(sx1 * CROP_SCALE) + pad),
                min(img_crop_base.height, int(sy1 * CROP_SCALE) + pad),
            )
            crop = img_crop_base.crop(box)
            if portrait:
                crop = crop.rotate(90, expand=True)  # 90° CCW → читается горизонтально
            buf = io.BytesIO()
            crop.save(buf, format="JPEG", quality=90)
            crop_b64[label] = base64.b64encode(buf.getvalue()).decode()
            crop_fields[label] = fields_in_group

        for group_name, fields_in_group in _CROP_GROUPS.items():
            _make_crop(group_name, fields_in_group)
        for field in _CROP_STANDALONE:
            _make_crop(field, [field])

        field_names = list(SCAN_FIELDS.keys())
        schema_hint = ", ".join(f'"{f}": true/false' for f in field_names)

        prompt_text = (
            "Это скан рукописного путевого листа ЭСМ-2 (строительная машина). "
            "Для каждого поля ниже определи, заполнено ли оно рукописным "
            "текстом/цифрами оператора (true) или оставлено пустым (false). "
            "Печатные линии, рамки таблиц и типографский текст формы НЕ считаются "
            "заполнением — только рукописные записи.\n\n"
            f"Поля:\n{_AI_FIELD_DESCRIPTIONS}\n\n"
            "Первое изображение — полный скан (контекст). Далее — увеличенные "
            "вырезки БЛОКАМИ: table_block содержит 3 строки таблицы подряд "
            "сверху вниз (число/объект/выезд/возврат в каждой) — ориентируйся "
            "на видимые линии сетки, чтобы не перепутать соседние строки. "
            "date_block — 3 квадратика даты подряд. period_block — поля «с»/«по» рядом.\n"
            "Ответь СТРОГО валидным JSON без пояснений и markdown-разметки, "
            f"со всеми перечисленными ключами: {{{schema_hint}}}"
        )

        content: list = [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{full_b64}"}},
        ]
        for label, b64 in crop_b64.items():
            fields_desc = ", ".join(crop_fields[label])
            content.append({"type": "text", "text": f"Вырезка блока «{label}» (поля: {fields_desc}):"})
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

        payload = {
            "model": AI_MODEL,
            "max_tokens": 512,
            "messages": [{"role": "user", "content": content}],
        }
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        }
        for attempt in range(3):
            resp = requests.post(OPENROUTER_URL, headers=headers,
                                 json=payload, timeout=90)
            if resp.status_code in (429, 502, 503):
                wait = 5 * (attempt + 1)
                log.warning("OpenRouter %s, повтор через %ds (попытка %d/3)",
                            resp.status_code, wait, attempt + 1)
                time.sleep(wait)
                continue
            break
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        ai_result = json.loads(text)

        for field in field_names:
            result[field] = bool(ai_result.get(field, False))
        log.info("Детектирование (AI+crop) %s: %s", Path(file_path).name, result)
    except Exception as exc:
        log.error("detect_fields_with_ai: %s — fallback на пиксельный метод", exc)
        return detect_filled_fields(file_path)
    return result


def fill_scan_pdf(scan_path: str, fields: dict, filled_on_scan: dict) -> bytes:
    """Обёртка с блокировкой — см. _fill_scan_pdf_impl()."""
    with _pdf_lock:
        return _fill_scan_pdf_impl(scan_path, fields, filled_on_scan)


def _fill_scan_pdf_impl(scan_path: str, fields: dict, filled_on_scan: dict) -> bytes:
    """
    Берёт оригинальный скан PDF и дописывает текст поверх пустых полей.
    Автоматически определяет ориентацию скана (портрет/ландшафт) и
    применяет нужный поворот текста и трансформацию координат.
    """
    doc = fitz.open(scan_path)
    p1 = doc[0]
    sw = p1.rect.width
    sh = p1.rect.height
    portrait = _scan_is_portrait(sw, sh)
    rot = 270 if portrait else 0   # 270° = 90° CW = текст читается как в ландшафте

    def ins(x_L: float, y_L: float, text: str, fsize: float = None):
        """Вставить текст по ландшафтным координатам шаблона."""
        xp, yp = _tmpl_to_scan_pt(x_L, y_L, sw, sh)
        ins_scan(xp, yp, text, fsize)

    def ins_scan(xp: float, yp: float, text: str, fsize: float = None):
        """Вставить текст по координатам скана напрямую (без transform из шаблона)."""
        p1.insert_text(
            fitz.Point(xp, yp), text,
            fontname="Arial", fontfile=_FONT,
            fontsize=fsize or _FSIZE, color=(0, 0, 0),
            rotate=rot,
        )

    def _pct_override(field: str):
        """Если оператор перетащил live-превью поля — вернуть (xp, yp) в СЫРЫХ
        координатах скана (проценты приходят из формы в пространстве ПОКАЗА,
        нужно перевести обратно через _disp_to_raw_pt)."""
        left = fields.get(f"{field}_left_pct")
        top = fields.get(f"{field}_top_pct")
        try:
            if left not in (None, "") and top not in (None, ""):
                disp_w, disp_h = _disp_dims(sw, sh)
                xd = (float(left) / 100.0) * disp_w
                yd = (float(top) / 100.0) * disp_h
                return _disp_to_raw_pt(xd, yd, sw, sh)
        except (TypeError, ValueError):
            pass
        return None

    # Дата составления — 3 квадрата ДД|ММ|ГГГГ (каждый квадрат отдельно).
    # Перетаскивание двигает весь блок разом: дельта считается в координатах
    # шаблона от опорной точки (день) и применяется ко всем трём квадратам.
    if not filled_on_scan.get("work_date"):
        date_str = str(fields.get("work_date", "")).strip()
        if date_str:
            parts = date_str.replace("-", ".").replace("/", ".").split(".")
            parts += ["", "", ""]
            dx_L = dy_L = 0.0
            override = _pct_override("work_date")
            if override:
                ox_L, oy_L = _scan_to_tmpl_pt(override[0], override[1], sw, sh)
                dx_L, dy_L = ox_L - _DATE_BOXES[0][0], oy_L - _DATE_BOXES[0][1]
            for text, (x, y) in zip(parts[:3], _DATE_BOXES):
                if text:
                    ins(x + dx_L, y + dy_L, text)

    # Период работы "с" и "по"
    if not filled_on_scan.get("period_from"):
        val = str(fields.get("period_from", "")).strip()
        if val:
            override = _pct_override("period_from")
            if override:
                ins_scan(override[0], override[1], val, fsize=_PERIOD_FSIZE)
            else:
                ins(_PERIOD_FROM[0], _PERIOD_FROM[1], val, fsize=_PERIOD_FSIZE)

    if not filled_on_scan.get("period_to"):
        val = str(fields.get("period_to", "")).strip()
        if val:
            override = _pct_override("period_to")
            if override:
                ins_scan(override[0], override[1], val, fsize=_PERIOD_FSIZE)
            else:
                ins(_PERIOD_TO[0], _PERIOD_TO[1], val, fsize=_PERIOD_FSIZE)

    # Остальные поля — эффективная точка вставки (заводская или автокалиброванная)
    for field in SCAN_FIELDS:
        if field in ("work_date", "period_from", "period_to"):
            continue
        if filled_on_scan.get(field):
            continue
        eff = _effective_ins(field)
        if eff is None:
            continue
        val = str(fields.get(field, "")).strip()
        if not val:
            continue
        override = _pct_override(field)
        if override:
            ins_scan(override[0], override[1], val)
        else:
            ins(eff[0], eff[1], val)

    # Организация — захардкоженный реквизит, печатается всегда (не зависит от fields),
    # если строка на скане ещё не заполнена. Две строки в зоне "company_name".
    if not filled_on_scan.get("company_name"):
        cx0, cy0, cx1, cy1 = SCAN_FIELDS["company_name"][:4]
        org_lines = ORG_NAME.split("\n")
        for i, line in enumerate(org_lines[:2]):
            if line.strip():
                ins(cx0 + 2, cy1 - 12 + i * 11, line, fsize=_ORG_FSIZE)

    # Метаданные страницы: говорим любому просмотрщику/принтеру повернуть
    # при показе — тогда и скан, и наш текст выглядят единообразно ровно
    # (без этого предпросмотр в приложении был ровным, а открытый PDF — нет).
    if portrait:
        p1.set_rotation(270)

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


# ---------------------------------------------------------------------------
# Штрихкод
# ---------------------------------------------------------------------------

def _images_from_file(file_path: str) -> list[Image.Image]:
    """Вернуть список PIL-изображений из файла (PDF — все страницы, иначе одна)."""
    if file_path.lower().endswith(".pdf"):
        doc = pdfium.PdfDocument(file_path)
        images = []
        for i in range(len(doc)):
            page = doc[i]
            bitmap = page.render(scale=3)   # 300 dpi эквивалент
            images.append(bitmap.to_pil())
        return images
    return [Image.open(file_path)]


def read_barcode(file_path: str) -> Optional[str]:
    """Прочитать штрихкод из файла. Возвращает полный штрихкод или None."""
    try:
        for img in _images_from_file(file_path):
            decoded = pyzbar.decode(img)
            if decoded:
                return decoded[0].data.decode("utf-8")
    except Exception as exc:
        log.error("Ошибка чтения штрихкода из %s: %s", file_path, exc)
    return None


# ---------------------------------------------------------------------------
# Watchdog — мониторинг папки сканера
# ---------------------------------------------------------------------------

class ScanHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return
        time.sleep(1.5)  # дать сканеру закончить запись файла
        _handle_new_scan(str(path))


def _handle_new_scan(file_path: str):
    job_id = str(uuid.uuid4())[:8]
    log.info("Новый скан: %s → id=%s", file_path, job_id)

    entry = {
        "id":             job_id,
        "file_path":      file_path,
        "file_name":      Path(file_path).name,
        "created_at":     datetime.now().isoformat(),
        "status":         "pending",
        "barcode":        None,
        "pl_number":      None,
        "fields":         {},
        "filled_on_scan": {},
        "error":          None,
        "warning":        None,
    }
    waybills[job_id] = entry

    # Определяем заполненные поля на скане (до запроса 1С)
    entry["filled_on_scan"] = (
        detect_fields_with_ai(file_path) if USE_AI_DETECTION
        else detect_filled_fields(file_path)
    )

    # Читаем штрихкод
    barcode = read_barcode(file_path)
    if barcode:
        entry["barcode"]   = barcode
        entry["pl_number"] = barcode    # номер путевого = весь штрихкод
        log.info("  Штрихкод: %s", barcode)
        try:
            entry["fields"], entry["warning"] = fetch_order_by_pl(barcode)
            if entry["warning"]:
                log.warning("  %s", entry["warning"])
        except Exception as exc:
            log.error("  Ошибка запроса 1С: %s", exc)
            entry["error"] = str(exc)
            entry["fields"] = _empty_fields()
    else:
        log.warning("  Штрихкод не найден — оператор заполнит вручную")
        entry["fields"] = _empty_fields()

    _save_state()

    # Открываем браузер
    url = f"http://{WEB_HOST}:{WEB_PORT}/waybill/{job_id}"
    webbrowser.open(url)
    log.info("  Браузер открыт: %s", url)


def _empty_fields() -> dict:
    fields = {
        "organization":  ORG_NAME,
        "org_okpo":      ORG_OKPO,
        "customer":      "",
        "driver_name":   "",
        "driver_id":     "",
        "vehicle_type":  "",
        "vehicle_plate": "",
        "work_object":   "",
        "work_address":  "",
        "work_date":     datetime.now().strftime("%d.%m.%Y"),
        "period_from":   "",
        "period_to":     "",
        "order_number":  "",
        "order_status":  "",
        "shift_status":  "",
    }
    for i in range(1, 4):
        fields[f"work_day_{i}"] = ""
        fields[f"work_object_{i}"] = ""
        fields[f"time_out_{i}"] = ""
        fields[f"time_in_{i}"] = ""
    return fields


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

app = FastAPI(title="Путевые листы ЭСМ-2")


@app.get("/", response_class=HTMLResponse)
async def index():
    pending   = sorted(
        [w for w in waybills.values() if w["status"] == "pending"],
        key=lambda x: x["created_at"], reverse=True,
    )
    confirmed = [w for w in waybills.values() if w["status"] == "confirmed"]

    rows = ""
    for w in pending:
        rows += f"""
        <tr>
          <td>{w['created_at'][11:19]}</td>
          <td>{w['file_name']}</td>
          <td>{w['pl_number'] or '—'}</td>
          <td><a href="/waybill/{w['id']}">Открыть →</a></td>
        </tr>"""
    if not rows:
        rows = "<tr><td colspan='4' style='color:#999;text-align:center'>Нет ожидающих</td></tr>"

    return HTMLResponse(content=INDEX_HTML.format(
        rows=rows,
        count_pending=len(pending),
        count_confirmed=len(confirmed),
        scan_folder=SCAN_FOLDER,
    ))


@app.post("/load-folder")
async def load_folder():
    """Обработать все файлы уже лежащие в SCAN_FOLDER."""
    scan_path = Path(SCAN_FOLDER)
    found = []
    for ext in SUPPORTED_EXTENSIONS:
        found.extend(scan_path.glob(f"*{ext}"))
        found.extend(scan_path.glob(f"*{ext.upper()}"))

    # пропустить уже загруженные файлы
    loaded_paths = {w["file_path"] for w in waybills.values()}
    new_files = [f for f in found if str(f) not in loaded_paths]

    for f in new_files:
        threading.Thread(target=_handle_new_scan, args=(str(f),), daemon=True).start()
        time.sleep(0.3)

    return JSONResponse({"loaded": len(new_files), "skipped": len(found) - len(new_files)})


@app.post("/upload")
async def upload_file(request: Request):
    """Принять файл загруженный через браузер и обработать его."""
    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(status_code=400, detail="Файл не передан")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Формат не поддерживается: {suffix}")

    # сохраняем во временную папку рядом со скриптом
    save_dir = Path(__file__).parent / "uploads"
    save_dir.mkdir(exist_ok=True)
    save_path = save_dir / f"{uuid.uuid4().hex[:8]}_{file.filename}"
    save_path.write_bytes(await file.read())

    job_id_holder = {}

    def process():
        _handle_new_scan(str(save_path))

    threading.Thread(target=process, daemon=True).start()
    return JSONResponse({"ok": True})


@app.get("/waybill/{job_id}", response_class=HTMLResponse)
async def waybill_page(job_id: str):
    w = waybills.get(job_id)
    if not w:
        raise HTTPException(status_code=404, detail="Путевой не найден")
    return HTMLResponse(content=_render_waybill(w))


@app.get("/image/{job_id}")
async def get_image(job_id: str, page: int = 0):
    """Отдать страницу скана как PNG (page=0 первая, page=1 вторая и т.д.)."""
    w = waybills.get(job_id)
    if not w:
        raise HTTPException(status_code=404)
    path = Path(w["file_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Файл скана не найден")

    if path.suffix.lower() == ".pdf":
        doc = pdfium.PdfDocument(str(path))
        page_idx = min(page, len(doc) - 1)
        pdf_page = doc[page_idx]
        pw, ph = pdf_page.get_width(), pdf_page.get_height()
        bitmap = pdf_page.render(scale=2)
        img = bitmap.to_pil()
        if _scan_is_portrait(pw, ph):
            img = img.rotate(90, expand=True)  # показываем развёрнуто, как человеку читать
    else:
        img = Image.open(str(path))

    buf = __import__("io").BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    from fastapi.responses import StreamingResponse
    return StreamingResponse(buf, media_type="image/png")


def _get_scan_dims(file_path: str):
    """Ширина/высота страницы скана в её собственных единицах (pt для PDF, px для картинок) —
    та же система координат, в которой fill_scan_pdf() потом реально вставляет текст."""
    try:
        if file_path.lower().endswith(".pdf"):
            doc = pdfium.PdfDocument(file_path)
            page = doc[0]
            return page.get_width(), page.get_height()
        img = Image.open(file_path)
        return float(img.width), float(img.height)
    except Exception:
        return None, None


def _overlay_positions(w: dict) -> dict:
    """Для каждого вставляемого поля — (left_pct, top_pct) для live-превью поверх
    ПОКАЗЫВАЕМОЙ (уже развёрнутой для человека) картинки — та же ориентация, что /image."""
    sw, sh = _get_scan_dims(w.get("file_path", ""))
    if not sw or not sh:
        return {}
    disp_w, disp_h = _disp_dims(sw, sh)
    positions = {}
    for field in SCAN_FIELDS:
        if field in ("work_date_2", "work_date_3"):
            continue  # часть блока даты work_date, отдельного поля формы для них нет
        eff = _effective_ins(field) or _SPECIAL_ANCHORS.get(field)
        if eff is None:
            continue
        xp, yp = _tmpl_to_scan_pt(eff[0], eff[1], sw, sh)
        xd, yd = _raw_to_disp_pt(xp, yp, sw, sh)
        positions[field] = (xd / disp_w * 100, yd / disp_h * 100)
    return positions


def _render_scan_image(job_id: str, scale: int = 2):
    """Рендерит первую страницу скана. Возвращает (img, page_w_pt, page_h_pt)."""
    import io
    w = waybills.get(job_id)
    if not w:
        return None, 0, 0
    path = Path(w["file_path"])
    if not path.exists() or path.suffix.lower() != ".pdf":
        return None, 0, 0
    doc = pdfium.PdfDocument(str(path))
    page = doc[0]
    page_w = page.get_width()
    page_h = page.get_height()
    bitmap = page.render(scale=scale)
    img = bitmap.to_pil().convert("RGB")
    return img, page_w, page_h


@app.get("/scan-grid/{job_id}")
async def scan_grid(job_id: str):
    """
    Скан с координатной сеткой каждые 50pt.
    Красные вертикальные линии = X, синие горизонтальные = Y.
    Используется для калибровки зон детектирования.
    """
    from PIL import ImageDraw
    from fastapi.responses import StreamingResponse
    import io

    scale = 2
    img, page_w, page_h = _render_scan_image(job_id, scale)
    if img is None:
        raise HTTPException(status_code=404)

    draw = ImageDraw.Draw(img)
    iw, ih = img.size

    # Вертикальные линии (X)
    for x_pt in range(0, int(page_w) + 1, 50):
        xp = int(x_pt * scale)
        draw.line([(xp, 0), (xp, ih)], fill=(220, 0, 0), width=1)
        draw.text((xp + 2, 4), str(x_pt), fill=(220, 0, 0))

    # Горизонтальные линии (Y)
    for y_pt in range(0, int(page_h) + 1, 50):
        yp = int(y_pt * scale)
        draw.line([(0, yp), (iw, yp)], fill=(0, 0, 200), width=1)
        draw.text((4, yp + 2), str(y_pt), fill=(0, 0, 200))

    # Заголовок с размерами страницы
    draw.rectangle([0, 0, 260, 22], fill=(0, 0, 0))
    draw.text((4, 4), f"Page: {page_w:.0f} x {page_h:.0f} pt  |  Image: {iw}x{ih}px",
              fill=(255, 255, 0))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/scan-debug/{job_id}")
async def scan_debug(job_id: str):
    """Скан с прямоугольниками текущих зон детектирования."""
    from PIL import ImageDraw
    from fastapi.responses import StreamingResponse
    import io

    scale = 2
    img, page_w, page_h = _render_scan_image(job_id, scale)
    if img is None:
        raise HTTPException(status_code=404)

    w = waybills.get(job_id)
    draw = ImageDraw.Draw(img)
    fos = w.get("filled_on_scan", {}) if w else {}

    for field, (x0, y0, x1, y1, *_) in SCAN_FIELDS.items():
        sx0, sy0, sx1, sy1 = _tmpl_region_to_scan(x0, y0, x1, y1, page_w, page_h)
        px0, py0 = int(sx0 * scale), int(sy0 * scale)
        px1, py1 = int(sx1 * scale), int(sy1 * scale)
        color = (0, 160, 0) if fos.get(field) else (200, 0, 0)
        draw.rectangle([px0, py0, px1, py1], outline=color, width=3)
        draw.text((min(px0, px1) + 2, min(py0, py1) + 2), f"{field}", fill=color)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/scan-markup/{job_id}")
async def scan_markup(job_id: str):
    """Скан с цветными полупрозрачными зонами всех полей SCAN_FIELDS."""
    from PIL import ImageDraw
    from fastapi.responses import StreamingResponse
    import io

    scale = 2
    img, page_w, page_h = _render_scan_image(job_id, scale)
    if img is None:
        raise HTTPException(status_code=404)

    _FIELD_COLORS = {
        "work_date":    (0,   180, 180),
        "period_from":  (0,   180, 180),
        "period_to":    (0,   180, 180),
        "customer":     (41,  128, 185),
        "vehicle_type": (142, 68,  173),
        "vehicle_plate":(142, 68,  173),
        "driver_name":  (230, 126, 34 ),
        "driver_id":    (230, 126, 34 ),
    }
    for i in range(1, 4):
        _FIELD_COLORS[f"work_day_{i}"] = (39, 174, 96)
        _FIELD_COLORS[f"work_object_{i}"] = (39, 174, 96)
        _FIELD_COLORS[f"time_out_{i}"] = (39, 174, 96)
        _FIELD_COLORS[f"time_in_{i}"] = (39, 174, 96)

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for field, (x0, y0, x1, y1, *_) in SCAN_FIELDS.items():
        sx0, sy0, sx1, sy1 = _tmpl_region_to_scan(x0, y0, x1, y1, page_w, page_h)
        px0 = int(sx0 * scale); py0 = int(sy0 * scale)
        px1 = int(sx1 * scale); py1 = int(sy1 * scale)
        r, g, b = _FIELD_COLORS.get(field, (120, 120, 120))
        draw.rectangle([px0, py0, px1, py1], fill=(r, g, b, 55))
        draw.rectangle([px0, py0, px1, py1], outline=(r, g, b, 230), width=2)
        draw.text((px0 + 3, py0 + 3), field, fill=(r, g, b, 255))

    result = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    buf = io.BytesIO()
    result.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/calibration-report", response_class=HTMLResponse)
async def calibration_report():
    """Отчёт: что автокалибровка уже применила и что копится на будущее.
    Сама правка происходит автоматически после каждой печати (см. _auto_calibrate) —
    эта страница только показывает текущее состояние."""
    samples = _collect_position_samples()
    rows = ""
    for field in SCAN_FIELDS:
        eff = _effective_ins(field)
        if eff is None:
            continue
        pts = samples.get(field, [])
        n = len(pts)
        overridden = field in _field_overrides
        orig_x, orig_y = SCAN_FIELDS[field][4], SCAN_FIELDS[field][5]
        cur_x, cur_y = eff
        dx = _median([p[0] for p in pts]) if n else 0.0
        dy = _median([p[1] for p in pts]) if n else 0.0
        if n == 0:
            note = "нет перетаскиваний"
        elif n < CALIBRATE_MIN_SAMPLES:
            note = f"копится: {n}/{CALIBRATE_MIN_SAMPLES}, текущий разброс Δx={dx:+.1f} Δy={dy:+.1f}"
        elif abs(dx) < CALIBRATE_MIN_DRIFT and abs(dy) < CALIBRATE_MIN_DRIFT:
            note = f"{n} образцов, отклонение незначительное — без изменений"
        else:
            note = f"{n} образцов, применится на следующей печати (Δx={dx:+.1f} Δy={dy:+.1f})"
        coord_str = (f"({orig_x},{orig_y}) → <b>({cur_x},{cur_y})</b> (автоприменено)"
                     if overridden else f"({orig_x},{orig_y})")
        rows += (f"<tr class='{'applied' if overridden else ''}'>"
                 f"<td>{field}</td><td>{n}</td><td>{coord_str}</td><td>{note}</td></tr>")

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Автокалибровка полей</title>
<style>
body{{font-family:sans-serif;margin:20px;background:#f5f5f5}}
table{{border-collapse:collapse;background:#fff;width:100%;box-shadow:0 1px 4px rgba(0,0,0,.1)}}
th{{background:#333;color:#fff;padding:8px 10px;text-align:left;font-size:13px}}
td{{padding:6px 10px;border-bottom:1px solid #eee;font-size:13px;font-family:monospace}}
tr.applied{{background:#eaffea}}
</style></head><body>
<h2>Автокалибровка точек вставки</h2>
<p>Правки применяются автоматически после каждой печати, если на поле накопилось
≥{CALIBRATE_MIN_SAMPLES} перетаскиваний и медианное отклонение ≥{CALIBRATE_MIN_DRIFT}pt
(шаг за раз ограничен {CALIBRATE_MAX_STEP}pt). Поправки хранятся в
<code>{FIELD_OVERRIDES_FILE.name}</code> и переживают перезапуск.</p>
<table>
<tr><th>Поле</th><th>Образцов</th><th>Координаты</th><th>Статус</th></tr>
{rows}
</table>
</body></html>""")


@app.post("/print/{job_id}")
async def print_waybill(job_id: str, request: Request):
    """Принять заполненные поля, сгенерировать PDF и вернуть его браузеру."""
    w = waybills.get(job_id)
    if not w:
        raise HTTPException(status_code=404)

    form      = await request.form()
    fields    = dict(form)
    pl_number = w.get("pl_number") or fields.pop("pl_number_manual", "") or "???"

    w["fields"] = fields
    log.info("Путевой %s — накладываем поля на скан", pl_number)

    try:
        scan_path = w["file_path"]
        filled_on_scan = w.get("filled_on_scan", {})
        pdf_bytes = fill_scan_pdf(scan_path, fields, filled_on_scan)
    except Exception as exc:
        log.error("Ошибка генерации PDF: %s", exc)
        _save_state()
        raise HTTPException(status_code=500, detail=str(exc))

    w["status"] = "confirmed"
    _save_state()
    _auto_calibrate()

    filename = f"PL_{pl_number}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# HTML шаблоны (встроенные, чтобы не нужно было папку templates)
# ---------------------------------------------------------------------------

INDEX_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>Путевые листы ЭСМ-2</title>
<meta http-equiv="refresh" content="10">
<style>
body{{font-family:Arial,sans-serif;margin:30px;background:#f0f2f5}}
h1{{color:#2c3e50;margin-bottom:5px}}
.subtitle{{color:#7f8c8d;font-size:13px;margin-bottom:20px}}
.stats{{display:flex;gap:16px;margin-bottom:20px}}
.stat{{background:white;border-radius:10px;padding:16px 28px;box-shadow:0 2px 6px rgba(0,0,0,.08)}}
.stat .num{{font-size:2.2em;font-weight:700;color:#e74c3c}}
.stat.ok .num{{color:#27ae60}}
.stat p{{margin:4px 0 0;color:#666;font-size:13px}}
.actions{{display:flex;gap:12px;margin-bottom:20px;align-items:center;flex-wrap:wrap}}
.btn{{padding:10px 20px;border:none;border-radius:6px;cursor:pointer;font-size:14px;font-weight:600}}
.btn-blue{{background:#2980b9;color:white}}
.btn-blue:hover{{background:#2471a3}}
.btn-green{{background:#27ae60;color:white}}
.btn-green:hover{{background:#229954}}
.upload-label{{background:#8e44ad;color:white;padding:10px 20px;border-radius:6px;cursor:pointer;font-size:14px;font-weight:600;display:inline-block}}
.upload-label:hover{{background:#7d3c98}}
#upload-input{{display:none}}
#msg{{font-size:13px;color:#27ae60;padding:6px 0}}
table{{width:100%;border-collapse:collapse;background:white;border-radius:10px;overflow:hidden;box-shadow:0 2px 6px rgba(0,0,0,.08)}}
th{{background:#2980b9;color:white;padding:12px 16px;text-align:left;font-size:13px}}
td{{padding:11px 16px;border-bottom:1px solid #f0f0f0;font-size:14px}}
tr:last-child td{{border-bottom:none}}
a{{color:#2980b9;text-decoration:none;font-weight:600}}
a:hover{{text-decoration:underline}}
.tip{{color:#aaa;font-size:12px;margin-top:15px}}
</style>
</head>
<body>
<h1>Обработка путевых листов ЭСМ-2</h1>
<p class="subtitle">Мониторинг папки: <code>{scan_folder}</code> &nbsp;·&nbsp; страница обновляется каждые 10 сек.</p>
<div class="stats">
  <div class="stat"><div class="num">{count_pending}</div><p>Ожидают обработки</p></div>
  <div class="stat ok"><div class="num">{count_confirmed}</div><p>Подтверждено</p></div>
</div>
<div class="actions">
  <button class="btn btn-blue" onclick="loadFolder()">↻ Загрузить из папки {scan_folder}</button>
  <label class="upload-label">
    ↑ Загрузить файлы вручную
    <input type="file" id="upload-input" multiple accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff" onchange="uploadFiles(this.files)">
  </label>
  <span id="msg"></span>
</div>
<table>
  <thead><tr><th>Время</th><th>Файл</th><th>№ Путевого</th><th>Действие</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<p class="tip">Или просто положите скан в папку <strong>{scan_folder}</strong> — браузер откроется автоматически.</p>
<script>
async function loadFolder() {{
  const msg = document.getElementById('msg');
  msg.style.color = '#999'; msg.textContent = 'Загружаю...';
  const r = await fetch('/load-folder', {{method:'POST'}});
  const d = await r.json();
  msg.style.color = '#27ae60';
  msg.textContent = d.loaded > 0
    ? 'Загружено файлов: ' + d.loaded + (d.skipped ? ' (пропущено уже загруженных: ' + d.skipped + ')' : '')
    : 'Новых файлов не найдено' + (d.skipped ? ' (уже загружены: ' + d.skipped + ')' : '');
  setTimeout(() => location.reload(), 2000);
}}
async function uploadFiles(files) {{
  const msg = document.getElementById('msg');
  msg.style.color = '#999'; msg.textContent = 'Загружаю ' + files.length + ' файл(ов)...';
  for (const file of files) {{
    const fd = new FormData();
    fd.append('file', file);
    await fetch('/upload', {{method:'POST', body:fd}});
  }}
  msg.style.color = '#27ae60'; msg.textContent = 'Готово, обрабатываю...';
  setTimeout(() => location.reload(), 2500);
}}
</script>
</body>
</html>"""


def _field_html(name: str, label: str, value: str,
                locked: bool = False, badge: str = "", on_scan: bool = False) -> str:
    if locked:
        border = "border-color:#27ae60"
        extras = 'readonly style="background:#f0f4f8;color:#555"'
        badge_html = '&nbsp;<span class="badge-fixed">фиксировано</span>'
    elif on_scan:
        # Поле уже заполнено на физическом документе — оператор читает скан
        border = "border-color:#6c757d"
        extras = ""
        badge_html = '&nbsp;<span class="badge-scan">на скане ✏</span>'
    elif value.strip() and badge:
        # Данные из 1С (поле было пустым на скане)
        border = "border-color:#27ae60"
        extras = ""
        badge_html = f'&nbsp;<span class="from1c">{badge}</span>'
    else:
        # Пусто — нужно заполнить вручную
        border = "border-color:#e74c3c" if not value.strip() else "border-color:#27ae60"
        extras = ""
        badge_html = ""
    return f"""
    <div class="field">
      <label>{label}{badge_html}</label>
      <input type="text" name="{name}" value="{html.escape(value)}" {extras}
             style="{border};width:100%;padding:8px 10px;border:2px solid;border-radius:5px;font-size:14px;box-sizing:border-box">
    </div>"""


def _render_waybill(w: dict) -> str:
    f   = w.get("fields", {})
    pl  = w.get("pl_number") or ""
    confirmed = w["status"] == "confirmed"

    badge = ('<span style="background:#27ae60;color:white;padding:3px 12px;'
             'border-radius:4px;font-size:13px">✓ Подтверждён</span>') if confirmed else ""

    def sec(title):
        return f'<div style="font-size:11px;font-weight:700;color:#2980b9;text-transform:uppercase;letter-spacing:.5px;margin:14px 0 6px;border-bottom:1px solid #e0e0e0;padding-bottom:3px">{title}</div>'

    fos = w.get("filled_on_scan", {})  # {field: True=заполнено на скане, False=пустое}

    # Поля которые уже заполнены на скане — не дозаполняем из 1С, не перезаписываем
    def fval(key):
        return "" if fos.get(key) else f.get(key, "")

    # Live-превью поверх скана: позиция каждого поля в % от картинки —
    # если оператор ранее перетаскивал (значение сохранено в f), берём его,
    # иначе — позиция по умолчанию из SCAN_FIELDS.
    default_pos = _overlay_positions(w)
    _raw_sw, _raw_sh = _get_scan_dims(w.get("file_path", ""))
    disp_w, _disp_h = _disp_dims(_raw_sw, _raw_sh) if _raw_sw and _raw_sh else (0, 0)
    overlay_labels = []
    overlay_hidden = []
    for field, (dleft, dtop) in default_pos.items():
        left = f.get(f"{field}_left_pct") or dleft
        top = f.get(f"{field}_top_pct") or dtop
        overlay_labels.append(
            f'<span class="live-label" data-field="{field}" '
            f'style="left:{left}%;top:{top}%"></span>'
        )
        overlay_hidden.append(
            f'<input type="hidden" name="{field}_left_pct" value="{f.get(f"{field}_left_pct") or ""}">'
            f'<input type="hidden" name="{field}_top_pct" value="{f.get(f"{field}_top_pct") or ""}">'
        )
    overlay_labels_html = "".join(overlay_labels)
    overlay_hidden_html = "".join(overlay_hidden)

    def fld(name, label, badge="из 1С", **kw):
        return _field_html(name, label, fval(name), badge=badge,
                           on_scan=fos.get(name, False), **kw)

    # Сводка: сколько пустых полей обнаружено
    empty_count = sum(1 for k, v in fos.items() if not v)
    filled_count = sum(1 for k, v in fos.items() if v)
    if fos:
        scan_summary = (
            f'<div style="background:#eaf4fb;border:1px solid #aed6f1;border-radius:6px;'
            f'padding:8px 12px;margin-bottom:12px;font-size:12px;color:#1a5276">'
            f'<b>Анализ скана:</b> заполнено вручную — <b>{filled_count}</b>, '
            f'пустых (дозаполним из 1С) — <b style="color:#e74c3c">{empty_count}</b>'
            f'</div>'
        )
    else:
        scan_summary = '<div style="color:#aaa;font-size:12px;margin-bottom:10px">Анализ скана не выполнен</div>'

    def day_row(i):
        return (
            '<div class="day-row">'
            + fld(f"work_day_{i}",    f"День {i}")
            + fld(f"work_object_{i}", "Объект")
            + fld(f"time_out_{i}",    "Выезд")
            + fld(f"time_in_{i}",     "Возврат")
            + '</div>'
        )

    day_rows_html = "".join(day_row(i) for i in range(1, 4))

    fields_html = scan_summary + sec("Основные реквизиты") + "".join([
        _field_html("company_name", "Организация", ORG_NAME.replace("\n", " "), locked=True),
        fld("work_date",   "Дата составления"),
        fld("period_from", "Период работы — с"),
        fld("period_to",   "Период работы — по"),
        fld("customer",    "Заказчик"),
    ]) + sec("Машина и машинист") + "".join([
        fld("vehicle_type",  "Машина (марка)"),
        fld("vehicle_plate", "Гос. номер"),
        fld("driver_name",   "Машинист (ФИО)"),
        fld("driver_id",     "Табельный №"),
    ]) + sec("Объект и время работы (до 3 дней)") + day_rows_html

    btn_html = "" if confirmed else """
    <button type="button" onclick="printWaybill()"
      style="width:100%;padding:14px;background:#27ae60;color:white;border:none;
             border-radius:6px;font-size:16px;cursor:pointer;margin-top:8px;font-weight:700">
      🖨 Дозаполнить и распечатать
    </button>"""

    err_block = ""
    if w.get("error"):
        err_block = f'<div style="background:#fde;border:1px solid #c00;border-radius:4px;padding:8px;margin-bottom:10px;font-size:13px">⚠ {w["error"]}</div>'

    warn_block = ""
    if w.get("warning"):
        warn_block = (
            f'<div style="background:#fff3cd;border:1px solid #ffc107;border-radius:4px;'
            f'padding:8px;margin-bottom:10px;font-size:13px;color:#7a5b00">⚠ {w["warning"]}</div>'
        )

    order_info = ""
    if f.get("order_number") or f.get("order_status") or f.get("shift_status"):
        order_info = (
            f'<div style="background:#f4f4f4;border:1px solid #ddd;border-radius:6px;'
            f'padding:8px 12px;margin-bottom:10px;font-size:12px;color:#333">'
            f'Заказ <b>№{f.get("order_number") or "—"}</b>'
            f' &nbsp;·&nbsp; статус заказа: <b>{f.get("order_status") or "—"}</b>'
            f' &nbsp;·&nbsp; статус смены: <b>{f.get("shift_status") or "—"}</b>'
            f'</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>Путевой {pl or '—'}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;font-family:Arial,sans-serif;background:#f0f2f5}}
.header{{background:#2980b9;color:white;padding:10px 18px;display:flex;align-items:center;gap:14px}}
.header a{{color:rgba(255,255,255,.8);text-decoration:none;font-size:13px}}
.header strong{{font-size:15px}}
.wrap{{display:flex;height:calc(100vh - 44px)}}
.img-panel{{flex:1;overflow:auto;background:#1a1a1a;display:flex;justify-content:center;align-items:flex-start;padding:12px}}
.img-panel img{{max-width:100%;height:auto;border:1px solid #444;border-radius:3px;display:block}}
#overlay-wrap{{position:relative;display:inline-block;line-height:0}}
.live-label{{position:absolute;top:0;left:0;transform:translate(0,-100%);
  writing-mode:horizontal-tb;white-space:nowrap;width:max-content;max-width:none;
  color:#000;font-weight:700;font-family:Arial,sans-serif;cursor:default;user-select:none;
  pointer-events:none;padding:1px 3px;line-height:1.15;margin:0;box-sizing:content-box}}
/* Поворот текста намеренно убран: он визуально совпадал с направлением
   скана, но из-за rotate()+transform-origin точка на экране переставала
   совпадать с координатой left/top, которая реально уходит в печать —
   отсюда "съезжание". Точность позиции важнее направления чтения подписи. */
.live-label.has-text{{cursor:move;pointer-events:auto;background:#fff200;outline:2px solid #c0392b;border-radius:2px}}
.live-label.dragging{{background:#ffd400}}
#overlay-hint{{display:none;position:absolute;top:4px;left:4px;background:#000c;color:#ffd;
  font-size:11px;padding:4px 8px;border-radius:4px;z-index:5}}
#overlay-hint.show{{display:block}}
.side{{width:390px;min-width:360px;overflow-y:auto;background:white;padding:18px 20px;border-left:1px solid #ddd}}
.side h3{{margin:0 0 5px;color:#2c3e50;font-size:15px}}
.meta{{font-size:12px;color:#999;margin-bottom:12px;border-bottom:1px solid #eee;padding-bottom:10px}}
.field{{margin-bottom:12px}}
.field label{{display:block;font-size:11px;font-weight:700;color:#555;margin-bottom:3px;text-transform:uppercase;letter-spacing:.4px}}
.day-row{{display:grid;grid-template-columns:52px 1fr 62px 62px;gap:6px;margin-bottom:8px;align-items:end}}
.day-row .field{{margin-bottom:0}}
.day-row .field label{{font-size:9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.day-row .field input{{padding:6px 4px;font-size:12px}}
.from1c{{background:#d4edda;color:#155724;border-radius:3px;padding:1px 5px;font-size:10px;text-transform:none;font-weight:400;letter-spacing:0}}
.badge-fixed{{background:#e8e8e8;color:#555;border-radius:3px;padding:1px 5px;font-size:10px;text-transform:none;font-weight:400;letter-spacing:0}}
.badge-scan{{background:#cfe2ff;color:#0a58ca;border-radius:3px;padding:1px 5px;font-size:10px;text-transform:none;font-weight:400;letter-spacing:0}}
.legend{{font-size:11px;color:#999;margin-bottom:12px;display:flex;gap:12px;align-items:center}}
.dot{{width:10px;height:10px;border-radius:50%;display:inline-block}}
#msg{{padding:10px;border-radius:5px;margin-top:10px;display:none;font-size:14px}}
.ok-msg{{background:#d4edda;color:#155724}}
.err-msg{{background:#f8d7da;color:#721c24}}
</style>
</head>
<body>
<div class="header">
  <a href="/">← Все путевые</a>
  <strong>Путевой лист ЭСМ-2 &nbsp;·&nbsp; {pl or 'без штрихкода'}</strong>
  {badge}
</div>
<div class="wrap">
  <div class="img-panel">
    <div style="text-align:center;padding:6px;background:#333;color:white;font-size:13px">
      <button onclick="switchPage(0)" id="btn0"
        style="background:#555;color:white;border:none;padding:4px 12px;border-radius:4px;cursor:pointer;margin:2px">
        Стр. 1
      </button>
      <button onclick="switchPage(1)" id="btn1"
        style="background:#555;color:white;border:none;padding:4px 12px;border-radius:4px;cursor:pointer;margin:2px">
        Стр. 2
      </button>
      <button onclick="window.open('/scan-grid/{w['id']}','_blank')"
        style="background:#117a8b;color:white;border:none;padding:4px 12px;border-radius:4px;cursor:pointer;margin:2px;font-size:11px">
        📐 Сетка координат
      </button>
      <button id="btnMarkup" onclick="toggleMarkup()"
        style="background:#6c3483;color:white;border:none;padding:4px 12px;border-radius:4px;cursor:pointer;margin:2px;font-size:11px">
        🔲 Разметка полей
      </button>
    </div>
    <div id="overlay-wrap">
      <img id="scan-img" src="/image/{w['id']}?page=0" alt="Скан">
      <div id="overlay-hint">Перетащите жёлтые подписи, если легли не туда — при печати текст пойдёт именно сюда</div>
      {overlay_labels_html}
    </div>
  </div>
  <div class="side">
    <h3>Данные путевого листа</h3>
    <div class="meta">
      Файл: {w['file_name']}<br>
      Штрихкод: {w.get('barcode') or '—'}<br>
      Добавлен: {w['created_at'][11:19]}
    </div>
    {err_block}
    {warn_block}
    {order_info}
    <div class="legend">
      <span class="dot" style="background:#6c757d"></span>&nbsp;есть на скане &nbsp;
      <span class="dot" style="background:#27ae60"></span>&nbsp;из 1С (авто) &nbsp;
      <span class="dot" style="background:#e74c3c"></span>&nbsp;заполнить
    </div>
    <form id="frm">
      {fields_html}
      {overlay_hidden_html}
      {btn_html}
    </form>
    <div id="msg"></div>
  </div>
</div>
<script>
var currentPage = 0;
var markupOn = false;
var PAGE_W = {disp_w or 0};

function updateOverlayVisibility() {{
  var show = currentPage === 0 && !markupOn;
  document.querySelectorAll('.live-label').forEach(function(l) {{ l.style.display = show ? '' : 'none'; }});
  var any = show && document.querySelectorAll('.live-label.has-text').length > 0;
  document.getElementById('overlay-hint').classList.toggle('show', any);
}}

function switchPage(n) {{
  currentPage = n;
  markupOn = false;
  document.getElementById('scan-img').src = '/image/{w["id"]}?page=' + n;
  document.getElementById('btn0').style.background = n===0 ? '#2980b9' : '#555';
  document.getElementById('btn1').style.background = n===1 ? '#2980b9' : '#555';
  var btn = document.getElementById('btnMarkup');
  btn.textContent = '🔲 Разметка полей';
  btn.style.background = '#6c3483';
  updateOverlayVisibility();
}}
function toggleMarkup() {{
  markupOn = !markupOn;
  var img = document.getElementById('scan-img');
  img.src = markupOn ? '/scan-markup/{w["id"]}' : '/image/{w["id"]}?page=' + currentPage;
  var btn = document.getElementById('btnMarkup');
  btn.textContent = markupOn ? '✕ Скрыть разметку' : '🔲 Разметка полей';
  btn.style.background = markupOn ? '#4a235a' : '#6c3483';
  updateOverlayVisibility();
}}
switchPage(0);

// --- Live-превью полей поверх скана: синхронизация текста + перетаскивание ---
(function() {{
  var wrap = document.getElementById('overlay-wrap');
  var img = document.getElementById('scan-img');
  if (!PAGE_W) return;

  function fontScale() {{
    var pxPerPt = img.clientWidth / PAGE_W;
    return Math.max(8, 9 * pxPerPt);
  }}
  function applyFontSize() {{
    var px = fontScale();
    document.querySelectorAll('.live-label').forEach(function(l) {{ l.style.fontSize = px + 'px'; }});
  }}
  img.addEventListener('load', function() {{ applyFontSize(); updateOverlayVisibility(); }});
  window.addEventListener('resize', applyFontSize);

  document.querySelectorAll('.live-label').forEach(function(label) {{
    var field = label.dataset.field;
    var input = document.querySelector('#frm input[name="' + field + '"]');
    if (!input) return;
    function sync() {{
      label.textContent = input.value;
      label.classList.toggle('has-text', !!input.value.trim());
      updateOverlayVisibility();
    }}
    input.addEventListener('input', sync);
    sync();
  }});

  var dragging = null;
  document.querySelectorAll('.live-label').forEach(function(label) {{
    label.addEventListener('mousedown', function(e) {{
      if (!label.classList.contains('has-text')) return;
      var rect = wrap.getBoundingClientRect();
      dragging = {{
        label: label, startX: e.clientX, startY: e.clientY,
        startLeft: parseFloat(label.style.left) || 0,
        startTop: parseFloat(label.style.top) || 0,
        wrapW: rect.width, wrapH: rect.height,
      }};
      label.classList.add('dragging');
      e.preventDefault();
    }});
  }});
  document.addEventListener('mousemove', function(e) {{
    if (!dragging) return;
    var dxPct = (e.clientX - dragging.startX) / dragging.wrapW * 100;
    var dyPct = (e.clientY - dragging.startY) / dragging.wrapH * 100;
    dragging.label.style.left = (dragging.startLeft + dxPct) + '%';
    dragging.label.style.top = (dragging.startTop + dyPct) + '%';
  }});
  document.addEventListener('mouseup', function() {{
    if (!dragging) return;
    var field = dragging.label.dataset.field;
    dragging.label.classList.remove('dragging');
    var leftInput = document.querySelector('#frm input[name="' + field + '_left_pct"]');
    var topInput = document.querySelector('#frm input[name="' + field + '_top_pct"]');
    if (leftInput) leftInput.value = parseFloat(dragging.label.style.left).toFixed(3);
    if (topInput) topInput.value = parseFloat(dragging.label.style.top).toFixed(3);
    dragging = null;
  }});

  applyFontSize();
  updateOverlayVisibility();
}})();

async function printWaybill() {{
  const btn = document.querySelector('button');
  btn.disabled = true;
  btn.textContent = 'Формирую PDF...';
  const msg = document.getElementById('msg');
  msg.style.display = 'none';
  try {{
    const resp = await fetch('/print/{w["id"]}', {{
      method: 'POST',
      body: new FormData(document.getElementById('frm'))
    }});
    if (!resp.ok) {{
      const txt = await resp.text();
      throw new Error(txt);
    }}
    const blob = await resp.blob();
    const url  = URL.createObjectURL(blob);
    window.open(url, '_blank');
    btn.textContent = '🖨 Распечатать путевой';
    btn.disabled = false;
  }} catch(e) {{
    msg.style.display = 'block';
    msg.className = 'err-msg'; msg.textContent = 'Ошибка: ' + e;
    btn.textContent = '🖨 Распечатать путевой';
    btn.disabled = false;
  }}
}}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def start_watcher():
    scan_path = Path(SCAN_FOLDER)
    if not scan_path.exists():
        log.warning("Папка сканера не найдена: %s — создаём", SCAN_FOLDER)
        scan_path.mkdir(parents=True, exist_ok=True)
    observer = Observer()
    observer.schedule(ScanHandler(), str(scan_path), recursive=False)
    observer.start()
    log.info("Мониторинг папки: %s", SCAN_FOLDER)
    return observer


if __name__ == "__main__":
    _load_state()
    _load_field_overrides()
    observer = start_watcher()
    log.info("Интерфейс доступен: http://%s:%d", WEB_HOST, WEB_PORT)

    # Открыть главную страницу при старте
    threading.Timer(1.5, lambda: webbrowser.open(f"http://{WEB_HOST}:{WEB_PORT}")).start()

    try:
        uvicorn.run(app, host=WEB_HOST, port=WEB_PORT, log_level="warning")
    finally:
        observer.stop()
        observer.join()
