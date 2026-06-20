import os
import json
import time
import uuid
import base64
import asyncio
import logging
from io import BytesIO
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

from PIL import Image, ImageDraw, ImageFont
import anthropic

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("supply-bot")

# --- Config from environment ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
}
OWNER_USER_IDS = {
    int(x) for x in os.environ.get("OWNER_USER_IDS", "").replace(" ", "").split(",") if x
}
MAX_TRIES_PER_USER = int(os.environ.get("MAX_TRIES_PER_USER", "2"))
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "").lstrip("@")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
# Когда true — бот открыт для любого пользователя Telegram, ALLOWED_USER_IDS
# не проверяется вообще. Лимит MAX_TRIES_PER_USER при этом остаётся главной
# защитой от случайного перерасхода — обязательно держи его разумным.
PUBLIC_BOT = os.environ.get("PUBLIC_BOT", "false").lower() in ("1", "true", "yes")

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")  # для анализа картинки И для поиска — точность важнее экономии

BASE_DIR = Path(__file__).parent
# Если задана STORAGE_DIR (например, смонтированный Volume на Railway) — храним
# рендеры и счётчик попыток там, чтобы они переживали передеплой. Без неё —
# как раньше, в папке рядом с кодом (стирается при каждом редеплое).
STORAGE_DIR = Path(os.environ.get("STORAGE_DIR", str(BASE_DIR)))
RENDERS_DIR = STORAGE_DIR / "renders"
RENDERS_DIR.mkdir(parents=True, exist_ok=True)
USAGE_FILE = STORAGE_DIR / "usage.json"
TEMPLATE = (BASE_DIR / "templates" / "app_template.html").read_text(encoding="utf-8")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

CHUNK_SIZE = 6
GRID_DIVISIONS = 10

# Эти сайты предлагаем проверить В ПЕРВУЮ ОЧЕРЕДЬ (профильные, проверенные),
# но поиск ими больше технически не ограничен — если там нет подходящего
# товара, модель имеет право поискать дальше у других надёжных украинских
# продавцов, а не выдавать что попало лишь бы заполнить позицию.
CATEGORY_DOMAINS = {
    "lighting": ["linija-svitla.ua", "svetilnikof.com.ua", "svetua.com.ua", "lampa.od.ua", "citylight.com.ua", "lustralux.com.ua"],
    "wood_decor": ["kronospan.com", "egger.com", "kronas.com.ua", "agtplus.ua", "scandiwall.com.ua"],
    "tile": ["plitka.ua", "plitkashop.com.ua", "cersanit.in.ua", "leoceramika.com", "topovi.com.ua", "stone.kiev.ua", "supers.com.ua"],
    "laminate": ["my-floor.com.ua", "parketiko.com.ua", "laminat-parketdoska.com.ua"],
    "paint": ["ncscolour.com.ua", "tikkurila-shop.com.ua", "colorstudio.com.ua"],
    "furniture": ["mebelok.com", "klen.ua", "ddn.ua", "dobralavka.ua", "taburetka.ua"],
    "quartz_marble": ["topovi.com.ua", "stone.kiev.ua", "supers.com.ua", "kitstone.kiev.ua"],
}

# А эти — технически заблокированы на уровне инструмента поиска, без исключений.
BLOCKED_DOMAINS = [
    "prom.ua", "rozetka.com.ua", "olx.ua",
    "ozon.ru", "wildberries.ru", "yandex.ru", "ya.ru", "avito.ru",
]

SUPPORT_LINE = f"\n\nПоддержка: https://t.me/{SUPPORT_USERNAME}" if SUPPORT_USERNAME else ""


def is_allowed(user_id: int) -> bool:
    if PUBLIC_BOT:
        return True
    return user_id in ALLOWED_USER_IDS or user_id in OWNER_USER_IDS


