"""
Мониторинг заказов 1С и автоматическая привязка путевых листов через штрихкод.

Зависимости: pip install requests pyzbar Pillow
На Windows для pyzbar нужен zbar DLL: https://github.com/NaturalHistoryMuseum/pyzbar#windows
"""

import base64
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from PIL import Image
from pyzbar import pyzbar

# ---------------------------------------------------------------------------
# Загрузка .env (из той же папки что скрипт)
# ---------------------------------------------------------------------------

def _load_env():
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ[k.strip()] = v.strip()  # .env всегда приоритетнее уже заданных системных переменных

_load_env()

# ---------------------------------------------------------------------------
# Конфигурация (берётся из .env)
# ---------------------------------------------------------------------------

API_BASE         = os.environ.get("API_BASE",         "http://77.51.227.6:80/buh30_upr/hs/api")
API_USER         = os.environ.get("API_USER",         "")
API_PASS         = os.environ.get("API_PASS",         "")
PL_BARCODE_USER  = os.environ.get("PL_BARCODE_USER",  "")
PL_BARCODE_PASS  = os.environ.get("PL_BARCODE_PASS",  "")
DATE_RANGE_DAYS  = int(os.environ.get("DATE_RANGE_DAYS",  "14"))
POLL_INTERVAL_SEC= int(os.environ.get("POLL_INTERVAL_SEC","600"))

# Защита перед реальным запуском на боевом сервере:
# - DRY_RUN=1 (по умолчанию!) — ничего не пишет в 1С, только показывает что бы записал.
#   Осознанно выставить DRY_RUN=0 в .env, когда прогон в сухом режиме проверен.
# - MAX_ORDERS_PER_RUN — не более N заказов за один опрос, остальные подхватятся
#   на следующих циклах (не обрабатывать 490+ штук разом при первом запуске).
DRY_RUN            = os.environ.get("DRY_RUN", "1").strip().lower() not in ("0", "false", "no")
MAX_ORDERS_PER_RUN = int(os.environ.get("MAX_ORDERS_PER_RUN", "50"))

# Штрихкод путевого — 13 цифр (по всем наблюдавшимся примерам: 2026000057859 и т.п.).
# Если pyzbar распознает мусор — не даём этому улететь в 1С.
PL_BARCODE_PATTERN = re.compile(r"^\d{13}$")

LOG_FILE = str(Path(__file__).parent / "monitor_pl.log")
QUEUE_STATE_FILE = Path(__file__).parent / "monitor_pl_queue.json"


def _queue_key(order: dict) -> str:
    # Один заказ может содержать несколько смен и файлов.
    return json.dumps([order.get("id", ""), order.get("namef", "")], ensure_ascii=False)


def _load_queue_state() -> dict:
    try:
        state = json.loads(QUEUE_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("Ожидался объект состояния очереди")
        return {key: value for key, value in state.items()
                if isinstance(value, (int, float)) and value >= 0}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Не удалось прочитать очередь, начинаем новый обход: %s", exc)
        return {}


def _save_queue_state(state: dict) -> None:
    try:
        temporary = QUEUE_STATE_FILE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, QUEUE_STATE_FILE)
    except OSError as exc:
        log.error("Не удалось сохранить очередь: %s", exc)


def _select_batch(orders: list[dict], state: dict, limit: int) -> list[dict]:
    # Сначала ещё не проверенные; затем те, которые проверялись раньше остальных.
    # Номер заказа определяет порядок только при одинаковом приоритете.
    unique = {_queue_key(order): order for order in orders}
    ranked = sorted(unique.values(), key=lambda o: str(o.get("num", "")), reverse=True)
    ranked.sort(key=lambda order: state.get(_queue_key(order), 0))
    return ranked[:max(1, limit)]


# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# API-слой
# ---------------------------------------------------------------------------