def load_usage() -> dict:
    if USAGE_FILE.exists():
        try:
            return json.loads(USAGE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_usage(data: dict):
    USAGE_FILE.write_text(json.dumps(data))


_usage_lock = asyncio.Lock()


async def try_consume_quota(user_id: int) -> bool:
    """True — можно обрабатывать. Владельцам лимит не считаем.
    Проверка и запись объединены под блокировкой, чтобы два почти
    одновременных запроса от одного человека не проскочили оба разом.
    ВАЖНО: счётчик хранится в файле на диске сервиса — без подключённого
    Volume (см. STORAGE_DIR) он обнуляется при каждом передеплое. Это
    дружелюбное ограничение, а не железная защита — основной барьер от
    перерасхода всё равно держи в лимите трат на console.anthropic.com."""
    if user_id in OWNER_USER_IDS:
        return True
    async with _usage_lock:
        usage = load_usage()
        count = usage.get(str(user_id), 0)
        if count >= MAX_TRIES_PER_USER:
            return False
        usage[str(user_id)] = count + 1
        save_usage(usage)
        return True


def resize_for_claude(raw_bytes: bytes, max_side: int = 1150) -> bytes:
    img = Image.open(BytesIO(raw_bytes)).convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    out = BytesIO()
    img.save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue()


def build_grid_overlay(clean_jpeg_bytes: bytes):
    img = Image.open(BytesIO(clean_jpeg_bytes)).convert("RGB")
    w, h = img.size
    cell = max(w, h) / GRID_DIVISIONS
    cols = min(18, max(1, round(w / cell)))
    rows = min(30, max(1, round(h / cell)))
    col_w, row_h = w / cols, h / rows

    overlay = img.copy()
    draw = ImageDraw.Draw(overlay, "RGBA")
    line_color = (255, 60, 60, 200)
    for c in range(1, cols):
        x = c * col_w
        draw.line([(x, 0), (x, h)], fill=line_color, width=2)
    for r in range(1, rows):
        y = r * row_h
        draw.line([(0, y), (w, y)], fill=line_color, width=2)

    font = ImageFont.load_default()
    for c in range(cols):
        for r in range(rows):
            label = f"{chr(65 + c)}{r + 1}"
            x, y = c * col_w + 3, r * row_h + 2
            draw.rectangle([x - 1, y - 1, x + len(label) * 6 + 1, y + 10], fill=(0, 0, 0, 170))
            draw.text((x, y), label, fill=(255, 230, 60, 255), font=font)

    out = BytesIO()
    overlay.save(out, format="JPEG", quality=85)
    return base64.b64encode(out.getvalue()).decode(), cols, rows


def cell_to_pct(cell, cols: int, rows: int):
    try:
        cell = str(cell).strip().upper()
        col_idx = max(0, min(cols - 1, ord(cell[0]) - 65))
        row_idx = max(0, min(rows - 1, int(cell[1:]) - 1))
        return round((col_idx + 0.5) / cols * 100, 1), round((row_idx + 0.5) / rows * 100, 1)
    except Exception:
        return 50.0, 50.0


def extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start_candidates = [i for i in [text.find("["), text.find("{")] if i != -1]
    if not start_candidates:
        raise ValueError("No JSON found in response")
    start = min(start_candidates)
    end = max(text.rfind("]"), text.rfind("}"))
    return json.loads(text[start:end + 1])


ANALYSIS_PROMPT_TEMPLATE = """Ты — ассистент архитектора-дизайнера с очень внимательным глазом на детали.

На картинке наложена сетка: красные линии делят её на {cols} столбцов (A-{last_col}) и {rows} строк (1-{rows}), подпись каждой ячейки — жёлтым текстом в её левом верхнем углу.

ВАЖНО — что искать: только крупные архитектурные и мебельные позиции — стены, потолок, пол, диван, шкаф, кровать и прочая крупная мебель, люстры, бра и другие светильники, плитка, деревянная отделка/панели.

НЕ включай мелкие предметы и личные вещи: полотенца, столовые приборы и посуду, одежду, мелкие аксессуары, лампочки (сами лампы внутри светильника — не нужны, а вот сам светильник нужен), книги, мелкий декор на столах.

Шаг 1. Пройдись по изображению зона за зоной: потолок, стены, пол, затем крупная мебель, затем освещение (только сами светильники).

Шаг 2. Для каждой обнаруженной позиции (6-10 штук) укажи:
- id: порядковый номер начиная с 1
- title: короткое название на русском
- eyebrow: категория одним словом ("Отделка", "Мебель", "Освещение")
- desc: 1-2 предложения, только визуально подтверждённые признаки (цвет, тип поверхности, материал). Если технологию нельзя определить точно — опиши нейтрально.
- cell: код ячейки сетки (например "C4"), где находится ЦЕНТР именно этого предмета — сверь с жёлтой подписью в этой ячейке, что она попадает на сам предмет, а не на пол под ним, стену за ним или соседний объект
- unit: "м²" для отделки стен/потолка/пола, "шт." для мебели/светильников
- tiered: true для отделки деревом/ДСП или плиткой/камнем (нужен подбор: инженерный материал + натуральный аналог); false для остального
- color_match: true, если это однотонная окрашенная поверхность, для которой имеет смысл подбирать цвет по вееру NCS; иначе false
- search_category: одно из ровно этих значений — "lighting" (люстры, бра, светильники), "wood_decor" (деревянная отделка, ДСП-панели, фасады из дерева/ДСП), "tile" (керамическая плитка/керамогранит на полу/стенах), "laminate" (ламинат, паркетная доска), "paint" (просто окрашенная поверхность), "furniture" (диваны, шкафы, кровати, столы, кресла), "quartz_marble" (столешницы, подоконники, облицовка из камня/кварцевого агломерата), "other" (если не подходит ни одно)

Ответь СТРОГО в виде JSON-массива объектов с этими полями. Никакого текста до или после JSON, никакого markdown."""


VERIFY_PROMPT_TEMPLATE = """Та же картинка с сеткой ({cols} столбцов A-{last_col}, {rows} строк) и черновой список позиций:

{items_json}

Сверь КАЖДУЮ позицию с картинкой:
1. cell — жёлтая подпись этой ячейки действительно находится на названном предмете (title), а не на полу/стене под ним и не на соседнем объекте? Если нет — укажи правильную ячейку.
2. desc/title — соответствуют видимому? Перепиши нейтральнее, если материал назван слишком конкретно без визуального подтверждения.

Верни ИСПРАВЛЕННЫЙ список из {count} позиций в ТОМ ЖЕ формате (id, title, eyebrow, desc, cell, unit, tiered, color_match, search_category). Только JSON, без текста и markdown."""


def analyze_render(overlay_b64: str, media_type: str, cols: int, rows: int) -> list:
    prompt = ANALYSIS_PROMPT_TEMPLATE.format(cols=cols, last_col=chr(64 + cols), rows=rows)
    t0 = time.monotonic()
    msg = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        timeout=90.0,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": overlay_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    log.info("analyze_render done in %.1fs", time.monotonic() - t0)
    text = "".join(b.text for b in msg.content if b.type == "text")
    items = extract_json(text)
    for it in items:
        it["x"], it["y"] = cell_to_pct(it.get("cell"), cols, rows)
    return items


def verify_items(overlay_b64: str, media_type: str, items: list, cols: int, rows: int) -> list:
    draft = [{k: v for k, v in it.items() if k not in ("x", "y", "data")} for it in items]
    prompt = VERIFY_PROMPT_TEMPLATE.format(
        cols=cols, last_col=chr(64 + cols), rows=rows,
        items_json=json.dumps(draft, ensure_ascii=False), count=len(draft),
    )
    t0 = time.monotonic()
    msg = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        timeout=90.0,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": overlay_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    log.info("verify_items done in %.1fs", time.monotonic() - t0)
    text = "".join(b.text for b in msg.content if b.type == "text")
    try:
        fixed = extract_json(text)
    except Exception:
        log.warning("Verification pass failed to parse, using draft items")
        fixed = draft
    for it in fixed:
        it["x"], it["y"] = cell_to_pct(it.get("cell"), cols, rows)
    return fixed


def build_batch_research_prompt(category: str, chunk: list) -> str:
    lines = []
    for it in chunk:
        text_blob = f"{it.get('title','')} {it.get('desc','')}".lower()
        extra = ""
        if "рейк" in text_blob or "рейч" in text_blob:
            extra = (" Это, скорее всего, рейчатая деревянная панель — по умолчанию считай её изделием "
                     "на заказ у столярной мастерской, и предлагай готовый заводской аналог только если "
                     "он реально близко совпадает по виду.")
        if it.get("color_match"):
            extra += (" Это однотонная окрашенная поверхность. Зайди на ncscolour.com.ua (официальный "
                      "представитель веера NCS Index 2050 в Украине) и подбери ближайший цвет именно по "
                      "этому каталогу — формат кода должен быть стандартным NCS, например 'NCS S 1502-Y' "
                      "(буква S, 4 цифры — чёрная/цветная составляющая, затем буква оттенка). Укажи код в "
                      "поле ncs_estimate. Также через colorstudio.com.ua или tikkurila-shop.com.ua укажи, "
                      "где в Украине можно заколеровать краску в этот код.")
        lines.append(
            f"- id {it['id']}: {it.get('title')} ({it.get('eyebrow')}). Описание: {it.get('desc')}. "
            f"Тип подбора: {'двухуровневый — сначала инженерный материал/декор, затем натуральный аналог' if it.get('tiered') else 'обычный — 1 максимально точный вариант, второй только если тоже очень похож'}."
            f"{extra}"
        )
    items_block = "\n".join(lines)

    domains = CATEGORY_DOMAINS.get(category)
    if domains:
        domain_rule = (f"Сначала проверь эти профильные сайты (они для этой категории обычно самые точные "
                        f"и качественные): {', '.join(domains)}. Если на них не нашлось ничего реально похожего — "
                        f"поищи дальше на других надёжных магазинах той же тематики, украинских или зарубежных.")
    else:
        domain_rule = "Ищи на любых надёжных специализированных магазинах — украинских или зарубежных."

    return f"""Ты помогаешь архитектору найти, где купить материалы и предметы для этих позиций:

{items_block}

ОБЩИЕ ПРАВИЛА:
- {domain_rule}
- Сайты могут быть из любой страны (Украина, Польша, Германия и т.д.) — главное, чтобы товар реально продавался и был похож на описание. Если для архитектора в Украине у зарубежного магазина нет прямой доставки — это нормально, он сам разберётся с логистикой, важно само совпадение по виду.
- НИКОГДА не используй российские сайты (.ru, ya.ru, ozon.ru, wildberries и подобные) и НИКОГДА не используй общие маркетплейсы (prom.ua, rozetka.com.ua, OLX и подобные) — они уже технически заблокированы, но не предлагай их и в рассуждениях.
- ТОЧНОСТЬ ВАЖНЕЕ КОЛИЧЕСТВА И ВАЖНЕЕ ЗАПОЛНЕННОСТИ. Предлагай товар, только если он РЕАЛЬНО похож на описание по цвету, фактуре, форме и материалу. Если уверенно похожего варианта только один — верни один. Максимум 2 варианта на уровень, и только если оба действительно близкие.
- Если после честного поиска ничего достаточно похожего не нашлось — оставь tiers пустым ("tiers": []). Это нормальный и ОЖИДАЕМЫЙ результат для редких/нестандартных позиций. Лучше честно ничего, чем случайный товар, который на самом деле не похож — архитектор должен доверять каждой ссылке, которую ты дал.
- В поле "url" — ссылка ДОЛЖНА вести на страницу КОНКРЕТНОГО найденного товара (карточка товара с фото, ценой и кнопкой купить), а не на раздел каталога и не на главную сайта.
- Цены и поставщиков бери ТОЛЬКО из реальных результатов поиска.

Ответь СТРОГО в виде JSON-объекта (без markdown, без пояснений):
{{"results": [
  {{"id": id_позиции, "tiers": [{{"name": "...", "options": [{{"name":"...","supplier":"...","price_label":"...","price_uah": число_или_null,"avail":"...","url":"..."}}]}}], "ncs_estimate": "код NCS или null"}}
]}}

Включи объект для каждой из {len(chunk)} позиций выше."""


def research_batch(category: str, chunk: list) -> dict:
    prompt = build_batch_research_prompt(category, chunk)
    max_uses = min(6, max(2, len(chunk) + 1))
    tool_def = {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": max_uses,
        "blocked_domains": BLOCKED_DOMAINS,
    }
    t0 = time.monotonic()
    resp = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=3000,
        timeout=120.0,
        tools=[tool_def],
        messages=[{"role": "user", "content": prompt}],
    )
    log.info("research_batch[%s, %d items] done in %.1fs", category, len(chunk), time.monotonic() - t0)
    text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        parsed = extract_json(text)
        results = parsed.get("results", []) if isinstance(parsed, dict) else []
    except Exception:
        log.warning("Batch research parse failed for chunk %s", [it["id"] for it in chunk])
        results = []
    by_id = {r.get("id"): r for r in results if isinstance(r, dict)}
    out = {}
    for it in chunk:
        r = by_id.get(it["id"])
        out[it["id"]] = {"tiers": r.get("tiers", []), "ncs_estimate": r.get("ncs_estimate")} if r else {"tiers": [], "ncs_estimate": None}
    return out