def get_orders_without_pl() -> list[dict]:
    """Получить список заказов без путевого листа за последние DATE_RANGE_DAYS дней."""
    date_to = datetime.now()
    date_from = date_to - timedelta(days=DATE_RANGE_DAYS)

    url = f"{API_BASE}/zakaz_no_pl"
    payload = {
        "dateFrom": date_from.strftime("%Y-%m-%d"),
        "dateTo":   date_to.strftime("%Y-%m-%d"),
    }

    token = base64.b64encode(f"{API_USER}:{API_PASS}".encode("utf-8")).decode("ascii")
    headers = {"Authorization": f"Basic {token}"}
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    if resp.status_code == 405:
        resp = requests.get(url, params=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # API может вернуть как список, так и обёртку {orders: [...]}
    if isinstance(data, list):
        return data
    return data.get("orders", data.get("data", []))


def write_pl_to_order(order_id: str, order_num: str, pl_number: str) -> bool:
    """Записать штрихкод путевого листа в смену через pl_barcode. Возвращает True при успехе."""
    if DRY_RUN:
        log.info("  [DRY-RUN] записал бы ШК %s в смену %s (заказ %s) — реальная запись отключена (DRY_RUN=1)",
                  pl_number, order_id, order_num)
        return True

    url = f"{API_BASE}/pl_barcode"
    payload = {"ШК": pl_number, "ИДСмены": order_id}
    token = base64.b64encode(f"{PL_BARCODE_USER}:{PL_BARCODE_PASS}".encode("utf-8")).decode("ascii")
    headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
    body_bytes = __import__("json").dumps(payload, ensure_ascii=False).encode("utf-8")
    resp = requests.post(url, data=body_bytes, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "Ок":
        log.info("  OK: ШК %s записан в смену %s (заказ %s)", pl_number, order_id, order_num)
        return True
    log.warning("  Неожиданный ответ от pl_barcode (заказ %s, ШК %s): %s", order_num, pl_number, data)
    return False


# ---------------------------------------------------------------------------
# Обработка штрихкода
# ---------------------------------------------------------------------------

def unc_to_windows_path(namef: str) -> str:
    path = namef.strip()
    # После JSON-парсинга \\\\server → \\server (верно), но иногда → \server (теряется слэш)
    if path.startswith("\\") and not path.startswith("\\\\"):
        path = "\\" + path
    return path


def read_barcode_from_file(file_path: str) -> list[str]:
    """Открыть изображение и вернуть список найденных штрихкодов (строки)."""
    from PIL import ImageEnhance
    from pyzbar.pyzbar import Rect

    if file_path.lower().endswith(".pdf"):
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(file_path)
        img = doc[0].render(scale=2).to_pil().convert("RGB")
    else:
        img = Image.open(file_path).convert("RGB")

    gray = img.convert("L")

    def _try(image):
        decoded = pyzbar.decode(image)
        return [obj.data.decode("utf-8") for obj in decoded]

    # 1. оригинал
    r = _try(img);
    if r: return r
    # 2. grayscale
    r = _try(gray)
    if r: return r
    # 3. x2
    g2 = gray.resize((gray.width * 2, gray.height * 2), Image.LANCZOS)
    r = _try(g2)
    if r: return r
    # 4. x2 + контраст
    r = _try(ImageEnhance.Contrast(gray).enhance(2.0).resize((gray.width*2, gray.height*2), Image.LANCZOS))
    if r: return r
    # 5. x3 + контраст
    r = _try(ImageEnhance.Contrast(gray).enhance(2.0).resize((gray.width*3, gray.height*3), Image.LANCZOS))
    if r: return r
    # 6. бинаризация x2
    bw2 = gray.point(lambda p: 255 if p > 128 else 0, '1').convert('L').resize((gray.width*2, gray.height*2), Image.LANCZOS)
    r = _try(bw2)
    if r: return r
    # 7. кроп нижней половины x3
    h = gray.height
    bottom3 = ImageEnhance.Contrast(gray.crop((0, h//2, gray.width, h))).enhance(2.0).resize((gray.width*3, gray.height//2*3), Image.LANCZOS)
    r = _try(bottom3)
    if r: return r
    # 8. резкость x3
    r = _try(ImageEnhance.Sharpness(gray).enhance(3.0).resize((gray.width*3, gray.height*3), Image.LANCZOS))
    if r: return r
    # 9. x4 + контраст
    r = _try(ImageEnhance.Contrast(gray).enhance(2.0).resize((gray.width*4, gray.height*4), Image.LANCZOS))
    if r: return r
    # 10–13. повороты ±5° и ±10°
    base = ImageEnhance.Contrast(gray).enhance(2.0).resize((gray.width*2, gray.height*2), Image.LANCZOS)
    for angle in (5, -5, 10, -10):
        r = _try(base.rotate(angle, expand=True, fillcolor=255))
        if r: return r

    return []


def extract_pl_number(barcode: str) -> str:
    """Номер путевого листа — весь штрихкод целиком."""
    return barcode


def is_valid_pl_barcode(pl_number: str) -> bool:
    """Проверка формата перед записью в 1С — отсекает мусор, который pyzbar
    иногда распознаёт на плохих сканах (не похоже на реальный номер путевого)."""
    return bool(PL_BARCODE_PATTERN.match(pl_number))


# ---------------------------------------------------------------------------
# Основной цикл обработки
# ---------------------------------------------------------------------------

def process_order(order: dict) -> str:
    """Обрабатывает одну смену. Возвращает код результата для сводки в run_once()."""
    order_num = order.get("num", "?")
    order_id  = order.get("id", "?")
    namef_raw = order.get("namef", "")

    file_path = unc_to_windows_path(namef_raw)
    log.info("--- Заказ %s (id=%s) | файл: %s", order_num, order_id, file_path)

    if not file_path:
        log.warning("  Пустой путь к файлу, пропускаем заказ %s", order_num)
        return "empty_path"

    if not Path(file_path).exists():
        log.error("  Файл не найден: %s", file_path)
        return "file_missing"

    # Читаем штрихкод
    barcodes = read_barcode_from_file(file_path)

    if not barcodes:
        log.warning("  Штрихкод не найден в файле: %s", file_path)
        return "no_barcode"

    # Если штрихкодов несколько — пробуем все, берём первый подходящий по формату
    pl_number = None
    used_barcode = None
    for bc in barcodes:
        candidate = extract_pl_number(bc)
        if is_valid_pl_barcode(candidate):
            pl_number = candidate
            used_barcode = bc
            break
        log.warning("  Штрихкод '%s' не похож на номер путевого (ожидались 13 цифр) — пропускаю", bc)

    if pl_number is None:
        log.error("  Ни один штрихкод в заказе %s не прошёл проверку формата: %s", order_num, barcodes)
        return "invalid_format"

    print(
        f"  Заказ:         {order_num}\n"
        f"  Файл:          {file_path}\n"
        f"  Штрихкод:      {used_barcode}\n"
        f"  Номер путевого: {pl_number}"
    )
    log.info("  Код распознан (ещё не записан в 1С): заказ %s, штрихкод %s, путевой %s",
             order_num, used_barcode, pl_number)

    ok = write_pl_to_order(order_id, order_num, pl_number)
    return ("dry_run" if DRY_RUN else "written") if ok else "write_failed"


def run_once() -> None:
    log.info("=== Запуск опроса === (DRY_RUN=%s, лимит за прогон=%d)", DRY_RUN, MAX_ORDERS_PER_RUN)
    try:
        orders = get_orders_without_pl()
    except requests.RequestException as exc:
        log.error("Ошибка получения заказов: %s", exc)
        return
    except Exception as exc:
        log.exception("Непредвиденная ошибка получения заказов: %s", exc)
        return

    if not orders:
        log.info("Заказов без путевого листа не найдено.")
        return

    state = _load_queue_state()
    active_keys = {_queue_key(order) for order in orders}
    state = {key: value for key, value in state.items() if key in active_keys}
    total = len(active_keys)
    batch = _select_batch(orders, state, MAX_ORDERS_PER_RUN)
    deferred = total - len(batch)
    log.info("Найдено заказов: %d. Обрабатываю %d за этот прогон%s.",
             total, len(batch),
             f" ({deferred} останутся на следующие циклы)" if deferred else "")

    counts: dict[str, int] = {}
    for order in batch:
        try:
            result = process_order(order)
        except Exception as exc:
            log.exception("Необработанная ошибка для заказа %s: %s",
                          order.get("num", "?"), exc)
            result = "exception"
        counts[result] = counts.get(result, 0) + 1
        # Даже ошибка считается попыткой: следующая порция должна идти дальше.
        state[_queue_key(order)] = time.time()
        _save_queue_state(state)

    log.info(
        "=== Итог: проверено=%d; записано в 1С=%d; пробных без записи=%d; "
        "нет пути=%d; файл недоступен=%d; код не найден=%d; неверный формат=%d; "
        "отказ записи=%d; исключений=%d ===",
        len(batch), counts.get("written", 0), counts.get("dry_run", 0),
        counts.get("empty_path", 0), counts.get("file_missing", 0),
        counts.get("no_barcode", 0), counts.get("invalid_format", 0),
        counts.get("write_failed", 0), counts.get("exception", 0),
    )
    unchecked = sum(key not in state for key in active_keys)
    log.info("Ещё не проверено в текущей выборке: %d. Ошибочные записи будут повторяться по очереди.", unchecked)


def main() -> None:
    log.info("Скрипт запущен. Интервал опроса: %d сек. DRY_RUN=%s, лимит=%d/прогон",
              POLL_INTERVAL_SEC, DRY_RUN, MAX_ORDERS_PER_RUN)
    if DRY_RUN:
        log.warning("Режим DRY_RUN включён — в 1С ничего не пишется. "
                     "Проверьте лог и явно выставьте DRY_RUN=0 в .env, когда будете готовы к боевой записи.")
    while True:
        try:
            run_once()
        except Exception:
            # Ловим вообще всё, чтобы падение одного цикла не убивало весь фоновый процесс.
            log.exception("Опрос упал с необработанной ошибкой — цикл продолжает работать")
        log.info("Следующий запуск через %d сек...", POLL_INTERVAL_SEC)
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