def render_app_page(render_id: str, image_b64: str, items: list):
    html = TEMPLATE.replace("__IMG_B64__", image_b64).replace(
        "__ITEMS_JSON__", json.dumps(items, ensure_ascii=False)
    )
    (RENDERS_DIR / f"{render_id}.html").write_text(html, encoding="utf-8")


# --- Telegram handlers ---

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_allowed(message.from_user.id):
        await message.answer(
            "Доступ к боту пока ограничен. Если тебе нужен доступ — обратись к администратору.\n"
            f"Твой Telegram ID: {message.from_user.id}{SUPPORT_LINE}"
        )
        return
    await message.answer(
        "Привет! Пришли фото рендера интерьера или фасада — разберу материалы и соберу "
        f"интерактивную карту с вариантами покупки в Украине.{SUPPORT_LINE}"
    )


@dp.message(F.photo)
async def handle_photo(message: Message):
    user_id = message.from_user.id
    if not is_allowed(user_id):
        await message.answer(
            f"Доступ к боту пока ограничен. Твой Telegram ID: {user_id} — "
            f"передай его администратору, чтобы получить доступ.{SUPPORT_LINE}"
        )
        return

    if message.media_group_id:
        await message.answer(
            f"Пришли, пожалуйста, одну картинку отдельным сообщением (не альбомом из нескольких фото) — "
            f"каждая попытка считается строго по одному фото.{SUPPORT_LINE}"
        )
        return

    if not await try_consume_quota(user_id):
        await message.answer(
            f"Пробный лимит ({MAX_TRIES_PER_USER} разбора) на этом аккаунте исчерпан. "
            f"Если нужно больше — напиши в поддержку.{SUPPORT_LINE}"
        )
        return

    status = await message.answer("Анализирую рендер — это займёт около минуты…")
    t_total = time.monotonic()

    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_io = await bot.download_file(file.file_path)
    clean_bytes = resize_for_claude(file_io.read())
    img_b64 = base64.b64encode(clean_bytes).decode()
    overlay_b64, cols, rows = build_grid_overlay(clean_bytes)

    try:
        items = await asyncio.to_thread(analyze_render, overlay_b64, "image/jpeg", cols, rows)
        items = await asyncio.to_thread(verify_items, overlay_b64, "image/jpeg", items, cols, rows)
    except Exception as e:
        log.exception("analyze_render failed")
        await status.edit_text(f"Не получилось разобрать рендер: {e}{SUPPORT_LINE}")
        return

    await status.edit_text(f"Нашёл {len(items)} позиций, ищу варианты покупки…")

    groups = {}
    for it in items:
        groups.setdefault(it.get("search_category", "other"), []).append(it)

    tasks = []
    for cat, group_items in groups.items():
        for i in range(0, len(group_items), CHUNK_SIZE):
            tasks.append((cat, group_items[i:i + CHUNK_SIZE]))

    async def do_task(cat, chunk):
        try:
            return await asyncio.to_thread(research_batch, cat, chunk)
        except Exception:
            log.exception("research_batch failed for category %s (%d items) — leaving them empty", cat, len(chunk))
            return {it["id"]: {"tiers": [], "ncs_estimate": None} for it in chunk}

    chunk_results = await asyncio.gather(*(do_task(c, ch) for c, ch in tasks))
    data_by_id = {}
    for cr in chunk_results:
        data_by_id.update(cr)
    for it in items:
        it["data"] = data_by_id.get(it["id"], {"tiers": [], "ncs_estimate": None})

    render_id = uuid.uuid4().hex[:10]
    render_app_page(render_id, img_b64, items)
    log.info("Render %s done in %.1fs total", render_id, time.monotonic() - t_total)

    if not PUBLIC_URL:
        await message.answer("Карта готова, но PUBLIC_URL ещё не настроен — добавь его и пришли рендер ещё раз.")
        return

    url = f"{PUBLIC_URL}/app/{render_id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть карту материалов", web_app=WebAppInfo(url=url))
    ]])
    await message.answer(
        f"Готово!\n\nСсылка для просмотра (можно переслать кому угодно, открывается в любом браузере):\n{url}{SUPPORT_LINE}",
        reply_markup=kb,
    )


# --- FastAPI app ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    polling_task = asyncio.create_task(dp.start_polling(bot))
    log.info("Bot polling started")
    yield
    polling_task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def health():
    return PlainTextResponse("OK")


@app.get("/app/{render_id}")
def get_render(render_id: str):
    path = RENDERS_DIR / f"{render_id}.html"
    if not path.exists():
        return HTMLResponse("<h1>Не найдено</h1>", status_code=404)
    return HTMLResponse(path.read_text(encoding="utf-8"))
